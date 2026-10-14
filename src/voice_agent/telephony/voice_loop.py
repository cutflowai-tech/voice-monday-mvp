"""The per-call voice loop: listen -> transcribe -> engine -> speak -> repeat.

The loop only ends when the caller hangs up, asks to end the call, stays silent
past the inactivity timeout, or repeated technical failures make the session
unusable. Accepting a command never ends the call.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from voice_agent.ack.base import AcknowledgmentGenerator
from voice_agent.conversation.engine import ConversationEngine, ConversationState
from voice_agent.logging_setup import get_logger
from voice_agent.speech.stt import SpeechToTextProvider
from voice_agent.telephony.channel import CallChannel, CallHungUp

log = get_logger(__name__)

EndReason = Literal["caller_hangup", "caller_requested", "inactivity_timeout", "technical_failure"]


@dataclass(frozen=True)
class VoiceLoopConfig:
    language: str = "ar"
    inactivity_timeout_seconds: float = 60.0
    record_max_seconds: int = 20
    record_max_silence_seconds: int = 2
    max_consecutive_failures: int = 3
    preparation_timeout_seconds: float = 8.0


class VoiceLoop:
    def __init__(
        self,
        *,
        channel: CallChannel,
        stt: SpeechToTextProvider,
        engine: ConversationEngine,
        acks: AcknowledgmentGenerator,
        config: VoiceLoopConfig,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.channel = channel
        self.stt = stt
        self.engine = engine
        self.acks = acks
        self.config = config
        self.clock = clock
        self.state = ConversationState(call_id=channel.call_id)
        self.failures = 0
        self.end_reason: EndReason | None = None
        self.reject_unknown_caller = False
        self._working_cue_sent = False

    async def run(self) -> EndReason:
        started = self.clock()
        try:
            self.end_reason = await self._run()
        except CallHungUp:
            self.end_reason = "caller_hangup"
        log.info(
            "call.ended",
            call_id=self.channel.call_id,
            session_id=self.state.session_id,
            reason=self.end_reason,
            turns=self.state.turn,
            accepted_jobs=self.state.accepted_job_ids,
            seconds=round(self.clock() - started, 1),
        )
        return self.end_reason

    async def _run(self) -> EndReason:
        import asyncio

        preparing_at = time.monotonic()
        if self.reject_unknown_caller:
            await self.channel.hangup()
            return "technical_failure"
        try:
            await asyncio.wait_for(self.channel.prepare(), self.config.preparation_timeout_seconds)
        except CallHungUp:
            return "caller_hangup"
        except Exception as exc:
            self._failed("prepare", exc)
            await self.channel.hangup()
            return "technical_failure"
        await self.channel.answer()
        log.info(
            "call.ready",
            call_id=self.channel.call_id,
            preparation_ms=round((time.monotonic() - preparing_at) * 1000),
        )
        log.info("call.answered", call_id=self.channel.call_id, session_id=self.state.session_id)
        last_activity = self.clock()

        while True:
            if not self.channel.active:
                return "caller_hangup"
            if self.failures >= self.config.max_consecutive_failures:
                return await self._end("technical_failure", "technical")
            if self.clock() - last_activity >= self.config.inactivity_timeout_seconds:
                return await self._end("inactivity_timeout", "timeout")

            try:
                audio = await self.channel.listen(
                    max_seconds=self.config.record_max_seconds,
                    max_silence_seconds=self.config.record_max_silence_seconds,
                )
            except CallHungUp:
                return "caller_hangup"
            except Exception as exc:
                self._failed("listen", exc)
                continue
            if audio is None:
                continue  # silence; inactivity timer keeps running
            utterance_ended_at = time.monotonic()

            try:
                transcript = await self.stt.transcribe(audio, language=self.config.language)
            except Exception as exc:
                self._failed("transcribe", exc)
                continue
            text = transcript.text.strip()
            if not text:
                continue
            log.info("call.heard", call_id=self.channel.call_id, text=text)
            if not self._working_cue_sent and len(text.split()) >= 3:
                try:
                    await self.channel.working()
                    self._working_cue_sent = True
                except Exception as exc:
                    log.warning(
                        "call.working_cue_failed", call_id=self.channel.call_id, error=repr(exc)
                    )

            # The engine persists accepted jobs before returning; that work completes
            # even if the caller hangs up while it runs.
            reply = await self.engine.handle(self.state, text)
            self.failures = self.failures + 1 if reply.kind == "error" else 0
            if not self.channel.active:
                return "caller_hangup"
            log.info(
                "call.response_ready",
                call_id=self.channel.call_id,
                turn=self.state.turn,
                latency_ms=round((time.monotonic() - utterance_ended_at) * 1000),
            )
            await self._say(reply.text)
            last_activity = self.clock()
            if reply.end_call:
                await self.channel.hangup()
                return "caller_requested"

    async def _say(self, text: str) -> None:
        if not await self.channel.say(text):
            self.failures += 1

    def _failed(self, stage: str, exc: Exception) -> None:
        self.failures += 1
        log.error(
            "call.stage_failed",
            call_id=self.channel.call_id,
            stage=stage,
            error=repr(exc),
            consecutive=self.failures,
        )

    async def _end(self, reason: EndReason, farewell: str) -> EndReason:
        try:
            await self.channel.say(await self.acks.farewell(farewell))  # type: ignore[arg-type]
        except CallHungUp:
            return "caller_hangup"
        except Exception as exc:
            log.warning("call.farewell_failed", error=repr(exc))
        await self.channel.hangup()
        return reason
