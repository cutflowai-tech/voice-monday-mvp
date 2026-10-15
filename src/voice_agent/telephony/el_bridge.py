"""Bridge Asterisk AudioSocket calls to the ElevenLabs Agent over a WebSocket.

Why: behind the office's multi-line load balancer, SIP/RTP to ElevenLabs loses the return
audio (inbound UDP has no stable way back). Here every packet is either local TCP
(Asterisk -> 127.0.0.1) or an outbound TLS WebSocket, so NAT does not matter.

AudioSocket (docs.asterisk.org, Channel-Drivers/AudioSocket): 3-byte header
``type(1) | length(2, big-endian)`` + payload. 0x00 hangup, 0x01 16-byte UUID, 0x03 DTMF,
0x10..0x18 signed-linear 16-bit little-endian mono at 8/12/16/24/32/44.1/48/96/192 kHz,
0xff error. We answer in the same audio type Asterisk sends.

ElevenLabs Agents WebSocket (elevenlabs.io/docs, Agents WebSocket API):
``GET /v1/convai/conversation/get-signed-url?agent_id=`` -> ``signed_url``; client sends
``conversation_initiation_client_data`` (with ``dynamic_variables``), ``{"user_audio_chunk":
<base64 pcm>}`` and ``{"type":"pong","event_id":..}``; server sends
``conversation_initiation_metadata`` (audio formats, conversation_id), ``audio``
(``audio_event.audio_base_64``), ``interruption``, ``ping``, ``user_transcript``,
``agent_response``.

The dialplan puts the caller's number in the last 12 hex digits of the AudioSocket UUID
(``${UUID():0:24}`` + 12 zero-padded digits), so tasks keep their requester.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import struct
import time
import uuid
from array import array
from typing import Any

import httpx

from voice_agent.logging_setup import get_logger

log = get_logger(__name__)

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_ERROR = 0xFF
AUDIO_RATES = {
    0x10: 8000,
    0x11: 12000,
    0x12: 16000,
    0x13: 24000,
    0x14: 32000,
    0x15: 44100,
    0x16: 48000,
    0x17: 96000,
    0x18: 192000,
}
FRAME_SECONDS = 0.02


# ------------------------------------------------------------------ audio helpers
def caller_from_uuid(raw: bytes) -> str | None:
    """Last 12 hex digits of the UUID hold the zero-padded caller number (digits only)."""
    tail = uuid.UUID(bytes=raw).hex[-12:]
    if not tail.isdigit():
        return None
    return tail.lstrip("0") or None


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resampler for 16-bit little-endian mono PCM."""
    if src_rate == dst_rate or not pcm:
        return pcm
    src = array("h")
    src.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n_out = max(1, round(len(src) * dst_rate / src_rate))
    step = (len(src) - 1) / (n_out - 1) if n_out > 1 else 0.0
    out = array("h", bytes(2 * n_out))
    last = len(src) - 1
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = src[j]
        b = src[j + 1] if j < last else a
        out[i] = int(a + (b - a) * frac)
    return out.tobytes()


def pcm_rate(fmt: str) -> int:
    """'pcm_16000' -> 16000. Only PCM is supported over AudioSocket."""
    prefix, _, rate = fmt.partition("_")
    if prefix != "pcm" or not rate.isdigit():
        raise ValueError(f"unsupported ElevenLabs audio format {fmt!r} (use pcm_*)")
    return int(rate)


async def read_message(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(3)
    kind, length = header[0], struct.unpack(">H", header[1:3])[0]
    payload = await reader.readexactly(length) if length else b""
    return kind, payload


def frame(kind: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + struct.pack(">H", len(payload)) + payload


# ------------------------------------------------------------------ one call
class BridgeCall:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        open_agent,
    ):
        self.reader = reader
        self.writer = writer
        self.open_agent = open_agent  # async (caller) -> websocket-like
        self.asterisk_kind = 0x10  # answer in whatever rate Asterisk speaks
        self.out_buffer = bytearray()
        self.agent_in_rate = 16000
        self.agent_out_rate = 16000
        self.conversation_id: str | None = None
        self.caller: str | None = None
        self.closed = asyncio.Event()
        self._user_spoke_at: float | None = None
        self._background: set[asyncio.Task] = set()

    @property
    def asterisk_rate(self) -> int:
        return AUDIO_RATES[self.asterisk_kind]

    async def run(self) -> None:
        started = time.monotonic()
        kind, payload = await asyncio.wait_for(read_message(self.reader), 5)
        if kind != KIND_UUID or len(payload) != 16:
            raise ValueError(f"expected UUID first, got type {kind:#x}")
        self.caller = caller_from_uuid(payload)
        ws = await self.open_agent(self.caller)
        try:
            await self._await_metadata(ws)
            log.info(
                "el_bridge.connected",
                caller=self.caller,
                conversation_id=self.conversation_id,
                setup_ms=round((time.monotonic() - started) * 1000),
            )
            tasks = [
                asyncio.create_task(self._asterisk_to_agent(ws)),
                asyncio.create_task(self._agent_to_asterisk(ws)),
                asyncio.create_task(self._pace_playback()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task.exception() and not isinstance(
                    task.exception(), asyncio.IncompleteReadError | ConnectionError
                ):
                    log.warning("el_bridge.task_failed", error=repr(task.exception()))
        finally:
            self.closed.set()
            with contextlib.suppress(Exception):
                await ws.close()
            with contextlib.suppress(Exception):
                self.writer.write(frame(KIND_HANGUP))
                await self.writer.drain()
            self.writer.close()
            log.info(
                "el_bridge.ended",
                caller=self.caller,
                conversation_id=self.conversation_id,
                seconds=round(time.monotonic() - started, 1),
            )

    async def _await_metadata(self, ws) -> None:
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if message.get("type") == "conversation_initiation_metadata":
                event = message.get("conversation_initiation_metadata_event") or {}
                self.conversation_id = event.get("conversation_id")
                self.agent_out_rate = pcm_rate(event.get("agent_output_audio_format", "pcm_16000"))
                self.agent_in_rate = pcm_rate(event.get("user_input_audio_format", "pcm_16000"))
                return
            if message.get("type") == "ping":
                await self._pong(ws, message)

    async def _asterisk_to_agent(self, ws) -> None:
        while True:
            kind, payload = await read_message(self.reader)
            if kind in AUDIO_RATES:
                self.asterisk_kind = kind
                pcm = resample(payload, AUDIO_RATES[kind], self.agent_in_rate)
                await ws.send(json.dumps({"user_audio_chunk": base64.b64encode(pcm).decode()}))
            elif kind in (KIND_HANGUP, KIND_ERROR):
                return
            # DTMF and anything else: ignored

    async def _agent_to_asterisk(self, ws) -> None:
        async for raw in ws:
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "audio":
                pcm = base64.b64decode(message["audio_event"]["audio_base_64"])
                if self._user_spoke_at is not None:
                    log.info(
                        "el_bridge.first_audio",
                        conversation_id=self.conversation_id,
                        after_user_ms=round((time.monotonic() - self._user_spoke_at) * 1000),
                    )
                    self._user_spoke_at = None
                self.out_buffer += resample(pcm, self.agent_out_rate, self.asterisk_rate)
            elif kind == "interruption":
                self.out_buffer.clear()  # caller talked over her: stop speaking at once
            elif kind == "ping":
                # answer in the background: ping_ms may ask us to wait, audio must not
                pong = asyncio.create_task(self._pong(ws, message))
                self._background.add(pong)
                pong.add_done_callback(self._background.discard)
            elif kind == "user_transcript":
                text = (message.get("user_transcription_event") or {}).get("user_transcript")
                self._user_spoke_at = time.monotonic()
                log.info("el_bridge.heard", conversation_id=self.conversation_id, text=text)
            elif kind == "agent_response":
                text = (message.get("agent_response_event") or {}).get("agent_response")
                log.info("el_bridge.said", conversation_id=self.conversation_id, text=text)

    async def _pace_playback(self) -> None:
        """Send her audio in real-time 20 ms frames so Asterisk never over/under-runs."""
        next_at = time.monotonic()
        while True:
            size = int(self.asterisk_rate * FRAME_SECONDS) * 2
            if len(self.out_buffer) >= size:
                chunk = bytes(self.out_buffer[:size])
                del self.out_buffer[:size]
                self.writer.write(frame(self.asterisk_kind, chunk))
                await self.writer.drain()
            next_at += FRAME_SECONDS
            delay = next_at - time.monotonic()
            if delay < -0.2:  # fell far behind (e.g. a stall): resynchronise
                next_at = time.monotonic()
                delay = 0
            await asyncio.sleep(max(0.0, delay))

    @staticmethod
    async def _pong(ws, message: dict[str, Any]) -> None:
        event = message.get("ping_event") or {}
        delay = (event.get("ping_ms") or 0) / 1000
        if delay:
            await asyncio.sleep(min(delay, 1.0))
        await ws.send(json.dumps({"type": "pong", "event_id": event.get("event_id")}))


# ------------------------------------------------------------------ server
class ElevenLabsBridge:
    def __init__(self, *, api_key: str, agent_id: str, base_url: str, host: str, port: int):
        self.api_key = api_key
        self.agent_id = agent_id
        self.base_url = base_url.rstrip("/")
        self.host = host
        self.port = port
        self.calls: set[asyncio.Task] = set()

    async def signed_url(self) -> str:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
            response = await client.get(
                "/v1/convai/conversation/get-signed-url",
                params={"agent_id": self.agent_id},
                headers={"xi-api-key": self.api_key},
            )
            response.raise_for_status()
            return response.json()["signed_url"]

    async def open_agent(self, caller: str | None):
        from websockets.asyncio.client import connect

        ws = await connect(await self.signed_url(), max_size=2**22)
        await ws.send(
            json.dumps(
                {
                    "type": "conversation_initiation_client_data",
                    "dynamic_variables": {"caller_id": caller or "unknown"},
                }
            )
        )
        return ws

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self.calls.add(task)
        try:
            await BridgeCall(reader, writer, open_agent=self.open_agent).run()
        except Exception as exc:
            log.warning("el_bridge.call_failed", error=repr(exc))
            with contextlib.suppress(Exception):
                writer.write(frame(KIND_HANGUP))
                await writer.drain()
                writer.close()
        finally:
            if task:
                self.calls.discard(task)

    async def serve(self, stop: asyncio.Event) -> None:
        server = await asyncio.start_server(self.handle, self.host, self.port)
        log.info("el_bridge.listening", host=self.host, port=self.port, agent_id=self.agent_id)
        async with server:
            await stop.wait()
        if self.calls:
            await asyncio.wait(self.calls, timeout=10)
