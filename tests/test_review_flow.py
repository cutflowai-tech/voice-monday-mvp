"""No Monday job is accepted until the caller reviews every spoken task."""

from voice_agent.conversation.agent import ReviewDecision, ReviewTask
from voice_agent.conversation.engine import ConversationState


async def test_two_tasks_are_read_back_and_saved_only_after_yes(container):
    container.engine.review_before_submit = True
    state = ConversationState(caller_number="102", requester_key="ahmed")
    first = await container.engine.handle(
        state, "حطي لمحسن تاسك ييجي بعد بكرة، وقولي لميكل ييجي بكرة"
    )
    assert first.kind == "review"
    assert "محمد" in first.text and "مايكل" in first.text
    assert "صح كده" in first.text
    assert container.store.status_counts() == {}

    confirmed = await container.engine.handle(state, "آه صح")
    assert confirmed.kind == "accepted"
    assert len(state.accepted_job_ids) == 2
    assert container.store.status_counts() == {"queued": 2}
    jobs = [container.store.get(job_id) for job_id in state.accepted_job_ids]
    assert [job.payload["assignee_key"] for job in jobs] == ["mohamed", "michael"]
    assert all(job.intent["requester"]["person_key"] == "ahmed" for job in jobs)


async def test_no_correction_repeats_review_without_saving(container):
    container.engine.review_before_submit = True
    state = ConversationState()
    await container.engine.handle(state, "حطي لمحمد يشتري هارد بعد بكرة")
    revised = await container.engine.handle(state, "لا غلط، عدليها لبكرة")
    assert revised.kind == "review"
    assert "بكرة" in revised.text
    assert "بعد بكرة" not in revised.text
    assert container.store.status_counts() == {}
    await container.engine.handle(state, "أيوه")
    job = container.store.get(state.accepted_job_ids[0])
    assert "بكرة" in job.payload["title"]
    assert "بعد بكرة" not in job.payload["title"]


async def test_hangup_without_confirmation_creates_nothing(container):
    container.engine.review_before_submit = True
    await container.engine.handle(ConversationState(), "حطي لمحمد يشتري هارد")
    assert container.store.status_counts() == {}


async def test_last_call_transcript_is_reviewed_as_two_tasks(container):
    class ReviewAgent:
        async def decide_review(self, text, current):
            assert "المحسن" in text and "الميكل" in text
            assert current == []
            return ReviewDecision(
                action="draft",
                tasks=[
                    ReviewTask(assignee_key="mohamed", task="يأتي بعد بكرة"),
                    ReviewTask(assignee_key="michael", task="يأتي بكرة"),
                ],
                question=None,
            )

    container.engine.review_before_submit = True
    container.engine.agent = ReviewAgent()
    state = ConversationState(caller_number="102", requester_key="ahmed")
    reply = await container.engine.handle(
        state, "أريك أن تقول المحسن أنه يأتي بعد بكرة. وتقول الميكل أنه يأتي لبكرة."
    )
    assert reply.kind == "review"
    assert "بعد بكرة" in reply.text and "مايكل" in reply.text
    assert container.store.status_counts() == {}


async def test_ambiguous_correction_never_saves_current_draft(container):
    container.engine.review_before_submit = True
    state = ConversationState()
    await container.engine.handle(state, "حطي لمحسن تاسك ييجي بعد بكرة، وقولي لميكل ييجي بكرة")
    reply = await container.engine.handle(state, "لا غلط عدليها")
    assert reply.kind == "clarification"
    assert len(state.review_drafts) == 2
    assert container.store.status_counts() == {}


async def test_named_correction_updates_only_target_task(container):
    class ReviewAgent:
        async def decide_review(self, text, current):
            assert "التاني" in text
            assert len(current) == 1 and current[0].assignee_key == "michael"
            return ReviewDecision(
                action="draft",
                tasks=[ReviewTask(assignee_key="michael", task="ييجي الخميس بدل بكرة")],
                question=None,
            )

    container.engine.review_before_submit = True
    state = ConversationState()
    await container.engine.handle(state, "حطي لمحسن تاسك ييجي بعد بكرة، وقولي لميكل ييجي بكرة")
    container.engine.agent = ReviewAgent()
    reply = await container.engine.handle(state, "لا غلط، التاني خليه ييجي الخميس")
    assert reply.kind == "review"
    assert "بعد بكرة" in reply.text and "الخميس" in reply.text
    assert container.store.status_counts() == {}
    await container.engine.handle(state, "آه صح")
    jobs = [container.store.get(job_id) for job_id in state.accepted_job_ids]
    assert "بعد بكرة" in jobs[0].payload["title"]
    assert "الخميس" in jobs[1].payload["title"]


async def test_wrong_task_from_model_is_rejected(container):
    class WrongReviewAgent:
        async def decide_review(self, text, current):
            return ReviewDecision(
                action="draft",
                tasks=[ReviewTask(assignee_key="mohamed", task="ييجي الخميس")],
                question=None,
            )

    container.engine.review_before_submit = True
    state = ConversationState()
    await container.engine.handle(state, "حطي لمحسن تاسك ييجي بعد بكرة، وقولي لميكل ييجي بكرة")
    container.engine.agent = WrongReviewAgent()
    reply = await container.engine.handle(state, "لا غلط، التاني خليه الخميس")
    assert reply.kind == "clarification"
    assert "بعد بكرة" in state.review_drafts[0].task
    assert "بكرة" in state.review_drafts[1].task
    assert container.store.status_counts() == {}


async def test_date_correction_with_ordinal_changes_only_second_task(container):
    container.engine.review_before_submit = True
    state = ConversationState()
    await container.engine.handle(state, "حطي لمحسن تاسك ييجي بعد بكرة، وقولي لميكل ييجي بكرة")
    reply = await container.engine.handle(state, "لا غلط، التاني الخميس بدل بكرة")
    assert reply.kind == "review"
    assert "بعد بكرة" in state.review_drafts[0].task
    assert "الخميس" in state.review_drafts[1].task
    assert "بكرة" not in state.review_drafts[1].task
    assert container.store.status_counts() == {}


async def test_multiple_names_without_clear_separator_ask_or_extract(container):
    container.engine.review_before_submit = True
    reply = await container.engine.handle(
        ConversationState(), "حطي تاسك لمحمد ييجي بكرة ولميكل ييجي الخميس"
    )
    assert reply.kind == "clarification"
    assert container.store.status_counts() == {}
