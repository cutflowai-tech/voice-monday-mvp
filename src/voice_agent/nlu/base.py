"""IntentParser interface.

Implementations turn one transcribed utterance into a :class:`ParseResult`.
The deterministic rule-based parser ships today; an LLM-backed parser (Claude,
OpenAI, ...) can implement the same interface and be swapped in via the
container without touching the call pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from voice_agent.domain.intents import ParseResult


class IntentParser(ABC):
    @abstractmethod
    async def parse(self, text: str) -> ParseResult:
        """Parse a full utterance into a structured intent."""

    async def parse_person(self, text: str) -> ParseResult:
        """Interpret a short answer to "who?" (used during clarification)."""
        return await self.parse(text)

    async def parse_task_answer(self, text: str) -> ParseResult:
        """Interpret a short answer to "what should they do?" (used during clarification)."""
        return await self.parse(text)
