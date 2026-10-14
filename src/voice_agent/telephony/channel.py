"""Call channel abstraction used by the voice loop.

``CallChannel`` hides telephony details (answer, speak, listen, hang up) so the
voice loop can be tested with a fake channel and reused with other telephony
backends later (e.g. a SIP media server or a WebRTC gateway).
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any

from voice_agent.logging_setup import get_logger
from voice_agent.telephony.ari_client import AriClient, AriError, AriEventBus
from voice_agent.telephony.media import SpeechRenderer

log = get_logger(__name__)

HANGUP_EVENTS = {"StasisEnd", "ChannelDestroyed", "ChannelHangupRequest"}


class CallHungUp(Exception):
    """The caller (or the network) ended the call."""


class CallChannel(ABC):
    call_id: str

    @property
    @abstractmethod
    def active(self) -> bool: ...

    @abstractmethod
    async def answer(self) -> None: ...

    async def prepare(self) -> None:
        """Check per-call prerequisites while the phone is still ringing."""
        return None

    async def working(self) -> None:
        """Give a brief receipt cue while the utterance is being understood."""
        return None

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
        ready_cue_media: str | None = None,
        working_cue_media: str | None = None,
    ):
        self.ari = ari
        self.bus = bus
        self.channel_id: str = channel["id"]
        self.call_id = self.channel_id
        self.caller = (channel.get("caller") or {}).get("number", "")
        self.speech = speech
        self.ready_cue_media = ready_cue_media
        self.working_cue_media = working_cue_media
        self._working_playback_id: str | None = None
        self._working_future: asyncio.Future | None = None
        self._barge_in_enabled = False
        self._talk_started = asyncio.Event()
        self._talk_finished = asyncio.Event()
        self._talking = False
        self._pending_audio: bytes | None = None
        self._hangup = asyncio.Event()
        self._turn = 0
        bus.on_channel(self.channel_id, self._on_channel_event)

    def _on_channel_event(self, event: dict[str, Any]) -> None:
        if event.get("type") in HANGUP_EVENTS:
            self._hangup.set()
        elif event.get("type") == "ChannelTalkingStarted":
            self._talking = True
            self._talk_started.set()
            self._talk_finished.clear()
        elif event.get("type") == "ChannelTalkingFinished":
            self._talking = False
            self._talk_finished.set()

    @property
    def active(self) -> bool:
        return not self._hangup.is_set()

    async def answer(self) -> None:
        await self.ari.answer(self.channel_id)

    async def prepare(self) -> None:
        if not self.active:
            raise CallHungUp
        # ARI recording requires an answered channel. Check its presence first,
        # then make the answer -> record transition without intervening playback.
        await self.ari.get_channel(self.channel_id)
        try:
            await self.ari.set_channel_var(self.channel_id, "TALK_DETECT(set)", "1500")
            self._barge_in_enabled = True
        except AriError as exc:
            log.warning("call.talk_detect_unavailable", call_id=self.call_id, error=str(exc))

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

    async def working(self) -> None:
        if not self.active or not self.working_cue_media:
            return
        await self._stop_working()
        playback_id = uuid.uuid4().hex
        self._working_playback_id = playback_id
        self._working_future = self.bus.expect("playback", playback_id)
        try:
            await self.ari.play(self.channel_id, self.working_cue_media, playback_id)
            log.info("call.working_cue_started", call_id=self.call_id)
        except AriError as exc:
            self.bus.discard("playback", playback_id)
            self._working_playback_id = None
            self._working_future = None
            log.warning("call.working_cue_failed", call_id=self.call_id, error=str(exc))

    async def _stop_working(self) -> None:
        playback_id = self._working_playback_id
        future = self._working_future
        if playback_id is None:
            return
        self._working_playback_id = None
        self._working_future = None
        if future is not None and not future.done():
            try:
                await self.ari.stop_playback(playback_id)
            except AriError as exc:
                log.warning("call.working_cue_stop_failed", call_id=self.call_id, error=str(exc))
        self.bus.discard("playback", playback_id)

    async def say(self, text: str) -> bool:
        if not self.active:
            raise CallHungUp
        ok = True
        started_at = time.monotonic()
        try:
            media = await self.speech.render(text)
        except Exception as exc:
            log.error("call.tts_failed", call_id=self.call_id, error=repr(exc))
            return False
        await self._stop_working()
        playback_id = uuid.uuid4().hex
        future = self.bus.expect("playback", playback_id)
        capture_name: str | None = None
        capture_future: asyncio.Future | None = None
        if self._barge_in_enabled:
            self._talk_started.clear()
            self._talk_finished.clear()
            capture_name = f"vm-barge-{uuid.uuid4().hex}"
            capture_future = self.bus.expect("recording", capture_name)
            try:
                await self.ari.record(
                    self.channel_id,
                    capture_name,
                    fmt="wav",
                    max_duration_seconds=60,
                    max_silence_seconds=0,
                )
            except AriError as exc:
                self.bus.discard("recording", capture_name)
                capture_name = None
                capture_future = None
                log.warning("call.barge_capture_unavailable", call_id=self.call_id, error=str(exc))
        try:
            await self.ari.play(self.channel_id, media, playback_id)
            log.info(
                "call.playback_started",
                call_id=self.call_id,
                render_and_start_ms=round((time.monotonic() - started_at) * 1000),
            )
            # Generous upper bound: ~0.15 s per character of Arabic speech + margin.
            timeout = 10 + len(text) * 0.15
            if capture_name:
                finished, interrupted = await self._wait_for_talk_or_playback(future, timeout)
                if interrupted:
                    await self.ari.stop_playback(playback_id)
                    log.info("call.playback_interrupted", call_id=self.call_id)
                    if self._talking:
                        await self._wait_for_talk_end(20.0)
                self._pending_audio = await self._finish_barge_capture(
                    capture_name, capture_future, keep=interrupted
                )
                capture_name = None
            else:
                finished = await self._wait(future, wait_seconds=timeout)
        except AriError as exc:
            if not self.active or exc.status == 404:
                raise CallHungUp from exc
            raise
        finally:
            if capture_name:
                await self._finish_barge_capture(capture_name, capture_future, keep=False)
            self.bus.discard("playback", playback_id)
        if not finished and not self.active:
            raise CallHungUp
        return ok

    async def _wait_for_talk_or_playback(
        self, playback: asyncio.Future, wait_seconds: float
    ) -> tuple[bool, bool]:
        talk = asyncio.create_task(self._talk_started.wait())
        hangup = asyncio.create_task(self._hangup.wait())
        try:
            done, _ = await asyncio.wait(
                {playback, talk, hangup},
                timeout=wait_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            return playback in done, talk in done
        finally:
            talk.cancel()
            hangup.cancel()

    async def _wait_for_talk_end(self, wait_seconds: float) -> None:
        ended = asyncio.create_task(self._talk_finished.wait())
        hangup = asyncio.create_task(self._hangup.wait())
        try:
            await asyncio.wait(
                {ended, hangup}, timeout=wait_seconds, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            ended.cancel()
            hangup.cancel()

    async def _finish_barge_capture(
        self, name: str, future: asyncio.Future | None, *, keep: bool
    ) -> bytes | None:
        try:
            await self.ari.stop_recording(name)
            if future and not future.done():
                await self._wait_plain(future, 2.0)
            if not keep:
                return None
            audio = await self.ari.get_stored_recording(name)
            return audio if len(audio) > 1000 else None
        except AriError as exc:
            log.warning("call.barge_capture_failed", call_id=self.call_id, error=str(exc))
            return None
        finally:
            self.bus.discard("recording", name)
            await self._safe_delete(name)

    async def listen(self, *, max_seconds: int, max_silence_seconds: int) -> bytes | None:
        if not self.active:
            raise CallHungUp
        if self._pending_audio is not None:
            audio, self._pending_audio = self._pending_audio, None
            return audio
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
            log.info("call.recording_started", call_id=self.call_id, turn=self._turn)
            if self._turn == 1 and self.ready_cue_media and self.active:
                try:
                    await self.ari.play(self.channel_id, self.ready_cue_media, uuid.uuid4().hex)
                    log.info("call.ready_cue_started", call_id=self.call_id)
                except AriError as exc:
                    log.warning("call.ready_cue_failed", call_id=self.call_id, error=str(exc))
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
        if self._working_playback_id:
            self.bus.discard("playback", self._working_playback_id)
        self.bus.remove_channel(self.channel_id)
