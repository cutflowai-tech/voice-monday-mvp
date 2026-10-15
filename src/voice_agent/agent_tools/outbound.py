"""Outbound calls requested by Sarah (e.g. «اتصلي بأحمد واعملي فيه مقلب»).

The API validates and rate-limits a request, then queues it in Redis. The AudioSocket bridge
(which runs on the PBX host and can reach ARI on loopback) places the call and, when it is
answered, starts Sarah in the matching mode. Only internal extensions from the caller
directory can be called; outside numbers are impossible by construction.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from voice_agent.domain.callers import CallerDirectory
from voice_agent.domain.people import PeopleDirectory
from voice_agent.logging_setup import get_logger

log = get_logger(__name__)

PURPOSES = ("prank",)


@dataclass
class OutboundRequest:
    target_key: str
    extension: str
    purpose: str
    requested_by: str | None  # person key of the caller who asked, if known
    requested_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> OutboundRequest:
        return cls(**json.loads(raw))


class OutboundCalls:
    def __init__(
        self,
        redis,
        *,
        people: PeopleDirectory,
        callers: CallerDirectory,
        namespace: str = "voice-agent",
        per_target_seconds: int = 300,
        per_requester_limit: int = 3,
        per_requester_window_seconds: int = 600,
    ):
        self.redis = redis
        self.people = people
        self.callers = callers
        self.queue_key = f"{namespace}:outbound"
        self.ns = namespace
        self.per_target_seconds = per_target_seconds
        self.per_requester_limit = per_requester_limit
        self.per_requester_window_seconds = per_requester_window_seconds

    async def request(
        self, *, target_key: str, purpose: str, caller_id: str | None
    ) -> dict[str, Any]:
        person = self.people.get(target_key)
        extension = self.callers.extension_for(target_key) if person else None
        if person is None or extension is None:
            return {"status": "unknown_person"}
        if purpose not in PURPOSES:
            return {"status": "unsupported"}
        requester = self.callers.lookup(caller_id) if caller_id else None
        if requester == target_key:
            return {"status": "same_person", "name": person.name_ar}
        # one prank per target every few minutes, and a few per requester
        target_lock = f"{self.ns}:outbound:target:{target_key}"
        if not await self.redis.set(target_lock, "1", nx=True, ex=self.per_target_seconds):
            return {"status": "too_soon", "name": person.name_ar}
        if requester:
            counter = f"{self.ns}:outbound:by:{requester}"
            count = await self.redis.incr(counter)
            if count == 1:
                await self.redis.expire(counter, self.per_requester_window_seconds)
            if count > self.per_requester_limit:
                await self.redis.delete(target_lock)
                return {"status": "limit_reached"}
        request = OutboundRequest(
            target_key=target_key,
            extension=extension,
            purpose=purpose,
            requested_by=requester,
            requested_at=time.time(),
        )
        await self.redis.lpush(self.queue_key, request.to_json())
        log.info("outbound.requested", target=target_key, purpose=purpose, by=requester)
        return {"status": "calling", "name": person.name_ar}

    async def next_request(self, wait_seconds: float = 5.0) -> OutboundRequest | None:
        item = await self.redis.brpop(self.queue_key, timeout=wait_seconds)
        if not item:
            return None
        request = OutboundRequest.from_json(item[1])
        if time.time() - request.requested_at > 60:  # stale (bridge was down): drop it
            log.warning("outbound.stale_request_dropped", target=request.target_key)
            return None
        return request
