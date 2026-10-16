"""HTTP API: health, readiness, simulation and job status."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from voice_agent.agent_tools.activity import SpeechActivity
from voice_agent.agent_tools.monday_assistant import MondayAssistant
from voice_agent.agent_tools.outbound import OutboundCalls
from voice_agent.agent_tools.router import router as agent_tools_router
from voice_agent.agent_tools.window import TaskWindow
from voice_agent.config import Role, Settings, load_settings
from voice_agent.connectors.monday_api import MondayAPI, MondayTools
from voice_agent.container import Container, build_container
from voice_agent.conversation.engine import ConversationState
from voice_agent.jobs.queue import RedisJobQueue
from voice_agent.logging_setup import configure_logging, get_logger
from voice_agent.telephony.media import SAFE_NAME

log = get_logger(__name__)

KEY_PATTERN = r"^[A-Za-z0-9_.:\-]{8,128}$"
SESSION_PATTERN = r"^[A-Za-z0-9_\-]{1,64}$"


class SimulateRequest(BaseModel):
    text: str = Field(
        min_length=1, max_length=1000, examples=["حطي تاسك لمحمد إنه يشتري هارد للسيرفر"]
    )
    session_id: str | None = Field(
        default=None,
        pattern=SESSION_PATTERN,
        description="Reuse to continue a multi-turn conversation (clarifications, offers).",
    )
    idempotency_key: str | None = Field(
        default=None,
        pattern=KEY_PATTERN,
        description="Resubmitting with the same key returns the original job (no duplicates).",
    )
    wait_seconds: float = Field(
        default=0, ge=0, le=30, description="Optionally wait for the job to reach a final state."
    )


class SimulatedSessions:
    """In-memory conversation state for /simulate (one per session_id)."""

    def __init__(self, max_sessions: int = 1000, ttl_seconds: float = 1800):
        self._items: OrderedDict[str, tuple[float, ConversationState]] = OrderedDict()
        self.max_sessions = max_sessions
        self.ttl = ttl_seconds

    def get(self, session_id: str | None) -> ConversationState:
        now = time.time()
        for key in [k for k, (ts, _) in self._items.items() if now - ts > self.ttl]:
            self._items.pop(key, None)
        if session_id and session_id in self._items:
            state = self._items.pop(session_id)[1]
        else:
            state = ConversationState(call_id="simulated")
            if session_id:
                state.session_id = session_id
        self._items[state.session_id] = (now, state)
        while len(self._items) > self.max_sessions:
            self._items.popitem(last=False)
        return state


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal settings, container
        if container is None:
            settings = settings or load_settings()
            configure_logging(settings.log_level, settings.log_format)
            settings.validate_for(Role.API)
            container = build_container(settings)
        app.state.container = container
        app.state.sessions = SimulatedSessions()
        s = container.settings
        activity = None
        if s.agent_tool_secret and isinstance(container.queue, RedisJobQueue):
            activity = SpeechActivity(container.queue.redis, s.queue_name)
        app.state.task_window = (
            TaskWindow(
                container,
                grace_seconds=s.agent_grace_seconds,
                done_wait_seconds=s.agent_done_wait_seconds,
                activity=activity,
                speech_tail_seconds=s.agent_speech_tail_seconds,
                max_hold_seconds=s.agent_max_hold_seconds,
            )
            if s.agent_tool_secret
            else None
        )
        app.state.outbound = (
            OutboundCalls(
                container.queue.redis,
                people=container.directory,
                callers=container.callers,
                namespace=s.queue_name,
            )
            if s.agent_tool_secret
            and s.agent_outbound_enabled
            and isinstance(container.queue, RedisJobQueue)
            else None
        )
        app.state.monday_assistant = (
            MondayAssistant(
                tools=MondayTools(
                    MondayAPI(
                        s.monday_api_token, url=s.monday_api_url, version=s.monday_api_version
                    )
                ),
                api_key=s.agent_api_key,
                model=s.monday_assistant_llm(),
                provider=s.agent_provider,
                allowed=s.monday_assistant_allowed(),
                callers=container.callers,
                directory=container.directory,
                monday_users=container.boards.monday_users,
                activity=activity,
                timeout_seconds=s.monday_assistant_timeout_seconds,
            )
            if s.agent_tool_secret
            and s.agent_api_key
            and s.monday_api_token
            and s.monday_assistant_allowed()
            else None
        )
        if app.state.monday_assistant is not None:
            app.state.monday_assistant.start()
        log.info(
            "api.started",
            monday_mode=s.monday_mode,
            agent_tools=app.state.task_window is not None,
            outbound_calls=app.state.outbound is not None,
            monday_assistant=app.state.monday_assistant is not None,
        )
        try:
            yield
        finally:
            if app.state.task_window:
                await app.state.task_window.aclose()
            if app.state.monday_assistant:
                await app.state.monday_assistant.aclose()
            await container.aclose()
            log.info("api.stopped")

    app = FastAPI(title="Voice Monday Agent", version="0.1.0", lifespan=lifespan)
    app.include_router(agent_tools_router)

    @app.middleware("http")
    async def only_agent_tools_from_tunnel(request: Request, call_next):
        # Requests relayed by Cloudflare always carry CF-Connecting-IP. Whatever the
        # tunnel's path rules say, only the agent tools are reachable from outside.
        if "cf-connecting-ip" in request.headers and not request.url.path.startswith(
            "/agent/tools/"
        ):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await call_next(request)

    def get_container(request: Request) -> Container:
        return request.app.state.container

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        c = get_container(request)
        checks: dict[str, Any] = {}
        try:
            checks["database"] = await asyncio.to_thread(c.db.check)
        except Exception as exc:
            checks["database"] = f"error: {exc}"
        try:
            checks["queue"] = await asyncio.wait_for(c.queue.ping(), 2)
        except Exception as exc:
            checks["queue"] = f"error: {exc!r}"
        for name in c.connectors.names():
            try:
                checks[f"connector:{name}"] = await c.connectors.get(name).check_ready()
            except Exception as exc:
                checks[f"connector:{name}"] = f"error: {exc}"
        ok = all(v is True for v in checks.values())
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks},
            status_code=200 if ok else 503,
        )

    @app.post("/simulate")
    async def simulate(
        body: SimulateRequest,
        request: Request,
        idempotency_header: Annotated[
            str | None, Header(alias="Idempotency-Key", pattern=KEY_PATTERN)
        ] = None,
    ) -> dict[str, Any]:
        c = get_container(request)
        state = request.app.state.sessions.get(body.session_id)
        key = body.idempotency_key or idempotency_header
        reply = await c.engine.handle(state, body.text, idempotency_key=key)
        job = reply.job
        if job and body.wait_seconds:
            job = await _wait_for_terminal(c, job.id, body.wait_seconds) or job
        return {
            "job_id": job.id if job else None,
            "job_ids": state.accepted_job_ids.copy(),
            "status": job.status.value if job else None,
            "intent": reply.intent.public_dict() if reply.intent else None,
            "acknowledgment": reply.text,
            "reply_kind": reply.kind,
            "duplicate": reply.duplicate,
            "session_id": state.session_id,
            "call_active": not reply.end_call,
            "job": job.public_dict() if job else None,
        }

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> dict[str, Any]:
        job = await asyncio.to_thread(get_container(request).store.get, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.public_dict()

    @app.get("/jobs/{job_id}/events")
    async def get_job_events(job_id: str, request: Request) -> dict[str, Any]:
        c = get_container(request)
        job = await asyncio.to_thread(c.store.get, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        events = await asyncio.to_thread(c.store.events, job_id)
        return {
            "job_id": job_id,
            "status": job.status.value,
            "events": [e.public_dict() for e in events],
        }

    @app.get("/media/{name}.wav", include_in_schema=False)
    async def media(name: str, request: Request) -> FileResponse:
        s = get_container(request).settings
        if s.asterisk_playback_mode != "http" or not SAFE_NAME.match(name):
            raise HTTPException(status_code=404)
        path = Path(s.asterisk_sounds_dir) / f"{name}.wav"
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="audio/wav")

    return app


async def _wait_for_terminal(c: Container, job_id: str, wait_seconds: float):
    deadline = time.monotonic() + wait_seconds
    job = None
    while time.monotonic() < deadline:
        job = await asyncio.to_thread(c.store.get, job_id)
        if job is None or job.is_terminal:
            return job
        await asyncio.sleep(0.2)
    return job
