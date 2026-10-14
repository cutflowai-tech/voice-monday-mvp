from __future__ import annotations

from voice_agent.commands.base import CommandHandler
from voice_agent.domain.intents import Action, Intent, IntentDetails
from voice_agent.jobs.models import JobSpec

CREATE_TASK_OPERATION = "create_task"


class CreateTaskHandler(CommandHandler):
    """``create_task`` -> any connector that implements the ``create_task`` operation."""

    action = Action.CREATE_TASK
    required_fields = ("assignee", "task")

    def __init__(self, connector: str = "monday"):
        self.connector = connector

    def build_job(self, intent: Intent) -> JobSpec:
        return JobSpec(
            connector=self.connector,
            operation=CREATE_TASK_OPERATION,
            payload={
                "title": (intent.task or "").strip(),
                "assignee": intent.assignee,
                "assignee_key": intent.details.assignee_key,
                "action_phrase": intent.details.action_phrase,
            },
        )

    def offer_for(self, intent: Intent) -> Intent | None:
        """A reminder we cannot send yet can still become a task for that person."""
        if intent.action is not Action.REMIND or not (intent.assignee and intent.task):
            return None
        when = intent.details.when
        task = f"{intent.task} {when}" if when else intent.task
        phrase = intent.details.action_phrase
        return Intent(
            action=Action.CREATE_TASK,
            assignee=intent.assignee,
            task=task[:255],
            details=IntentDetails(
                assignee_spoken=intent.details.assignee_spoken,
                assignee_key=intent.details.assignee_key,
                assignee_gender=intent.details.assignee_gender,
                action_phrase=f"{phrase} {when}" if phrase and when else phrase,
            ),
        )
