"""Caller speech activity shared between the AudioSocket bridge and the API.

The grace window must not close while the caller is still saying a correction: the
correction only reaches the API (as a tool call) after the caller stops talking and the
agent has processed it. The bridge marks "speaking" in Redis while it hears the caller;
the API holds a pending commit while the mark is fresh, then waits a short tail.
"""

from __future__ import annotations

import time

from voice_agent.logging_setup import get_logger

log = get_logger(__name__)

MARK_TTL_MS = 5000  # longer than any speech tail we wait for


class SpeechActivity:
    def __init__(self, redis, namespace: str = "voice-agent"):
        self.redis = redis
        self.ns = namespace
        self._last_mark: dict[str, float] = {}
        self._warned = False

    def _key(self, conversation_id: str) -> str:
        return f"{self.ns}:speaking:{conversation_id}"

    async def mark(self, conversation_id: str) -> None:
        """Caller is talking now: store the wall-clock time (refreshed at most every 100 ms)."""
        now = time.monotonic()
        if now - self._last_mark.get(conversation_id, 0.0) < 0.1:
            return
        self._last_mark[conversation_id] = now
        try:
            await self.redis.set(self._key(conversation_id), f"{time.time():.3f}", px=MARK_TTL_MS)
        except Exception as exc:
            if not self._warned:
                self._warned = True
                log.warning("speech_activity.unavailable", error=repr(exc))

    async def seconds_since_speech(self, conversation_id: str) -> float | None:
        """None when the caller has not spoken recently (or Redis is unavailable)."""
        try:
            value = await self.redis.get(self._key(conversation_id))
        except Exception:
            return None  # without Redis the window falls back to its fixed length
        if value is None:
            return None
        return max(0.0, time.time() - float(value))

    def forget(self, conversation_id: str) -> None:
        self._last_mark.pop(conversation_id, None)


def is_speech(pcm: bytes, threshold: int = 700) -> bool:
    """Crude energy VAD on 16-bit little-endian PCM (mean absolute amplitude)."""
    if len(pcm) < 4:
        return False
    total = 0
    count = len(pcm) // 2
    for i in range(0, count * 2, 8):  # every 4th sample is plenty for 20 ms frames
        total += abs(int.from_bytes(pcm[i : i + 2], "little", signed=True))
    return total / max(1, (count + 3) // 4) > threshold
