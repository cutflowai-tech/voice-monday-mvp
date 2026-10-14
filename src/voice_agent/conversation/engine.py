"""Conversation engine: one utterance in, one spoken reply out.

Shared by the Asterisk voice loop, the HTTP ``/simulate`` endpoint and the CLI
simulator, so the business flow is identical everywhere. It knows nothing about
telephony or Monday: commands map to job specs, connectors validate payloads.

Order of operations for a command (the important guarantee)::

    parse -> validate -> persist job (SQLite commit) + enqueue -> acknowledgment text

The acknowledgment is only produced after the job is durable, so the caller can
hang up safely once they hear it.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from voice_agent.ack.base import AcknowledgmentGenerator
from voice_agent.commands.base import CommandHandler, CommandRegistry
from voice_agent.connectors.base import ConnectorRegistry
from voice_agent.conversation.session_log import SessionLog
from voice_agent.domain.intents import Action, Intent, IntentDetails, ParseResult
from voice_agent.jobs.models import Job
from voice_agent.jobs.service import JobService, make_idempotency_key
from voice_agent.logging_setup import get_logger
from voice_agent.nlu.base import IntentParser
from voice_agent.nlu.normalize import normalize_text

log = get_logger(__name__)

if TYPE_CHECKING:
    from voice_agent.conversation.agent import StructuredTaskAgent
    from voice_agent.domain.people import PeopleDirectory

MAX_UTTERANCE_CHARS = 1000
MAX_CLARIFICATION_ROUNDS = 2

ReplyKind = Literal[
    "accepted",
    "clarification",
    "unsupported",
    "offer",
    "declined",
    "end_call",
    "small_talk",
    "not_understood",
    "error",
]


class AssistantReply(BaseModel):
    kind: ReplyKind
    text: str
    end_call: bool = False
    intent: Intent | None = None
    job: Job | None = None
    duplicate: bool = False


@dataclass
class Pending:
    kind: Literal["clarify", "offer"]
    intent: Intent
    missing: list[str] = field(default_factory=list)
    rounds: int = 0
    transcripts: list[str] = field(default_factory=list)


@dataclass
class ConversationState:
    """Per-call conversational state. Never stored with, or shared by, jobs."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    call_id: str | None = None
    caller_number: str | None = None
    requester_key: str | None = None
    turn: int = 0
    pending: Pending | None = None
    accepted_job_ids: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    ended: bool = False


class ConversationEngine:
    def __init__(
        self,
        *,
        parser: IntentParser,
        acks: AcknowledgmentGenerator,
        commands: CommandRegistry,
        connectors: ConnectorRegistry,
        jobs: JobService,
        session_log: SessionLog | None = None,
        agent: StructuredTaskAgent | None = None,
        directory: PeopleDirectory | None = None,
    ):
        self.parser = parser
        self.acks = acks
        self.commands = commands
        self.connectors = connectors
        self.jobs = jobs
        self.session_log = session_log
        self.agent = agent
        self.directory = directory

    async def handle(
        self,
        state: ConversationState,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> AssistantReply:
        text = (text or "").strip()[:MAX_UTTERANCE_CHARS]
        state.turn += 1
        state.last_activity_at = time.time()
        if not text:
            return AssistantReply(kind="not_understood", text=await self.acks.not_understood())

        parse = await self.parser.parse(text)
        correction = re.split(r"(?:،|,)?\s*(?:لا\s+قصدي|مش\s+.+?\s+قصدي)\s+", text, maxsplit=1)
        if len(correction) == 2 and correction[0].strip():
            base = await self.parser.parse(correction[0])
            revised = await self.parser.parse_person(correction[1])
            if base.intent and base.intent.action is Action.CREATE_TASK and revised.intent:
                intent = base.intent.model_copy(deep=True)
                intent.assignee = revised.intent.assignee
                intent.details.assignee_key = revised.intent.details.assignee_key
                intent.details.assignee_spoken = revised.intent.details.assignee_spoken
                intent.details.assignee_gender = revised.intent.details.assignee_gender
                parse = ParseResult(status="ok", intent=intent, confidence=base.confidence)
        try:
            unknown_spoken_assignee = (
                parse.intent is not None
                and parse.intent.action is Action.CREATE_TASK
                and parse.intent.assignee is not None
                and parse.intent.details.assignee_key is None
            )
            if (
                self.agent
                and not unknown_spoken_assignee
                and not (parse.status == "ok" and parse.confidence >= 0.99)
            ):
                reply = await self._dispatch_agent(state, text, idempotency_key)
            else:
                reply = await self._dispatch(state, text, parse, idempotency_key)
        except Exception:
            log.exception("conversation.error", session_id=state.session_id, turn=state.turn)
            reply = AssistantReply(kind="error", text=await self.acks.system_error())
        if reply.end_call:
            state.ended = True

        log.info(
            "conversation.turn",
            session_id=state.session_id,
            call_id=state.call_id,
            turn=state.turn,
            parse_status=parse.status,
            action=parse.intent.action.value if parse.intent else None,
            reply_kind=reply.kind,
            job_id=reply.job.id if reply.job else None,
        )
        await self._audit(state, text, parse, reply)
        return reply

    async def _dispatch_agent(
        self, state: ConversationState, text: str, key: str | None
    ) -> AssistantReply:
        normalized = normalize_text(text)
        if normalized in {"استني", "استنى", "لحظه", "لحظة"}:
            return AssistantReply(kind="small_talk", text="حاضر، مستنية.")
        if re.match(r"^\s*(?:لا\s+قصدي|مش\s+.+?\s+قصدي)\b", text) and not state.pending:
            return await self._correct_saved(state, text, key)
        assert self.directory is not None
        try:
            decision = await self.agent.decide(text, state.pending)  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("conversation.agent_fallback", error=repr(exc))
            return await self._dispatch(state, text, await self.parser.parse(text), key)
        if decision.action == "wait":
            return AssistantReply(kind="small_talk", text="حاضر، مستنية.")
        if decision.action == "discard":
            state.pending = None
            return AssistantReply(kind="declined", text="تمام، لغيت الطلب اللي لسه ما اتحفظش.")
        if decision.action == "unsupported":
            return AssistantReply(kind="unsupported", text="أقدر أضيف تاسكات بس.")
        person = self.directory.get(decision.assignee_key) if decision.assignee_key else None
        intent = Intent(
            action=Action.CREATE_TASK,
            assignee=person.name_en if person else None,
            task=decision.task,
            details=IntentDetails(
                assignee_key=person.key if person else None,
                assignee_spoken=person.name_ar if person else None,
                assignee_gender=person.gender if person else None,
            ),
        )
        if decision.action == "clarify" or not (person and decision.task):
            missing = [
                field
                for field, value in (("assignee", person), ("task", decision.task))
                if not value
            ]
            if not missing:
                missing = ["assignee"]  # genuine ambiguity in a named person
                intent.assignee = None
                intent.details.assignee_key = None
            reply = await self._ask(state, intent, missing, None, text)
            if decision.question:
                reply.text = decision.question
            return reply
        return await self._process(state, text, intent, key)

    async def _correct_saved(
        self, state: ConversationState, text: str, key: str | None
    ) -> AssistantReply:
        if not state.accepted_job_ids:
            return AssistantReply(kind="not_understood", text="قولي الطلب كامل من فضلك.")
        original = self.jobs.store.get(state.accepted_job_ids[-1])
        if original is None:
            return AssistantReply(kind="error", text=await self.acks.system_error())
        cancelled = await self.jobs.cancel_queued(original.id)
        if cancelled is None:
            return AssistantReply(
                kind="unsupported",
                text="الطلب بدأ تنفيذه أو اتضاف بالفعل. عدّليه مباشرة في Monday.",
            )
        earlier = {k: v for k, v in original.intent.items() if k != "requester"}
        intent = Intent.model_validate(earlier)
        tail = re.sub(r"^\s*(?:لا\s+قصدي|مش\s+.+?\s+قصدي)\s*", "", text)
        revised = await self.parser.parse_person(tail)
        if revised.intent and revised.intent.details.assignee_key:
            intent.assignee = revised.intent.assignee
            intent.details.assignee_key = revised.intent.details.assignee_key
            intent.details.assignee_spoken = revised.intent.details.assignee_spoken
            intent.details.assignee_gender = revised.intent.details.assignee_gender
            handler = self.commands.get(Action.CREATE_TASK)
            assert handler is not None
            if handler.validate(intent, self.connectors):
                return await self._ask(state, intent, ["assignee"], "unknown_person", text)
            return await self._accept(state, text, intent, key, [original.transcript], handler)
        intent.assignee = None
        intent.details.assignee_key = None
        return await self._ask(state, intent, ["assignee"], None, text)

    # ------------------------------------------------------------------ dispatch
    async def _dispatch(
        self, state: ConversationState, text: str, parse: ParseResult, key: str | None
    ) -> AssistantReply:
        intent = parse.intent
        pending = state.pending

        if normalize_text(text) in {"استني", "استنى", "لحظه", "لحظة"}:
            return AssistantReply(kind="small_talk", text="حاضر، مستنية.")
        if re.match(r"^\s*(?:لا\s+قصدي|مش\s+.+?\s+قصدي)\b", text) and not pending:
            return await self._correct_saved(state, text, key)

        if pending and pending.kind == "offer":
            state.pending = None
            if intent and intent.action is Action.CONFIRM:
                return await self._accept(state, text, pending.intent, key, pending.transcripts)
            if intent and intent.action is Action.DENY:
                return AssistantReply(kind="declined", text=await self.acks.declined())
            # Anything else is treated as a brand-new instruction.

        if pending and pending.kind == "clarify":
            is_new_command = parse.status == "ok" and intent and self.commands.get(intent.action)
            if intent and intent.action in (Action.END_CALL, Action.DENY):
                state.pending = None
            elif not is_new_command:
                merged = await self._merge(pending, text, parse)
                if merged is None:
                    return await self._ask(state, pending.intent, pending.missing, None, text)
                return await self._process(state, text, merged, key)
            else:
                state.pending = None

        if parse.status == "unknown" or intent is None:
            return AssistantReply(kind="not_understood", text=await self.acks.not_understood())
        if intent.action is Action.END_CALL:
            return AssistantReply(
                kind="end_call", text=await self.acks.farewell("caller"), end_call=True
            )
        if intent.action in (Action.SMALL_TALK, Action.CONFIRM):
            return AssistantReply(kind="small_talk", text=await self.acks.small_talk())
        if intent.action is Action.DENY:
            return AssistantReply(kind="declined", text=await self.acks.declined())
        return await self._process(state, text, intent, key)

    async def _process(
        self, state: ConversationState, text: str, intent: Intent, key: str | None
    ) -> AssistantReply:
        handler = self.commands.get(intent.action)
        if handler is None:
            return await self._unsupported(state, text, intent)

        missing = handler.missing_fields(intent)
        if missing:
            return await self._ask(state, intent, missing, None, text)

        issues = handler.validate(intent, self.connectors)
        if issues:
            fields = {i.field for i in issues}
            log.info("conversation.validation", issues=[i.code for i in issues])
            if "assignee" in fields:
                return await self._ask(state, intent, ["assignee"], "unknown_person", text)
            if "task" in fields:
                return await self._ask(state, intent, ["task"], None, text)
            log.error("conversation.misconfigured", issues=[i.message for i in issues])
            return AssistantReply(kind="error", text=await self.acks.system_error(), intent=intent)

        transcripts = state.pending.transcripts if state.pending else []
        return await self._accept(state, text, intent, key, transcripts, handler)

    async def _accept(
        self,
        state: ConversationState,
        text: str,
        intent: Intent,
        key: str | None,
        earlier: list[str],
        handler: CommandHandler | None = None,
    ) -> AssistantReply:
        handler = handler or self.commands.get(intent.action)
        assert handler is not None
        spec = handler.build_job(intent)
        spec.payload["requester_key"] = state.requester_key
        transcript = " | ".join([*earlier, text])
        key = key or make_idempotency_key(state.session_id, state.turn, normalize_text(transcript))
        try:
            metadata = intent.public_dict()
            metadata["requester"] = {
                "person_key": state.requester_key,
                "caller_number": state.caller_number,
            }
            job, created = await self.jobs.submit(
                spec,
                idempotency_key=key,
                transcript=transcript,
                intent=metadata,
                session_id=state.session_id,
                call_id=state.call_id,
            )
        except Exception:
            # Not persisted -> must not acknowledge as accepted.
            log.exception("conversation.persist_failed", session_id=state.session_id)
            return AssistantReply(kind="error", text=await self.acks.system_error(), intent=intent)
        state.pending = None
        if job.id not in state.accepted_job_ids:
            state.accepted_job_ids.append(job.id)
        return AssistantReply(
            kind="accepted",
            text=await self.acks.acknowledge(intent),
            intent=intent,
            job=job,
            duplicate=not created,
        )

    async def _ask(
        self,
        state: ConversationState,
        intent: Intent,
        missing: list[str],
        reason: str | None,
        text: str,
    ) -> AssistantReply:
        previous = state.pending if state.pending and state.pending.kind == "clarify" else None
        rounds = previous.rounds + 1 if previous else 1
        if rounds > MAX_CLARIFICATION_ROUNDS:
            state.pending = None
            return AssistantReply(
                kind="clarification", text=await self.acks.give_up_clarifying(), intent=intent
            )
        question = await self.acks.clarify(intent, missing, reason)
        stored = intent.model_copy(deep=True)
        if reason == "unknown_person":
            stored.assignee = None
            stored.details.assignee_key = None
            stored.details.assignee_spoken = None
            stored.details.assignee_gender = None
        transcripts = [*(previous.transcripts if previous else []), text]
        state.pending = Pending("clarify", stored, list(missing), rounds, transcripts)
        return AssistantReply(kind="clarification", text=question, intent=intent)

    async def _unsupported(
        self, state: ConversationState, text: str, intent: Intent
    ) -> AssistantReply:
        return AssistantReply(kind="unsupported", text="أقدر أضيف تاسكات بس.", intent=intent)

    async def _merge(self, pending: Pending, text: str, parse: ParseResult) -> Intent | None:
        """Fill the fields we asked for from a short follow-up answer."""
        merged = pending.intent.model_copy(deep=True)
        changed = False
        candidate = parse.intent if parse.intent and parse.intent.action == merged.action else None

        def take_person(source: Intent) -> None:
            nonlocal changed
            merged.assignee = source.assignee
            merged.details.assignee_spoken = source.details.assignee_spoken
            merged.details.assignee_key = source.details.assignee_key
            merged.details.assignee_gender = source.details.assignee_gender
            changed = True

        def take_task(source: Intent) -> None:
            nonlocal changed
            merged.task = source.task
            merged.details.action_phrase = source.details.action_phrase
            changed = True

        if "assignee" in pending.missing:
            if candidate and candidate.assignee:
                take_person(candidate)
            else:
                person = await self.parser.parse_person(text)
                if person.intent and person.intent.assignee:
                    take_person(person.intent)
        if "task" in pending.missing:
            if candidate and candidate.task:
                take_task(candidate)
            elif "assignee" not in pending.missing:
                answer = await self.parser.parse_task_answer(text)
                if answer.intent and answer.intent.task:
                    take_task(answer.intent)
        return merged if changed else None

    async def _audit(
        self, state: ConversationState, text: str, parse: ParseResult, reply: AssistantReply
    ) -> None:
        if self.session_log is None:
            return
        try:
            await asyncio.to_thread(
                self.session_log.record,
                session_id=state.session_id,
                call_id=state.call_id,
                turn=state.turn,
                transcript=text,
                parse_status=parse.status,
                intent=parse.intent.public_dict() if parse.intent else None,
                reply_kind=reply.kind,
                reply_text=reply.text,
                job_id=reply.job.id if reply.job else None,
            )
        except Exception:
            log.exception("conversation.audit_failed", session_id=state.session_id)
