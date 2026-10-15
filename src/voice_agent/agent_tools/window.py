"""Grace window for tasks proposed by the hosted voice agent (ElevenLabs).

The agent speaks its acknowledgment ("تمام، هقول لأحمد ...") and then calls
``submit_task``. The request waits ``grace_seconds`` before anything is saved, so a
correction spoken in that window can replace the draft (``mode=replace_last``) or
cancel it, while an additional request simply becomes another draft. After the
window the job is committed durably and the request waits briefly for the worker,
so the agent only says "Done" once the Monday item really exists.

Each draft runs in its own background task: a dropped webhook connection never
loses a committed task, and a retried webhook joins the existing draft instead of
creating a duplicate.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from voice_agent.agent_tools.task_text import normalize_task
from voice_agent.container import Container
from voice_agent.domain.intents import Action, Intent, IntentDetails
from voice_agent.jobs.models import JobStatus
from voice_agent.logging_setup import get_logger
from voice_agent.nlu.normalize import normalize_text

log = get_logger(__name__)

Mode = Literal["new", "replace_last"]
DraftState = Literal["waiting", "replaced", "cancelled", "committed"]


@dataclass
class Draft:
    key: str
    assignee_key: str
    task: str
    caller_id: str | None
    created_at: float
    state: DraftState = "waiting"
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    result: asyncio.Future[dict[str, Any]] | None = None


@dataclass
class Conversation:
    drafts: list[Draft] = field(default_factory=list)
    inflight: int = 0  # drafts waiting or being saved (drives the typing sound)
    last_seen: float = field(default_factory=time.monotonic)


class TaskWindow:
    def __init__(
        self,
        container: Container,
        *,
        grace_seconds: float = 2.0,
        done_wait_seconds: float = 5.0,
        poll_seconds: float = 0.1,
        idle_ttl_seconds: float = 3600.0,
        activity=None,
        speech_tail_seconds: float = 1.5,
        max_hold_seconds: float = 6.0,
    ):
        self.c = container
        self.activity = activity  # SpeechActivity: hold commits while the caller talks
        self.speech_tail_seconds = speech_tail_seconds
        self.max_hold_seconds = max_hold_seconds
        self.grace_seconds = grace_seconds
        self.done_wait_seconds = done_wait_seconds
        self.poll_seconds = poll_seconds
        self.idle_ttl_seconds = idle_ttl_seconds
        self._conversations: dict[str, Conversation] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ public API
    async def submit(
        self,
        *,
        conversation_id: str,
        assignee_key: str,
        task: str,
        mode: Mode = "new",
        caller_id: str | None = None,
    ) -> dict[str, Any]:
        task = " ".join(task.split())
        problem = self._validate(assignee_key, task)
        if problem:
            log.info("agent_tool.invalid", conversation_id=conversation_id, problem=problem)
            return {"status": "invalid", "problem": problem}

        conv = self._conversation(conversation_id)
        key = _draft_key(conversation_id, assignee_key, task)
        existing = next((d for d in conv.drafts if d.key == key), None)
        if existing and existing.state in ("waiting", "committed") and existing.result:
            log.info("agent_tool.joined", conversation_id=conversation_id, key=key)
            return await asyncio.shield(existing.result)

        previous = None
        if mode == "replace_last":
            previous = self._latest(conv, "waiting")
            if previous:
                self._finish_early(previous, "replaced")
                log.info("agent_tool.replaced", conversation_id=conversation_id, key=previous.key)

        draft = Draft(
            key=key,
            assignee_key=assignee_key,
            task=task,
            caller_id=caller_id,
            created_at=time.monotonic(),
        )
        draft.result = asyncio.get_running_loop().create_future()
        conv.drafts.append(draft)
        log.info(
            "agent_tool.received",
            conversation_id=conversation_id,
            key=key,
            mode=mode,
            assignee_key=assignee_key,
        )
        conv.inflight += 1
        await self._signal_saving(conversation_id, True)
        runner = asyncio.create_task(self._run(conversation_id, draft), name=f"draft-{key}")
        self._tasks.add(runner)
        runner.add_done_callback(self._tasks.discard)
        result = await asyncio.shield(draft.result)
        if mode == "replace_last" and previous is None and self._latest(conv, "committed", draft):
            result = {**result, "previous": "already_saved"}
        return result

    async def cancel_last(self, *, conversation_id: str) -> dict[str, Any]:
        conv = self._conversation(conversation_id)
        draft = self._latest(conv, "waiting")
        if draft:
            self._finish_early(draft, "cancelled")
            log.info("agent_tool.cancelled", conversation_id=conversation_id, key=draft.key)
            return {"status": "cancelled"}
        if self._latest(conv, "committed"):
            return {"status": "already_saved"}
        return {"status": "nothing_pending"}

    async def aclose(self) -> None:
        """Let committed drafts finish; drafts still waiting are committed immediately."""
        for conv in self._conversations.values():
            for draft in conv.drafts:
                draft.wake.set()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=self.done_wait_seconds + 5)

    # ------------------------------------------------------------------ internals
    def _validate(self, assignee_key: str, task: str) -> str | None:
        person = self.c.directory.get(assignee_key)
        if person is None:
            return "unknown_person: ask the caller who the task is for"
        if not task or len(task) > 255:
            return "bad_task: ask the caller to repeat the task briefly"
        handler = self.c.commands.get(Action.CREATE_TASK)
        if handler is None:
            return "unsupported"
        issues = handler.validate(self._intent(assignee_key, task), self.c.connectors)
        if issues:
            return f"{issues[0].code}: {person.name_en} cannot receive Monday tasks yet"
        return None

    def _intent(self, assignee_key: str, task: str) -> Intent:
        person = self.c.directory.get(assignee_key)
        assert person is not None
        return Intent(
            action=Action.CREATE_TASK,
            assignee=person.name_en,
            task=task,
            details=IntentDetails(
                assignee_key=person.key,
                assignee_spoken=person.name_ar,
                assignee_gender=person.gender,
            ),
        )

    async def _run(self, conversation_id: str, draft: Draft) -> None:
        assert draft.result is not None
        try:
            # wake is set early only to cancel/replace, or on shutdown (commit now)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(draft.wake.wait(), self.grace_seconds)
            await self._hold_while_caller_speaks(conversation_id, draft)
            if draft.state != "waiting":
                return
            draft.state = "committed"
            draft.result.set_result(await self._commit(conversation_id, draft))
        except Exception as exc:
            log.exception("agent_tool.commit_failed", conversation_id=conversation_id)
            if not draft.result.done():
                draft.result.set_result({"status": "failed", "problem": repr(exc)})
        finally:
            conv = self._conversations.get(conversation_id)
            if conv is not None:
                conv.inflight = max(0, conv.inflight - 1)
                if conv.inflight == 0:
                    await self._signal_saving(conversation_id, False)

    async def _signal_saving(self, conversation_id: str, on: bool) -> None:
        if self.activity is not None and hasattr(self.activity, "set_saving"):
            await self.activity.set_saving(conversation_id, on)

    async def _hold_while_caller_speaks(self, conversation_id: str, draft: Draft) -> None:
        """A correction only arrives after the caller stops talking and the agent reacts:
        keep the draft open until ``speech_tail_seconds`` of quiet (bounded)."""
        if self.activity is None:
            return
        deadline = draft.created_at + self.grace_seconds + self.max_hold_seconds
        held = False
        while not draft.wake.is_set() and time.monotonic() < deadline:
            since = await self.activity.seconds_since_speech(conversation_id)
            if since is None or since >= self.speech_tail_seconds:
                break
            if not held:
                held = True
                log.info("agent_tool.held_for_speech", conversation_id=conversation_id)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(draft.wake.wait(), self.poll_seconds)

    async def _commit(self, conversation_id: str, draft: Draft) -> dict[str, Any]:
        handler = self.c.commands.get(Action.CREATE_TASK)
        assert handler is not None
        requester_key = self.c.callers.lookup(draft.caller_id) if draft.caller_id else None
        requester = self.c.directory.get(requester_key) if requester_key else None
        assignee = self.c.directory.get(draft.assignee_key)
        assert assignee is not None
        # «يشتري كاميرات» -> «اشتري كاميرات»; «يتصل بيا» -> «اتصل بمحمد»
        title = normalize_task(
            draft.task,
            assignee_names=(assignee.name_ar, assignee.name_en, *assignee.aliases),
            assignee_female=assignee.gender == "f",
            caller_name=requester.name_ar if requester else None,
        )
        intent = self._intent(draft.assignee_key, title)
        spec = handler.build_job(intent)
        spec.payload["requester_key"] = requester_key
        metadata = intent.public_dict()
        metadata["requester"] = {"person_key": requester_key, "caller_number": draft.caller_id}
        metadata["source"] = "elevenlabs_agent"
        job, _ = await self.c.jobs.submit(
            spec,
            idempotency_key=draft.key,
            transcript=draft.task,  # the caller's words as the agent heard them
            intent=metadata,
            session_id=conversation_id,
            call_id=conversation_id,
        )
        log.info("agent_tool.committed", conversation_id=conversation_id, job_id=job.id)
        job = await self._wait_for_result(job.id) or job
        person = self.c.directory.get(draft.assignee_key)
        base = {"assignee": person.name_ar if person else draft.assignee_key, "task": title}
        if job.status is JobStatus.COMPLETED:
            status = "created"
        elif job.status in (JobStatus.FAILED, JobStatus.DEAD_LETTER):
            status = "failed"
        else:
            status = "queued"
        log.info(
            "agent_tool.result",
            conversation_id=conversation_id,
            job_id=job.id,
            status=status,
            seconds=round(time.monotonic() - draft.created_at, 2),
        )
        return {"status": status, "job_id": job.id, **base}

    async def _wait_for_result(self, job_id: str):
        deadline = time.monotonic() + self.done_wait_seconds
        job = None
        while True:
            job = await asyncio.to_thread(self.c.store.get, job_id)
            if job is None or job.is_terminal or time.monotonic() >= deadline:
                return job
            await asyncio.sleep(self.poll_seconds)

    def _finish_early(self, draft: Draft, state: DraftState) -> None:
        draft.state = state
        if draft.result and not draft.result.done():
            draft.result.set_result({"status": state})
        draft.wake.set()

    @staticmethod
    def _latest(conv: Conversation, state: DraftState, exclude: Draft | None = None):
        return next(
            (d for d in reversed(conv.drafts) if d.state == state and d is not exclude), None
        )

    def _conversation(self, conversation_id: str) -> Conversation:
        now = time.monotonic()
        for cid in [
            cid
            for cid, conv in self._conversations.items()
            if now - conv.last_seen > self.idle_ttl_seconds
        ]:
            self._conversations.pop(cid, None)
        conv = self._conversations.setdefault(conversation_id, Conversation())
        conv.last_seen = now
        return conv


def _draft_key(conversation_id: str, assignee_key: str, task: str) -> str:
    """Same conversation + person + words = same draft (webhook retries, double calls)."""
    digest = hashlib.sha256(
        f"{conversation_id}|{assignee_key}|{normalize_text(task)}".encode()
    ).hexdigest()
    return f"el_{digest[:32]}"
