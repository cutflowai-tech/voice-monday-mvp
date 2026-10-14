"""Connector interface and registry.

A connector executes one kind of external side effect (create a Monday item,
send a WhatsApp message, ...). To add one:

1. Subclass :class:`Connector`, set ``name`` and ``operations``.
2. Implement ``execute`` (and optionally ``validate``/``check_ready``).
3. Register it in ``voice_agent.container.build_connectors``.

Connectors must raise :class:`TransientConnectorError` only for failures that are
safe to retry, and :class:`PermanentConnectorError` otherwise.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(frozen=True)
class ExecutionContext:
    job_id: str
    idempotency_key: str
    attempt: int
    created_at: float
    prior_attempt_uncertain: bool = False
    """True when an earlier attempt may have reached the external system."""


@dataclass(frozen=True)
class ConnectorResult:
    external_id: str | None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    code: str
    message: str


class ConnectorError(Exception):
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        uncertain: bool = False,
        code: str | None = None,
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.uncertain = uncertain
        self.code = code


class TransientConnectorError(ConnectorError):
    """Safe to retry (timeouts, 5xx, rate limits, locks)."""

    retryable = True


class PermanentConnectorError(ConnectorError):
    """Retrying will not help (bad config, invalid ids, auth failure)."""

    retryable = False


class Connector(ABC):
    name: ClassVar[str]
    operations: ClassVar[frozenset[str]]

    def validate(self, operation: str, payload: dict[str, Any]) -> list[ValidationIssue]:
        """Cheap, local pre-flight check run *before* a job is accepted."""
        if operation not in self.operations:
            return [ValidationIssue("operation", "unsupported", f"{self.name}: {operation}")]
        return []

    @abstractmethod
    async def execute(
        self, operation: str, payload: dict[str, Any], ctx: ExecutionContext
    ) -> ConnectorResult: ...

    async def check_ready(self) -> bool:
        return True

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        pass


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        if connector.name in self._connectors:
            raise ValueError(f"Connector already registered: {connector.name}")
        self._connectors[connector.name] = connector

    def get(self, name: str) -> Connector:
        try:
            return self._connectors[name]
        except KeyError:
            raise KeyError(f"No connector registered under {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._connectors

    def names(self) -> list[str]:
        return sorted(self._connectors)

    async def aclose(self) -> None:
        for connector in self._connectors.values():
            await connector.aclose()
