"""Command handlers turn a complete intent into a connector-agnostic job.

To add a command: add an ``Action`` + parser support, write a handler that
builds a :class:`JobSpec`, and register it in ``voice_agent.container``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from voice_agent.connectors.base import ConnectorRegistry, ValidationIssue
from voice_agent.domain.intents import Action, Intent
from voice_agent.jobs.models import JobSpec


class CommandHandler(ABC):
    action: ClassVar[Action]
    required_fields: ClassVar[tuple[str, ...]] = ()

    def missing_fields(self, intent: Intent) -> list[str]:
        return [f for f in self.required_fields if not getattr(intent, f, None)]

    @abstractmethod
    def build_job(self, intent: Intent) -> JobSpec: ...

    def validate(self, intent: Intent, connectors: ConnectorRegistry) -> list[ValidationIssue]:
        spec = self.build_job(intent)
        if spec.connector not in connectors:
            return [ValidationIssue("connector", "missing", f"{spec.connector} not registered")]
        return connectors.get(spec.connector).validate(spec.operation, spec.payload)

    def offer_for(self, intent: Intent) -> Intent | None:
        """Optionally convert an unsupported intent into one this handler can run."""
        return None


class CommandRegistry:
    def __init__(self) -> None:
        self._handlers: dict[Action, CommandHandler] = {}

    def register(self, handler: CommandHandler) -> None:
        self._handlers[handler.action] = handler

    def get(self, action: Action) -> CommandHandler | None:
        return self._handlers.get(action)

    def handlers(self) -> list[CommandHandler]:
        return list(self._handlers.values())
