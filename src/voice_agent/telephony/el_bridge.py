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
import math
import struct
import time
import uuid
from array import array
from typing import Any

import httpx

from voice_agent.agent_tools.activity import is_speech
from voice_agent.logging_setup import get_logger
from voice_agent.sarah import (
    UNKNOWN_CALLER,
    caller_gender,
    opening_for,
    prank_opening,
    style_for_call,
)

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
# Ringback while the agent connects (Egypt: 425 Hz, 2 s on / 1 s off), so the caller hears
# ringing instead of dead air and the agent "picks up" with her first words.
RINGBACK_HZ = 425
RINGBACK_ON_S = 2.0
RINGBACK_PERIOD_S = 3.0
RINGBACK_AMPLITUDE = 4000
SIGNED_URL_MAX_AGE_S = 60.0
OUTBOUND_CONTEXT = "voice-monday-el-outbound"  # dialplan: AudioSocket(${VM_UUID},...)
OUTBOUND_RING_SECONDS = 25
OUTBOUND_MAX_CALL_SECONDS = 90
# Keyboard typing played while a task is being saved (louder than ElevenLabs' own
# tool sound, which has no volume setting). Ducked under Sarah's voice when she talks.
TYPING_TARGET_RMS = 2200  # at volume 1.0; phone speech is roughly 3000-5000
TYPING_UNDER_VOICE = 0.35
_TYPING: dict[int, array] = {}


def typing_loop(rate: int) -> array:
    """The packaged 16 kHz typing loop, resampled to ``rate`` and normalised (cached)."""
    if rate not in _TYPING:
        from importlib import resources

        raw = resources.files("voice_agent").joinpath("assets/typing_16k.pcm").read_bytes()
        pcm = resample(raw, 16000, rate)
        samples = array("h")
        samples.frombytes(pcm)
        rms = math.sqrt(sum(v * v for v in samples) / max(1, len(samples))) or 1.0
        gain = TYPING_TARGET_RMS / rms
        _TYPING[rate] = array("h", (max(-32768, min(32767, int(v * gain))) for v in samples))
    return _TYPING[rate]


def mix(voice: bytes, effect: array, start: int, effect_gain: float) -> tuple[bytes, int]:
    """Add the looping effect under ``voice`` (16-bit PCM); returns frame and next index."""
    out = array("h")
    out.frombytes(voice)
    n = len(effect)
    for i in range(len(out)):
        v = out[i] + int(effect[(start + i) % n] * effect_gain)
        out[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    return out.tobytes(), (start + len(out)) % n


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


def ringback_frame(rate: int, start_sample: int) -> bytes:
    """One 20 ms frame of the ringback cadence starting at ``start_sample``."""
    n = int(rate * FRAME_SECONDS)
    out = array("h", bytes(2 * n))
    for i in range(n):
        t = (start_sample + i) / rate
        if t % RINGBACK_PERIOD_S < RINGBACK_ON_S:
            out[i] = int(RINGBACK_AMPLITUDE * math.sin(2 * math.pi * RINGBACK_HZ * t))
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
        on_speech=None,
        is_saving=None,
        typing_volume: float = 1.0,
        limit_for=None,
    ):
        self.limit_for = limit_for  # (uuid bytes) -> max seconds for this call, or None
        self.call_uuid: bytes | None = None
        self.is_saving = is_saving  # async (conversation_id) -> bool, from the task window
        self.typing_volume = typing_volume
        self.saving = False
        self._typing_pos = 0
        self.reader = reader
        self.on_speech = on_speech  # async (conversation_id) -> None, e.g. SpeechActivity.mark
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
        self.connecting = True  # ringback until the agent's conversation has started
        self._ring_sample = 0

    @property
    def asterisk_rate(self) -> int:
        return AUDIO_RATES[self.asterisk_kind]

    async def run(self) -> None:
        started = time.monotonic()
        kind, payload = await asyncio.wait_for(read_message(self.reader), 5)
        if kind != KIND_UUID or len(payload) != 16:
            raise ValueError(f"expected UUID first, got type {kind:#x}")
        self.call_uuid = payload
        self.caller = caller_from_uuid(payload)
        limit = self.limit_for(payload) if self.limit_for else None
        # AudioSocket hangs up after 2 s without data from us: stream (silence) from now on.
        pacer = asyncio.create_task(self._pace_playback())
        ws = None
        try:
            ws = await self.open_agent(self.caller, payload)
            await self._await_metadata(ws)
            self.connecting = False
            log.info(
                "el_bridge.connected",
                caller=self.caller,
                conversation_id=self.conversation_id,
                setup_ms=round((time.monotonic() - started) * 1000),
            )
            tasks = [
                asyncio.create_task(self._asterisk_to_agent(ws)),
                asyncio.create_task(self._agent_to_asterisk(ws)),
                pacer,
            ]
            if self.is_saving and self.typing_volume > 0:
                tasks.append(asyncio.create_task(self._watch_saving()))
            done, pending = await asyncio.wait(
                tasks, timeout=limit, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                log.info("el_bridge.call_limit_reached", seconds=limit)
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
            if not pacer.done():
                pacer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pacer
            if ws is not None:
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
        t0 = time.monotonic()
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if message.get("type") == "conversation_initiation_metadata":
                event = message.get("conversation_initiation_metadata_event") or {}
                self.conversation_id = event.get("conversation_id")
                self.agent_out_rate = pcm_rate(event.get("agent_output_audio_format", "pcm_16000"))
                self.agent_in_rate = pcm_rate(event.get("user_input_audio_format", "pcm_16000"))
                log.info("el_bridge.metadata", wait_ms=round((time.monotonic() - t0) * 1000))
                return
            if message.get("type") == "ping":
                await self._pong(ws, message)

    async def _asterisk_to_agent(self, ws) -> None:
        while True:
            kind, payload = await read_message(self.reader)
            if kind in AUDIO_RATES:
                self.asterisk_kind = kind
                if self.on_speech and self.conversation_id and is_speech(payload):
                    mark = asyncio.create_task(self.on_speech(self.conversation_id))
                    self._background.add(mark)
                    mark.add_done_callback(self._background.discard)
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
            if self.connecting:
                chunk = ringback_frame(self.asterisk_rate, self._ring_sample)
                self._ring_sample += size // 2
            elif len(self.out_buffer) >= size:
                chunk = bytes(self.out_buffer[:size])
                del self.out_buffer[:size]
                if self.saving:
                    chunk, self._typing_pos = mix(
                        chunk,
                        typing_loop(self.asterisk_rate),
                        self._typing_pos,
                        self.typing_volume * TYPING_UNDER_VOICE,
                    )
            elif self.saving:
                chunk, self._typing_pos = mix(
                    bytes(size),
                    typing_loop(self.asterisk_rate),
                    self._typing_pos,
                    self.typing_volume,
                )
            else:
                chunk = bytes(size)  # silence keeps the AudioSocket alive between words
            self.writer.write(frame(self.asterisk_kind, chunk))
            await self.writer.drain()
            next_at += FRAME_SECONDS
            delay = next_at - time.monotonic()
            if delay < -0.2:  # fell far behind (e.g. a stall): resynchronise
                next_at = time.monotonic()
                delay = 0
            await asyncio.sleep(max(0.0, delay))

    async def _watch_saving(self) -> None:
        """Follow the task window's "saving now" flag for this conversation."""
        while True:
            if self.conversation_id:
                self.saving = await self.is_saving(self.conversation_id)
            await asyncio.sleep(0.15)

    @staticmethod
    async def _pong(ws, message: dict[str, Any]) -> None:
        event = message.get("ping_event") or {}
        delay = (event.get("ping_ms") or 0) / 1000
        if delay:
            await asyncio.sleep(min(delay, 1.0))
        # the call may have ended meanwhile; a late pong on a closed socket is harmless
        with contextlib.suppress(Exception):
            await ws.send(json.dumps({"type": "pong", "event_id": event.get("event_id")}))


# ------------------------------------------------------------------ server
class ElevenLabsBridge:
    def __init__(
        self,
        *,
        api_key: str,
        agent_id: str,
        base_url: str,
        host: str,
        port: int,
        activity=None,
        identify=None,
        typing_volume: float = 1.0,
        outbound=None,
        ari=None,
        people=None,
        outbound_caller_id: str = "Sarah <777>",
    ):
        self.typing_volume = typing_volume
        self.outbound = outbound  # OutboundCalls: queued call requests from the API
        self.ari = ari  # AriClient on loopback, used to place those calls
        self.people = people
        self.outbound_caller_id = outbound_caller_id
        self.pending_outbound: dict[bytes, tuple[float, Any]] = {}
        self.activity = activity  # SpeechActivity: lets the task window wait for the caller
        self.identify = identify  # (caller number) -> Person | None, for Sarah's greeting
        self.api_key = api_key
        self.agent_id = agent_id
        self.base_url = base_url.rstrip("/")
        self.host = host
        self.port = port
        self.calls: set[asyncio.Task] = set()
        # kept-alive connection + one signed URL fetched ahead of the next call
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10)
        self._ready_url: tuple[float, str] | None = None
        self._prefetch: asyncio.Task | None = None

    async def _fetch_signed_url(self) -> str:
        response = await self._client.get(
            "/v1/convai/conversation/get-signed-url",
            params={"agent_id": self.agent_id},
            headers={"xi-api-key": self.api_key},
        )
        response.raise_for_status()
        return response.json()["signed_url"]

    async def _refill(self) -> None:
        try:
            self._ready_url = (time.monotonic(), await self._fetch_signed_url())
        except Exception as exc:
            log.warning("el_bridge.prefetch_failed", error=repr(exc))

    def _schedule_refill(self) -> None:
        if self._prefetch is None or self._prefetch.done():
            self._prefetch = asyncio.create_task(self._refill())

    async def signed_url(self) -> str:
        """A prefetched URL when fresh (one per call), else a new one; always refill."""
        ready, self._ready_url = self._ready_url, None
        self._schedule_refill()
        if ready and time.monotonic() - ready[0] < SIGNED_URL_MAX_AGE_S:
            return ready[1]
        return await self._fetch_signed_url()

    async def keep_warm(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self._ready_url is None or (
                time.monotonic() - self._ready_url[0] > SIGNED_URL_MAX_AGE_S / 2
            ):
                await self._refill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 10)

    async def open_agent(self, caller: str | None, call_uuid: bytes | None = None):
        from websockets.asyncio.client import connect

        t0 = time.monotonic()
        url = await self.signed_url()
        t1 = time.monotonic()
        ws = await connect(url, max_size=2**22)
        log.info(
            "el_bridge.opening",
            signed_url_ms=round((t1 - t0) * 1000),
            connect_ms=round((time.monotonic() - t1) * 1000),
        )
        await ws.send(json.dumps(self.initiation(caller, call_uuid)))
        return ws

    def initiation(self, caller: str | None, call_uuid: bytes | None = None) -> dict[str, Any]:
        """Who Sarah is talking to and why, plus a fresh greeting for this call."""
        outbound = self.pending_outbound.pop(call_uuid, None) if call_uuid else None
        if outbound is not None and self.people is not None:
            request = outbound[1]
            target = self.people.get(request.target_key)
            requester = self.people.get(request.requested_by) if request.requested_by else None
            if target is not None:
                log.info("el_bridge.outbound_answered", target=target.key, purpose=request.purpose)
                return {
                    "type": "conversation_initiation_client_data",
                    "dynamic_variables": {
                        "call_mode": request.purpose,
                        "caller_id": request.extension,
                        "caller_name": target.name_ar,
                        "caller_gender": caller_gender(target),
                        "prank_by": requester.name_ar if requester else "حد من الفريق",
                        "opening": prank_opening(target),
                        **style_for_call(),
                    },
                }
        person = self.identify(caller) if (self.identify and caller) else None
        return {
            "type": "conversation_initiation_client_data",
            "dynamic_variables": {
                "call_mode": "inbound",
                "caller_id": caller or UNKNOWN_CALLER,
                "caller_name": person.name_ar if person else UNKNOWN_CALLER,
                "caller_gender": caller_gender(person),
                "prank_by": UNKNOWN_CALLER,
                "opening": opening_for(person),
                **style_for_call(),
            },
        }

    def limit_for(self, call_uuid: bytes) -> float | None:
        return OUTBOUND_MAX_CALL_SECONDS if call_uuid in self.pending_outbound else None

    async def place_call(self, request) -> None:
        """Ring the extension; when answered the dialplan opens an AudioSocket to us with
        this UUID, and ``initiation`` recognises it as an outbound call."""
        call_uuid = uuid.uuid4()
        now = time.monotonic()
        self.pending_outbound = {k: v for k, v in self.pending_outbound.items() if now - v[0] < 120}
        self.pending_outbound[call_uuid.bytes] = (now, request)
        try:
            await self.ari.originate(
                endpoint=f"PJSIP/{request.extension}",
                context=OUTBOUND_CONTEXT,
                extension="s",
                caller_id=self.outbound_caller_id,
                timeout_seconds=OUTBOUND_RING_SECONDS,
                variables={"VM_UUID": str(call_uuid)},
            )
            log.info("el_bridge.outbound_ringing", target=request.target_key)
        except Exception as exc:
            self.pending_outbound.pop(call_uuid.bytes, None)
            log.warning("el_bridge.outbound_failed", target=request.target_key, error=repr(exc))

    async def dial_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                request = await self.outbound.next_request(wait_seconds=2)
            except Exception as exc:
                log.warning("el_bridge.outbound_queue_error", error=repr(exc))
                await asyncio.sleep(2)
                continue
            if request is not None:
                await self.place_call(request)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self.calls.add(task)
        try:
            await BridgeCall(
                reader,
                writer,
                open_agent=self.open_agent,
                limit_for=self.limit_for,
                on_speech=self.activity.mark if self.activity else None,
                is_saving=self.activity.is_saving if self.activity else None,
                typing_volume=self.typing_volume,
            ).run()
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
        background = [asyncio.create_task(self.keep_warm(stop))]
        if self.outbound is not None and self.ari is not None:
            background.append(asyncio.create_task(self.dial_loop(stop)))
            log.info("el_bridge.outbound_enabled")
        async with server:
            await stop.wait()
        if self.calls:
            await asyncio.wait(self.calls, timeout=10)
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await self._client.aclose()
