"""Call channel abstraction used by the voice loop.

``CallChannel`` hides telephony details (answer, speak, listen, hang up) so the
voice loop can be tested with a fake channel and reused with other telephony
backends later (e.g. a SIP media server or a WebRTC gateway).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from abc import ABC, abstractmethod
from typing import Any

from voice_agent.logging_setup import get_logger
from voice_agent.telephony.ari_client import AriClient, AriError, AriEventBus
from voice_agent.telephony.media import SpeechRenderer

log = get_logger(__name__)

HANGUP_EVENTS = {"StasisEnd", "ChannelDestroyed", "ChannelHangupRequest"}
FALLBACK_MEDIA = "sound:beep"


class CallHungUp(Exception):
    """The caller (or the network) ended the call."""


class CallChannel(ABC):
    call_id: str

    @property
    @abstractmethod
    def active(self) -> bool: ...

    @abstractmethod
    async def answer(self) -> None: ...

    @abstractmethod
    async def say(self, text: str) -> bool:
        """Speak ``text``. Returns False if speech could not be rendered (fallback played)."""

    @abstractmethod
    async def listen(self, *, max_seconds: int, max_silence_seconds: int) -> bytes | None:
        """Record one utterance. Returns audio, None for silence; raises CallHungUp."""

    @abstractmethod
    async def hangup(self) -> None: ...


class AriCallChannel(CallChannel):
    def __init__(
        self,
        *,
        ari: AriClient,
        bus: AriEventBus,
        channel: dict[str, Any],
        speech: SpeechRenderer,
    ):
        self.ari = ari
        self.bus = bus
        self.channel_id: str = channel["id"]
        self.call_id = self.channel_id
        self.caller = (channel.get("caller") or {}).get("number", "")
        self.speech = speech
        self._hangup = asyncio.Event()
        self._turn = 0
        bus.on_channel(self.channel_id, self._on_channel_event)

    def _on_channel_event(self, event: dict[str, Any]) -> None:
        if event.get("type") in HANGUP_EVENTS:
            self._hangup.set()

    @property
    def active(self) -> bool:
        return not self._hangup.is_set()

    async def answer(self) -> None:
        await self.ari.answer(self.channel_id)

    async def _wait(self, future: asyncio.Future, wait_seconds: float) -> bool:
        """Wait for ``future`` or hangup. Returns True if the future completed."""
        hangup = asyncio.ensure_future(self._hangup.wait())
        try:
            done, _ = await asyncio.wait(
                {future, hangup}, timeout=wait_seconds, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            hangup.cancel()
        return future in done

    async def say(self, text: str) -> bool:
        if not self.active:
            raise CallHungUp
        ok = True
        try:
            media = await self.speech.render(text)
        except Exception as exc:
            log.error("call.tts_failed", call_id=self.call_id, error=repr(exc))
            media, ok = FALLBACK_MEDIA, False
        playback_id = uuid.uuid4().hex
        future = self.bus.expect("playback", playback_id)
        try:
            await self.ari.play(self.channel_id, media, playback_id)
            # Generous upper bound: ~0.15 s per character of Arabic speech + margin.
            finished = await self._wait(future, wait_seconds=10 + len(text) * 0.15)
        except AriError as exc:
            if not self.active or exc.status == 404:
                raise CallHungUp from exc
            raise
        finally:
            self.bus.discard("playback", playback_id)
        if not finished and not self.active:
            raise CallHungUp
        return ok

    async def listen(self, *, max_seconds: int, max_silence_seconds: int) -> bytes | None:
        if not self.active:
            raise CallHungUp
        self._turn += 1
        safe_channel = re.sub(r"[^A-Za-z0-9_-]", "-", self.channel_id)
        name = f"vm-{safe_channel}-{self._turn}-{uuid.uuid4().hex[:6]}"
        future = self.bus.expect("recording", name)
        try:
            await self.ari.record(
                self.channel_id,
                name,
                fmt="wav",
                max_duration_seconds=max_seconds,
                max_silence_seconds=max_silence_seconds,
            )
            finished = await self._wait(future, wait_seconds=max_seconds + 10)
            if not finished and not self.active:
                # Caller hung up mid-sentence: give Asterisk a moment to store the file.
                finished = await self._wait_plain(future, 2.0)
        except AriError as exc:
            self.bus.discard("recording", name)
            if not self.active or exc.status == 404:
                raise CallHungUp from exc
            raise
        if not finished:
            self.bus.discard("recording", name)
            if not self.active:
                return await self._fetch_after_hangup(name)
            await self.ari.stop_recording(name)
            raise TimeoutError("recording did not finish in time")

        event = future.result()
        if event.get("type") == "RecordingFailed":
            raise RuntimeError(f"recording failed: {event.get('recording', {}).get('cause')}")
        recording = event.get("recording") or {}
        talking = recording.get("talking_duration")
        if talking is not None and talking <= 0:
            await self._safe_delete(name)
            return None
        audio = await self.ari.get_stored_recording(name)
        await self._safe_delete(name)
        return audio if len(audio) > 1000 else None

    async def _wait_plain(self, future: asyncio.Future, wait_seconds: float) -> bool:
        try:
            await asyncio.wait_for(asyncio.shield(future), wait_seconds)
            return True
        except (TimeoutError, asyncio.CancelledError):
            return False

    async def _fetch_after_hangup(self, name: str) -> bytes | None:
        try:
            audio = await self.ari.get_stored_recording(name)
        except AriError:
            raise CallHungUp from None
        await self._safe_delete(name)
        return audio if len(audio) > 1000 else None

    async def _safe_delete(self, name: str) -> None:
        try:
            await self.ari.delete_stored_recording(name)
        except AriError as exc:
            log.warning("call.recording_delete_failed", name=name, error=str(exc))

    async def hangup(self) -> None:
        try:
            await self.ari.hangup(self.channel_id)
        except AriError as exc:
            log.warning("call.hangup_failed", call_id=self.call_id, error=str(exc))
        self._hangup.set()

    def close(self) -> None:
        self.bus.remove_channel(self.channel_id)
