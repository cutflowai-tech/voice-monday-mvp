"""Voice loop behaviour with a fake call channel (no Asterisk, no audio)."""

from __future__ import annotations

from voice_agent.jobs.models import JobStatus
from voice_agent.speech.stt import SpeechToTextProvider, Transcription
from voice_agent.telephony.channel import CallChannel, CallHungUp
from voice_agent.telephony.voice_loop import VoiceLoop, VoiceLoopConfig

from .conftest import EXAMPLE

HANGUP = object()
SILENCE = None


class FakeChannel(CallChannel):
    """Plays a script of caller utterances; records everything the assistant says."""

    def __init__(self, script: list, *, tts_ok: bool = True):
        self.call_id = "fake-call"
        self.script = list(script)
        self.said: list[str] = []
        self.working_cues = 0
        self.answered = False
        self.hung_up_by_agent = False
        self._active = True
        self.tts_ok = tts_ok

    @property
    def active(self) -> bool:
        return self._active

    async def answer(self) -> None:
        self.answered = True

    async def say(self, text: str) -> bool:
        if not self._active:
            raise CallHungUp
        self.said.append(text)
        return self.tts_ok

    async def working(self) -> None:
        self.working_cues += 1

    async def listen(self, *, max_seconds: int, max_silence_seconds: int) -> bytes | None:
        if not self._active:
            raise CallHungUp
        if not self.script:
            return SILENCE
        item = self.script.pop(0)
        if item is HANGUP:
            self._active = False
            raise CallHungUp
        if isinstance(item, tuple) and item[0] == "hangup_after":
            self._active = False  # caller hung up right after speaking
            return item[1].encode()
        return item.encode() if isinstance(item, str) else item

    async def hangup(self) -> None:
        self.hung_up_by_agent = True
        self._active = False


class EchoSTT(SpeechToTextProvider):
    """The fake 'audio' is the UTF-8 transcript itself."""

    async def transcribe(self, audio: bytes, *, language: str) -> Transcription:
        return Transcription(text=audio.decode())


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0  # every check advances time a little
        return self.now


def make_loop(container, channel, **config) -> VoiceLoop:
    return VoiceLoop(
        channel=channel,
        stt=EchoSTT(),
        engine=container.engine,
        acks=container.acks,
        config=VoiceLoopConfig(**({"inactivity_timeout_seconds": 1000} | config)),
        clock=FakeClock(),
    )


async def test_call_stays_active_after_task_and_accepts_more(container, worker, mock_monday):
    channel = FakeChannel([EXAMPLE, SILENCE, "حطي تاسك لأحمد إنه يراجع الميزانية", HANGUP])
    loop = make_loop(container, channel)
    reason = await loop.run()

    assert reason == "caller_hangup"
    assert channel.answered and not channel.hung_up_by_agent
    assert len(channel.said) == 2
    assert channel.working_cues == 2
    assert "محمد" in channel.said[0] and "أحمد" in channel.said[1]
    assert len(loop.state.accepted_job_ids) == 2

    await worker.run_until_idle()  # after the hangup
    assert container.store.status_counts() == {"completed": 2}
    assert len(mock_monday.items) == 2


async def test_command_spoken_right_before_hangup_is_still_executed(container, worker):
    channel = FakeChannel([("hangup_after", EXAMPLE)])
    loop = make_loop(container, channel)
    assert await loop.run() == "caller_hangup"
    assert len(channel.said) == 0
    await worker.run_until_idle()
    job = container.store.get(loop.state.accepted_job_ids[0])
    assert job.status is JobStatus.COMPLETED


async def test_caller_can_end_the_call(container):
    channel = FakeChannel([EXAMPLE, "شكرا مع السلامة"])
    reason = await make_loop(container, channel).run()
    assert reason == "caller_requested"
    assert channel.hung_up_by_agent
    assert "مع السلامة" in channel.said[-1]


async def test_inactivity_timeout_ends_call(container):
    channel = FakeChannel([])  # caller never speaks
    loop = make_loop(container, channel, inactivity_timeout_seconds=5)
    assert await loop.run() == "inactivity_timeout"
    assert channel.hung_up_by_agent
    assert "هقفل" in channel.said[-1]


async def test_clarification_happens_inside_the_call(container):
    channel = FakeChannel(["حطي تاسك لمحمد", "يشتري هارد للسيرفر", HANGUP])
    loop = make_loop(container, channel)
    await loop.run()
    assert channel.said[0].endswith("؟")
    assert "هارد" in channel.said[1]
    assert len(loop.state.accepted_job_ids) == 1


async def test_repeated_technical_failures_end_the_session(container):
    class BrokenSTT(SpeechToTextProvider):
        async def transcribe(self, audio, *, language):
            raise RuntimeError("model crashed")

    channel = FakeChannel([EXAMPLE, EXAMPLE, EXAMPLE, EXAMPLE])
    loop = make_loop(container, channel, max_consecutive_failures=3)
    loop.stt = BrokenSTT()
    assert await loop.run() == "technical_failure"
    assert channel.hung_up_by_agent


async def test_unsupported_request_does_not_end_call(container):
    channel = FakeChannel(["ضيفي اجتماع للفريق يوم الخميس", EXAMPLE, HANGUP])
    loop = make_loop(container, channel)
    assert await loop.run() == "caller_hangup"
    assert not channel.hung_up_by_agent
    assert len(loop.state.accepted_job_ids) == 1


async def test_prepare_precedes_answer_and_immediate_recording(container):
    order = []

    class OrderedChannel(FakeChannel):
        async def prepare(self):
            order.append("prepare")

        async def answer(self):
            order.append("answer")
            await super().answer()

        async def listen(self, **kwargs):
            order.append("listen")
            return await super().listen(**kwargs)

        async def say(self, text):
            order.append("say")
            return await super().say(text)

    channel = OrderedChannel([EXAMPLE, HANGUP])
    await make_loop(container, channel).run()
    assert order[:3] == ["prepare", "answer", "listen"]


async def test_prepare_failure_never_answers(container):
    class BrokenChannel(FakeChannel):
        async def prepare(self):
            raise RuntimeError("audio path unavailable")

    channel = BrokenChannel([EXAMPLE])
    assert await make_loop(container, channel).run() == "technical_failure"
    assert not channel.answered
    assert channel.hung_up_by_agent
