"""Config validation, ElevenLabs request, ARI client/event routing, media publishing."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from voice_agent.config import ConfigError, Role, Settings
from voice_agent.speech.tts import ElevenLabsTTS, SynthesizedAudio
from voice_agent.telephony.ari_client import AriClient, AriEventBus
from voice_agent.telephony.channel import AriCallChannel, CallHungUp
from voice_agent.telephony.media import MediaPublisher, SpeechRenderer

# ------------------------------------------------------------------ config


def test_live_monday_requires_ids():
    settings = Settings(monday_mode="live")
    with pytest.raises(ConfigError) as info:
        settings.validate_for(Role.WORKER, environ={})
    message = str(info.value)
    for name in (
        "MONDAY_API_TOKEN",
        "MONDAY_BOARD_ID",
        "MONDAY_GROUP_ID",
        "MONDAY_ASSIGNEE_COLUMN",
        "MONDAY_MOHAMED_PERSON_ID",
    ):
        assert name in message


def test_ari_role_requires_telephony_and_tts():
    with pytest.raises(ConfigError) as info:
        Settings().validate_for(Role.ARI, environ={})
    for name in ("ASTERISK_ARI_URL", "ASTERISK_ARI_PASSWORD", "ELEVENLABS_API_KEY"):
        assert name in str(info.value)


def test_mock_mode_simulator_needs_nothing():
    Settings(monday_mode="mock").validate_for(Role.SIMULATE, environ={})


def test_person_ids_discovered_from_env():
    env = {"MONDAY_MOHAMED_PERSON_ID": "123", "MONDAY_AHMED_PERSON_ID": "456", "OTHER": "1"}
    assert Settings().monday_person_ids(env) == {"mohamed": 123, "ahmed": 456}


def test_valid_live_config_passes():
    settings = Settings(
        monday_mode="live",
        monday_api_token="t",
        monday_board_id="1",
        monday_group_id="g",
        monday_assignee_column="person",
    )
    settings.validate_for(Role.WORKER, environ={"MONDAY_MOHAMED_PERSON_ID": "9"})


# ------------------------------------------------------------------ ElevenLabs


async def test_elevenlabs_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"\x00\x01" * 800)

    tts = ElevenLabsTTS(
        api_key="el-key", voice_id="I3u6waC588j43py1kDN2", transport=httpx.MockTransport(handler)
    )
    audio = await tts.synthesize("حاضر، هقول لمحمد يشتري هارد للسيرفر حالًا.", sample_rate=16000)
    request = seen[0]
    assert request.url.path == "/v1/text-to-speech/I3u6waC588j43py1kDN2"
    assert request.url.params["output_format"] == "pcm_16000"
    assert request.headers["xi-api-key"] == "el-key"
    assert json.loads(request.content) == {
        "text": "حاضر، هقول لمحمد يشتري هارد للسيرفر حالًا.",
        "model_id": "eleven_multilingual_v2",
    }
    assert audio.sample_rate == 16000 and len(audio.pcm) == 1600
    assert audio.to_wav()[:4] == b"RIFF"


# ------------------------------------------------------------------ media


class CountingTTS(ElevenLabsTTS):
    def __init__(self):
        self.calls = 0

    async def synthesize(self, text, *, sample_rate=16000):
        self.calls += 1
        return SynthesizedAudio(pcm=b"\x00\x00" * 100, sample_rate=sample_rate)

    @property
    def cache_namespace(self) -> str:
        return "test"


async def test_media_file_mode_and_cache(tmp_path):
    publisher = MediaPublisher(
        mode="file", sounds_dir=tmp_path, uri_prefix="/var/lib/asterisk/sounds/voice-monday"
    )
    tts = CountingTTS()
    renderer = SpeechRenderer(tts, publisher)
    uri = await renderer.render("أهلاً")
    assert uri.startswith("sound:/var/lib/asterisk/sounds/voice-monday/vm-")
    name = uri.rsplit("/", 1)[1]
    assert (tmp_path / f"{name}.sln16").stat().st_size == 200
    assert await renderer.render("أهلاً") == uri
    assert tts.calls == 1  # cached


def test_media_rejects_unsafe_names(tmp_path):
    publisher = MediaPublisher(mode="http", sounds_dir=tmp_path, public_base_url="http://a:8000")
    with pytest.raises(ValueError):
        publisher.path_for("../../etc/passwd")
    assert publisher.uri_for("vm-1") == "sound:http://a:8000/media/vm-1.wav"


# ------------------------------------------------------------------ ARI


def test_ari_events_url():
    client = AriClient(
        base_url="http://pbx:8088", username="u", password="p w", app="voice-monday-agent"
    )
    assert client.events_url() == (
        "ws://pbx:8088/ari/events?app=voice-monday-agent&api_key=u%3Ap+w&subscribeAll=false"
    )


class FakeAri:
    """Mimics the subset of AriClient used by AriCallChannel and emits events."""

    def __init__(self, bus: AriEventBus):
        self.bus = bus
        self.requests: list[tuple] = []
        self.recording_event: dict | None = None

    async def answer(self, channel_id):
        self.requests.append(("answer", channel_id))

    async def get_channel(self, channel_id):
        self.requests.append(("get_channel", channel_id))
        return {"id": channel_id}

    async def set_channel_var(self, channel_id, variable, value):
        self.requests.append(("set_channel_var", variable, value))

    async def play(self, channel_id, media, playback_id):
        self.requests.append(("play", media))
        asyncio.get_running_loop().call_soon(
            self.bus.dispatch, {"type": "PlaybackFinished", "playback": {"id": playback_id}}
        )

    async def stop_playback(self, playback_id):
        self.requests.append(("stop_playback", playback_id))

    async def stop_recording(self, name):
        self.requests.append(("stop_recording", name))
        self.bus.dispatch({"type": "RecordingFinished", "recording": {"name": name}})

    async def record(self, channel_id, name, **kwargs):
        self.requests.append(("record", name, kwargs))
        event = {"type": "RecordingFinished", "recording": {"name": name, "duration": 3}}
        event["recording"].update(self.recording_event or {"talking_duration": 2})
        asyncio.get_running_loop().call_soon(self.bus.dispatch, event)

    async def get_stored_recording(self, name):
        self.requests.append(("get", name))
        return b"RIFF" + b"\x00" * 4000

    async def delete_stored_recording(self, name):
        self.requests.append(("delete", name))

    async def hangup(self, channel_id, reason="normal"):
        self.requests.append(("hangup", channel_id))


class StaticRenderer:
    async def render(self, text):
        return "sound:/sounds/vm-test"


async def test_ari_channel_say_listen_and_hangup_event():
    bus = AriEventBus()
    ari = FakeAri(bus)
    channel = AriCallChannel(
        ari=ari, bus=bus, channel={"id": "1700000000.42"}, speech=StaticRenderer()
    )
    assert await channel.say("أهلاً") is True
    audio = await channel.listen(max_seconds=10, max_silence_seconds=2)
    assert audio.startswith(b"RIFF")
    record = next(r for r in ari.requests if r[0] == "record")
    assert record[1].startswith("vm-1700000000-42-1-")
    assert record[2]["max_silence_seconds"] == 2
    assert ("delete", record[1]) in ari.requests

    ari.recording_event = {"talking_duration": 0}
    assert await channel.listen(max_seconds=10, max_silence_seconds=2) is None  # silence

    bus.dispatch({"type": "StasisEnd", "channel": {"id": "1700000000.42"}})
    assert not channel.active
    with pytest.raises(CallHungUp):
        await channel.listen(max_seconds=10, max_silence_seconds=2)


async def test_ready_cue_starts_only_after_first_recording_is_active():
    bus = AriEventBus()
    ari = FakeAri(bus)
    channel = AriCallChannel(
        ari=ari,
        bus=bus,
        channel={"id": "c1"},
        speech=StaticRenderer(),
        ready_cue_media="sound:voice-monday/ready",
    )
    await channel.listen(max_seconds=10, max_silence_seconds=2)
    assert [r[0] for r in ari.requests[:2]] == ["record", "play"]
    assert ari.requests[1][1] == "sound:voice-monday/ready"
    await channel.listen(max_seconds=10, max_silence_seconds=2)
    assert sum(r[0] == "play" for r in ari.requests) == 1


async def test_final_reply_stops_unfinished_working_cue():
    class SlowCueAri(FakeAri):
        async def play(self, channel_id, media, playback_id):
            self.requests.append(("play", media))
            if media != "sound:voice-monday/working":
                asyncio.get_running_loop().call_soon(
                    self.bus.dispatch, {"type": "PlaybackFinished", "playback": {"id": playback_id}}
                )

    bus = AriEventBus()
    ari = SlowCueAri(bus)
    channel = AriCallChannel(
        ari=ari,
        bus=bus,
        channel={"id": "c2"},
        speech=StaticRenderer(),
        working_cue_media="sound:voice-monday/working",
    )
    await channel.working()
    assert await channel.say("تمام") is True
    assert [r[0] for r in ari.requests] == ["play", "stop_playback", "play"]


async def test_speech_during_playback_interrupts_and_is_preserved():
    class TalkingAri(FakeAri):
        async def record(self, channel_id, name, **kwargs):
            self.requests.append(("record", name, kwargs))

        async def play(self, channel_id, media, playback_id):
            self.requests.append(("play", media))
            asyncio.get_running_loop().call_soon(
                self.bus.dispatch,
                {"type": "ChannelTalkingStarted", "channel": {"id": channel_id}},
            )
            asyncio.get_running_loop().call_soon(
                self.bus.dispatch,
                {"type": "ChannelTalkingFinished", "channel": {"id": channel_id}},
            )

    bus = AriEventBus()
    ari = TalkingAri(bus)
    channel = AriCallChannel(ari=ari, bus=bus, channel={"id": "c3"}, speech=StaticRenderer())
    await channel.prepare()
    assert await channel.say("رد طويل") is True
    assert any(r[0] == "stop_playback" for r in ari.requests)
    assert await channel.listen(max_seconds=10, max_silence_seconds=2) == b"RIFF" + b"\x00" * 4000
    assert sum(r[0] == "record" for r in ari.requests) == 1


async def test_speech_during_synthesis_is_preserved_before_playback():
    class TalkingRenderer:
        async def render(self, text):
            assert any(r[0] == "record" for r in ari.requests)
            bus.dispatch({"type": "ChannelTalkingStarted", "channel": {"id": "c5"}})
            bus.dispatch({"type": "ChannelTalkingFinished", "channel": {"id": "c5"}})
            return "sound:/sounds/vm-test"

    class CapturingAri(FakeAri):
        async def record(self, channel_id, name, **kwargs):
            self.requests.append(("record", name, kwargs))

    bus = AriEventBus()
    ari = CapturingAri(bus)
    channel = AriCallChannel(ari=ari, bus=bus, channel={"id": "c5"}, speech=TalkingRenderer())
    await channel.prepare()
    assert await channel.say("رد طويل") is True
    assert not any(r[0] == "play" for r in ari.requests)
    assert await channel.listen(max_seconds=10, max_silence_seconds=2) == b"RIFF" + b"\x00" * 4000


async def test_normal_playback_discards_inbound_capture():
    bus = AriEventBus()
    ari = FakeAri(bus)
    channel = AriCallChannel(ari=ari, bus=bus, channel={"id": "c4"}, speech=StaticRenderer())
    await channel.prepare()
    assert await channel.say("تمام") is True
    assert any(r[0] == "stop_recording" for r in ari.requests)
    assert channel._pending_audio is None


async def test_ari_channel_does_not_beep_when_tts_fails():
    class BrokenRenderer:
        async def render(self, text):
            raise RuntimeError("ElevenLabs down")

    bus = AriEventBus()
    ari = FakeAri(bus)
    channel = AriCallChannel(ari=ari, bus=bus, channel={"id": "c1"}, speech=BrokenRenderer())
    assert await channel.say("أهلاً") is False
    assert not any(request[0] == "play" for request in ari.requests)


# ------------------------------------------------------------------ STT audio decoding


def test_wav_is_decoded_and_resampled_for_whisper():
    np = pytest.importorskip("numpy")
    from voice_agent.speech.stt import decode_audio
    from voice_agent.speech.tts import pcm_to_wav

    one_second_8k = (np.sin(np.arange(8000) / 10) * 10000).astype("<i2").tobytes()
    samples = decode_audio(pcm_to_wav(one_second_8k, 8000))
    assert samples.dtype == np.float32
    assert samples.shape == (16000,)
    assert float(abs(samples).max()) <= 1.0
