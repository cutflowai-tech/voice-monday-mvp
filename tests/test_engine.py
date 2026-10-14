"""Conversation engine: durable acceptance, clarification, unsupported, multi-turn."""

from __future__ import annotations

from voice_agent.conversation.agent import AgentDecision
from voice_agent.conversation.engine import ConversationState
from voice_agent.jobs.models import JobStatus

from .conftest import EXAMPLE


async def test_accepted_command_is_persisted_before_ack(container):
    state = ConversationState()
    reply = await container.engine.handle(state, EXAMPLE)

    assert reply.kind == "accepted"
    assert not reply.end_call
    stored = container.store.get(reply.job.id)  # committed before the reply existed
    assert stored is not None and stored.status is JobStatus.QUEUED
    assert stored.transcript == EXAMPLE
    assert stored.intent["action"] == "create_task"
    assert stored.intent["assignee"] == "Mohamed"
    assert stored.intent["task"] == "شراء هارد للسيرفر"
    assert stored.payload["assignee_key"] == "mohamed"
    assert stored.connector == "monday"
    assert "محمد" in reply.text and "هارد" in reply.text
    assert container.queue.pending() == 1


async def test_requester_metadata_never_replaces_assignee(container):
    state = ConversationState(caller_number="101", requester_key="ahmed")
    reply = await container.engine.handle(state, EXAMPLE)
    job = container.store.get(reply.job.id)
    assert job.intent["requester"] == {"person_key": "ahmed", "caller_number": "101"}
    assert job.payload["assignee_key"] == "mohamed"


async def test_agent_cannot_guess_an_unknown_spoken_assignee(container):
    class GuessingAgent:
        async def decide(self, text, pending):
            return AgentDecision(
                action="submit", assignee_key="mohamed", task="شراء هارد", question=None
            )

    container.engine.agent = GuessingAgent()
    reply = await container.engine.handle(ConversationState(), "حطي لسليم يشتري هارد")
    assert reply.kind == "clarification"
    assert container.store.status_counts() == {}


async def test_agent_cannot_create_task_from_audio_diagnostic(container):
    class GuessingAgent:
        async def decide(self, text, pending):
            return AgentDecision(action="submit", assignee_key="mohamed", task=text, question=None)

    container.engine.agent = GuessingAgent()
    reply = await container.engine.handle(ConversationState(), "اختبار الصوت 1 2 3")
    assert reply.kind == "not_understood"
    assert container.store.status_counts() == {}


async def test_clear_task_uses_fast_parser_path(container):
    class FailingAgent:
        async def decide(self, text, pending):
            raise AssertionError("clear task should not call the model")

    container.engine.agent = FailingAgent()
    reply = await container.engine.handle(ConversationState(), "حطي لمحمد تاسك يشتري هارد")
    assert reply.kind == "accepted"
    assert reply.job.payload["title"] == "شراء هارد"


async def test_clear_missing_task_asks_without_model_delay(container):
    class FailingAgent:
        async def decide(self, text, pending):
            raise AssertionError("clear missing task should not call the model")

    container.engine.agent = FailingAgent()
    reply = await container.engine.handle(ConversationState(), "حطي تاسك لمحمد")
    assert reply.kind == "clarification"
    assert reply.job is None
    assert "محمد" in reply.text


async def test_same_utterance_correction_preserves_task_details(container):
    reply = await container.engine.handle(
        ConversationState(), "حطي لمحمد يشتري هارد اتنين تيرا، لا قصدي أحمد"
    )
    assert reply.kind == "accepted"
    assert reply.job.payload["assignee_key"] == "ahmed"
    assert "اتنين تيرا" in reply.job.payload["title"]


async def test_mohsen_is_verified_alias_for_mohamed(container):
    direct = await container.engine.handle(ConversationState(), "حطي لمحسن يشتري هارد")
    assert direct.kind == "accepted"
    assert direct.job.payload["assignee_key"] == "mohamed"

    reply = await container.engine.handle(
        ConversationState(), "حطي لمحمد يشتري هارد اتنين تيرا، لا قصدي محسن"
    )
    assert reply.kind == "accepted"
    assert reply.job.payload["assignee_key"] == "mohamed"
    assert "اتنين تيرا" in reply.job.payload["title"]


async def test_saved_job_can_be_replaced_only_before_worker_claims(container, worker):
    state = ConversationState()
    first = await container.engine.handle(state, EXAMPLE)
    second = await container.engine.handle(state, "لا قصدي أحمد")
    assert second.kind == "accepted"
    assert container.store.get(first.job.id).last_error == "cancelled by caller before execution"
    await worker.run_until_idle()
    assert container.store.get(second.job.id).status is JobStatus.COMPLETED


async def test_executed_job_correction_is_truthful(container, worker):
    state = ConversationState()
    await container.engine.handle(state, EXAMPLE)
    await worker.run_until_idle()
    reply = await container.engine.handle(state, "لا قصدي أحمد")
    assert reply.kind == "unsupported"
    assert container.store.status_counts() == {"completed": 1}


async def test_multiple_commands_in_one_session(container, worker, mock_monday):
    state = ConversationState()
    commands = [
        EXAMPLE,
        "حطي تاسك لأحمد إنه يراجع الميزانية",
        "اعملي تاسك لسارة إنها تكلم المورد",
    ]
    replies = [await container.engine.handle(state, text) for text in commands]

    assert [r.kind for r in replies] == ["accepted"] * 3
    assert not any(r.end_call for r in replies)
    assert not state.ended
    assert len(set(state.accepted_job_ids)) == 3
    await worker.run_until_idle()
    assert {c["title"] for c in mock_monday.calls} == {
        "شراء هارد للسيرفر",
        "مراجعة الميزانية",
        "التواصل مع المورد",
    }


async def test_unclear_request_asks_one_question_and_creates_nothing(container):
    state = ConversationState()
    reply = await container.engine.handle(state, "حطي تاسك لمحمد")
    assert reply.kind == "clarification"
    assert reply.text.count("؟") == 1
    assert reply.job is None
    assert container.store.status_counts() == {}

    reply = await container.engine.handle(state, "يشتري هارد للسيرفر")
    assert reply.kind == "accepted"
    assert reply.intent.assignee == "Mohamed"
    assert reply.intent.task == "شراء هارد للسيرفر"
    job = container.store.get(reply.job.id)
    assert job.transcript == "حطي تاسك لمحمد | يشتري هارد للسيرفر"


async def test_missing_assignee_then_answer(container):
    state = ConversationState()
    first = await container.engine.handle(state, "حطي تاسك إنه يشتري هارد للسيرفر")
    assert first.kind == "clarification" and "مين" in first.text
    second = await container.engine.handle(state, "لمحمد")
    assert second.kind == "accepted"
    assert second.intent.assignee == "Mohamed"


async def test_unknown_person_triggers_clarification(container):
    state = ConversationState()
    reply = await container.engine.handle(state, "حطي تاسك لشادي إنه يشتري هارد للسيرفر")
    assert reply.kind == "clarification"
    assert "شادي" in reply.text
    reply = await container.engine.handle(state, "خليها لمحمد")
    assert reply.kind == "accepted"
    assert reply.intent.assignee == "Mohamed"
    assert reply.intent.task == "شراء هارد للسيرفر"


async def test_clarification_gives_up_after_two_rounds(container):
    state = ConversationState()
    await container.engine.handle(state, "حطي تاسك")
    await container.engine.handle(state, "مش عارف")
    reply = await container.engine.handle(state, "برضه مش عارف")
    assert reply.kind == "clarification"
    assert state.pending is None
    assert container.store.status_counts() == {}


async def test_new_full_command_overrides_pending_clarification(container):
    state = ConversationState()
    await container.engine.handle(state, "حطي تاسك")
    reply = await container.engine.handle(state, EXAMPLE)
    assert reply.kind == "accepted"
    assert state.pending is None


async def test_unsupported_command_keeps_call_open(container):
    state = ConversationState()
    reply = await container.engine.handle(state, "ابعتي لسارة تقرير المبيعات")
    assert reply.kind == "unsupported"
    assert not reply.end_call
    assert "تاسكات" in reply.text
    assert container.store.status_counts() == {}


async def test_unsupported_reminder_does_not_become_a_task(container):
    state = ConversationState()
    reply = await container.engine.handle(state, "فكري أحمد يكلم العميل بكرة")
    assert reply.kind == "unsupported"
    assert state.pending is None
    reply = await container.engine.handle(state, "آه حطيها")
    assert reply.kind != "accepted"
    assert container.store.status_counts() == {}


async def test_declined_offer_creates_nothing(container):
    state = ConversationState()
    await container.engine.handle(state, "فكري أحمد يكلم العميل بكرة")
    reply = await container.engine.handle(state, "لا")
    assert reply.kind == "declined"
    assert container.store.status_counts() == {}


async def test_gibberish_is_not_understood(container):
    reply = await container.engine.handle(ConversationState(), "الجو حلو النهارده")
    assert reply.kind == "not_understood"
    assert not reply.end_call


async def test_end_call_only_on_request(container):
    state = ConversationState()
    await container.engine.handle(state, EXAMPLE)
    assert not state.ended
    reply = await container.engine.handle(state, "شكرا")
    assert reply.kind == "small_talk" and not reply.end_call
    reply = await container.engine.handle(state, "خلاص كده شكرا مع السلامة")
    assert reply.kind == "end_call" and reply.end_call and state.ended


async def test_same_idempotency_key_returns_same_job(container, worker, mock_monday):
    state = ConversationState()
    r1 = await container.engine.handle(state, EXAMPLE, idempotency_key="client-key-0001")
    r2 = await container.engine.handle(state, EXAMPLE, idempotency_key="client-key-0001")
    assert r1.job.id == r2.job.id
    assert r2.duplicate
    await worker.run_until_idle()
    assert len(mock_monday.items) == 1


async def test_persist_failure_is_never_acknowledged(container, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(container.store, "create", boom)
    reply = await container.engine.handle(ConversationState(), EXAMPLE)
    assert reply.kind == "error"
    assert reply.job is None
    assert "هقول" not in reply.text


async def test_utterances_are_audited_separately(container):
    state = ConversationState()
    await container.engine.handle(state, "حطي تاسك لمحمد")
    await container.engine.handle(state, "يشتري هارد للسيرفر")
    rows = container.session_log.for_session(state.session_id)
    assert [r["reply_kind"] for r in rows] == ["clarification", "accepted"]
    assert rows[1]["job_id"]
