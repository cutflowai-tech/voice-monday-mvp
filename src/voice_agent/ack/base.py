"""AcknowledgmentGenerator interface.

Generates everything the assistant *says*. The template implementation ships
today; an LLM-backed implementation can replace it without changing the
conversation engine or call pipeline.

Rules every implementation must follow:

* Short, natural Egyptian Arabic, spoken by a female assistant.
* Mention the person, action and key subject when known.
* Describe actions as accepted/pending, never as already completed: the
  background worker is the only component that knows whether it succeeded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from voice_agent.domain.intents import Intent

FarewellReason = Literal["caller", "timeout", "technical"]


class AcknowledgmentGenerator(ABC):
    @abstractmethod
    async def acknowledge(self, intent: Intent) -> str:
        """Confirm an accepted (persisted, not yet executed) command."""

    @abstractmethod
    async def clarify(self, intent: Intent | None, missing: list[str], reason: str | None) -> str:
        """Ask exactly one short question to resolve ``missing`` fields."""

    @abstractmethod
    async def unsupported(self, intent: Intent | None, offer: Intent | None) -> str:
        """Explain briefly that the request is not supported (optionally offer ``offer``)."""

    @abstractmethod
    async def greeting(self) -> str: ...

    @abstractmethod
    async def farewell(self, reason: FarewellReason) -> str: ...

    @abstractmethod
    async def small_talk(self) -> str: ...

    @abstractmethod
    async def declined(self) -> str: ...

    @abstractmethod
    async def not_understood(self) -> str: ...

    @abstractmethod
    async def give_up_clarifying(self) -> str: ...

    @abstractmethod
    async def system_error(self) -> str: ...
