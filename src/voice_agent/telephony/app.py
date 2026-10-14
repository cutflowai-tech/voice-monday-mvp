"""ARI application: accepts calls routed to the Stasis app and runs a voice loop per call."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from voice_agent.logging_setup import get_logger
from voice_agent.telephony.ari_client import AriClient, AriEventBus
from voice_agent.telephony.channel import AriCallChannel
from voice_agent.telephony.media import SpeechRenderer
from voice_agent.telephony.voice_loop import VoiceLoop

log = get_logger(__name__)

LoopFactory = Callable[[AriCallChannel], VoiceLoop]


class AriApplication:
    def __init__(
        self,
        *,
        ari: AriClient,
        speech: SpeechRenderer,
        loop_factory: LoopFactory,
        shutdown_grace_seconds: float = 10.0,
    ):
        self.ari = ari
        self.bus = AriEventBus()
        self.speech = speech
        self.loop_factory = loop_factory
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self.sessions: dict[str, asyncio.Task] = {}
        self._draining = False
        self.ready_cue_media: str | None = None

    async def run(self, stop: asyncio.Event) -> None:
        info = await self.ari.info()
        try:
            self.ready_cue_media = await self.speech.render("معاك")
        except Exception as exc:
            log.warning("ari.ready_cue_unavailable", error=repr(exc))
        log.info(
            "ari.ready",
            app=self.ari.app,
            asterisk_version=(info.get("system") or {}).get("version"),
        )
        consumer = asyncio.create_task(self._consume(), name="ari-events")
        await stop.wait()
        await self.shutdown(consumer)

    async def _consume(self) -> None:
        async for event in self.ari.events():
            try:
                self.handle_event(event)
            except Exception:
                log.exception("ari.event_error", type=event.get("type"))

    def handle_event(self, event: dict[str, Any]) -> None:
        self.bus.dispatch(event)
        if event.get("type") != "StasisStart":
            return
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        if not channel_id or channel_id in self.sessions:
            return
        if self._draining:
            log.warning("ari.rejecting_call_during_shutdown", channel_id=channel_id)
            asyncio.create_task(self.ari.hangup(channel_id, reason="congestion"))  # noqa: RUF006
            return
        log.info(
            "ari.incoming_call",
            channel_id=channel_id,
            caller=(channel.get("caller") or {}).get("number"),
            exten=(channel.get("dialplan") or {}).get("exten"),
        )
        call = AriCallChannel(
            ari=self.ari,
            bus=self.bus,
            channel=channel,
            speech=self.speech,
            ready_cue_media=self.ready_cue_media,
        )
        task = asyncio.create_task(self._run_call(call), name=f"call-{channel_id}")
        self.sessions[channel_id] = task

    async def _run_call(self, call: AriCallChannel) -> None:
        try:
            await self.loop_factory(call).run()
        except asyncio.CancelledError:
            await asyncio.shield(call.hangup())
            raise
        except Exception:
            log.exception("call.crashed", call_id=call.call_id)
            await call.hangup()
        finally:
            call.close()
            self.sessions.pop(call.channel_id, None)

    async def shutdown(self, consumer: asyncio.Task) -> None:
        """Stop taking calls, let active calls finish briefly, then hang them up.

        Accepted jobs are already durable, so cutting a call never loses work.
        """
        self._draining = True
        active = list(self.sessions.values())
        log.info("ari.shutting_down", active_calls=len(active))
        if active:
            _, pending = await asyncio.wait(active, timeout=self.shutdown_grace_seconds)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
