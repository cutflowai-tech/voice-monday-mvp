"""AudioSocket <-> ElevenLabs WebSocket bridge (no network)."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid

import pytest

from voice_agent.telephony.el_bridge import (
    KIND_HANGUP,
    KIND_UUID,
    BridgeCall,
    caller_from_uuid,
    frame,
    pcm_rate,
    resample,
)


def test_caller_number_travels_in_the_uuid():
    raw = uuid.UUID("3f2a9c1e-7b4d-4e8a-9c21-000000000101").bytes
    assert caller_from_uuid(raw) == "101"
    mobile = uuid.UUID("3f2a9c1e-7b4d-4e8a-9c21-201001234567").bytes
    assert caller_from_uuid(mobile) == "201001234567"
    assert caller_from_uuid(uuid.UUID("3f2a9c1e-7b4d-4e8a-9c21-00000000abcd").bytes) is None
    assert caller_from_uuid(uuid.UUID("3f2a9c1e-7b4d-4e8a-9c21-000000000000").bytes) is None


def test_resample_and_formats():
    pcm8 = bytes(320)  # 20 ms at 8 kHz
    assert len(resample(pcm8, 8000, 16000)) == 640
    assert len(resample(bytes(640), 16000, 8000)) == 320
    assert resample(pcm8, 8000, 8000) is pcm8
    ramp = b"".join(int(v).to_bytes(2, "little", signed=True) for v in (0, 100, 200, 300))
    up = resample(ramp, 8000, 16000)
    values = [int.from_bytes(up[i : i + 2], "little", signed=True) for i in range(0, len(up), 2)]
    assert values[0] == 0 and values[-1] == 300 and values == sorted(values)
    assert pcm_rate("pcm_16000") == 16000
    with pytest.raises(ValueError):
        pcm_rate("ulaw_8000")


class FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def frames(self) -> list[tuple[int, bytes]]:
        out, i = [], 0
        while i + 3 <= len(self.data):
            kind, length = self.data[i], int.from_bytes(self.data[i + 1 : i + 3], "big")
            out.append((kind, bytes(self.data[i + 3 : i + 3 + length])))
            i += 3 + length
        return out


class FakeAgentSocket:
    """Metadata, a ping, 40 ms of 16 kHz agent audio, an interruption, more audio."""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False
        audio = base64.b64encode(bytes(1280)).decode()  # 40 ms at 16 kHz
        self._script = [
            {"type": "ping", "ping_event": {"event_id": 7}},
            {"type": "user_transcript", "user_transcription_event": {"user_transcript": "ألو"}},
            {"type": "audio", "audio_event": {"audio_base_64": audio, "event_id": 1}},
        ]

    async def recv(self) -> str:
        return json.dumps(
            {
                "type": "conversation_initiation_metadata",
                "conversation_initiation_metadata_event": {
                    "conversation_id": "conv_test",
                    "agent_output_audio_format": "pcm_16000",
                    "user_input_audio_format": "pcm_16000",
                },
            }
        )

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        for message in self._script:
            yield json.dumps(message)
        await asyncio.sleep(0.2)  # let the pacer play the 40 ms out


async def test_call_streams_audio_both_ways_and_hangs_up_cleanly():
    reader = asyncio.StreamReader()
    caller_uuid = uuid.UUID("3f2a9c1e-7b4d-4e8a-9c21-000000000102").bytes
    reader.feed_data(frame(KIND_UUID, caller_uuid))
    reader.feed_data(frame(0x10, bytes(320)) + frame(0x10, bytes(320)))  # 2 x 20 ms caller
    writer = FakeWriter()
    ws = FakeAgentSocket()
    opened_for = []

    async def open_agent(caller):
        opened_for.append(caller)
        return ws

    call = BridgeCall(reader, writer, open_agent=open_agent)
    asyncio.get_running_loop().call_later(0.15, reader.feed_data, frame(KIND_HANGUP))
    await asyncio.wait_for(call.run(), 3)

    assert opened_for == ["102"]
    assert call.conversation_id == "conv_test"
    chunks = [m for m in ws.sent if "user_audio_chunk" in m]
    assert len(chunks) == 2
    assert all(len(base64.b64decode(c["user_audio_chunk"])) == 640 for c in chunks)  # 8k->16k
    assert {"type": "pong", "event_id": 7} in ws.sent

    frames = writer.frames()
    audio = [payload for kind, payload in frames if kind == 0x10]
    assert len(audio) == 2 and all(len(p) == 320 for p in audio)  # 40 ms -> two 20 ms @ 8k
    assert frames[-1] == (KIND_HANGUP, b"")
    assert ws.closed and writer.closed


async def test_interruption_drops_queued_agent_audio():
    call = BridgeCall(asyncio.StreamReader(), FakeWriter(), open_agent=None)
    call.out_buffer += bytes(3200)

    class OneMessage:
        def __aiter__(self):
            async def gen():
                yield json.dumps({"type": "interruption", "interruption_event": {"event_id": 3}})

            return gen()

    await call._agent_to_asterisk(OneMessage())
    assert call.out_buffer == bytearray()


async def test_first_message_must_be_the_uuid():
    reader = asyncio.StreamReader()
    reader.feed_data(frame(0x10, bytes(320)))
    with pytest.raises(ValueError):
        await BridgeCall(reader, FakeWriter(), open_agent=None).run()
