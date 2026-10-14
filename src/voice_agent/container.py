"""Composition root: the only place that picks concrete implementations.

Swap an implementation (LLM parser, another TTS, a new connector) here; the call
pipeline, engine and worker depend only on the interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass

from voice_agent.ack.base import AcknowledgmentGenerator
from voice_agent.ack.templates import TemplateAcknowledgmentGenerator
from voice_agent.commands.base import CommandRegistry
from voice_agent.commands.create_task import CreateTaskHandler
from voice_agent.config import Settings
from voice_agent.connectors.base import ConnectorRegistry
from voice_agent.connectors.monday import (
    MockMondayConnector,
    MondayConfig,
    MondayGraphQLConnector,
)
from voice_agent.conversation.agent import StructuredTaskAgent
from voice_agent.conversation.engine import ConversationEngine
from voice_agent.conversation.session_log import SessionLog
from voice_agent.db import Database
from voice_agent.domain.callers import CallerDirectory
from voice_agent.domain.people import PeopleDirectory
from voice_agent.jobs.queue import JobQueue, RedisJobQueue
from voice_agent.jobs.service import JobService
from voice_agent.jobs.store import JobStore
from voice_agent.jobs.worker import RetryPolicy, Worker
from voice_agent.nlu.base import IntentParser
from voice_agent.nlu.egyptian_parser import EgyptianArabicParser


@dataclass
class Container:
    settings: Settings
    db: Database
    store: JobStore
    session_log: SessionLog
    queue: JobQueue
    jobs: JobService
    directory: PeopleDirectory
    callers: CallerDirectory
    parser: IntentParser
    acks: AcknowledgmentGenerator
    commands: CommandRegistry
    connectors: ConnectorRegistry
    engine: ConversationEngine
    agent: StructuredTaskAgent | None = None

    async def aclose(self) -> None:
        if self.agent:
            await self.agent.aclose()
        await self.connectors.aclose()
        await self.queue.close()


def build_connectors(settings: Settings, directory: PeopleDirectory) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    person_ids = settings.monday_person_ids()
    if settings.monday_mode == "live":
        config = MondayConfig(
            api_token=settings.monday_api_token,
            board_id=settings.monday_board_id,
            group_id=settings.monday_group_id,
            assignee_column=settings.monday_assignee_column,
            assignee_column_type=settings.monday_assignee_column_type,
            task_name_column=settings.monday_task_name_column,
            idempotency_column=settings.monday_idempotency_column,
            requester_column=settings.monday_requester_column,
            api_url=settings.monday_api_url,
            api_version=settings.monday_api_version,
            timeout_seconds=settings.monday_timeout_seconds,
        )
        registry.register(
            MondayGraphQLConnector(
                config, person_ids, requester_ids=settings.monday_requester_ids()
            )
        )
    else:
        # Mock mode: every directory person gets a fake id unless a real one is set.
        fake_ids = {p.key: 50_000_000 + i for i, p in enumerate(directory)}
        registry.register(
            MockMondayConnector(fake_ids | person_ids, fail_times=settings.mock_monday_fail_times)
        )
    # Future connectors (WhatsApp, email, calendar, CRM, ...) are registered here.
    return registry


def build_commands(settings: Settings) -> CommandRegistry:
    commands = CommandRegistry()
    commands.register(CreateTaskHandler(connector=settings.task_connector))
    # Future commands: commands.register(SendMessageHandler(connector="whatsapp")) ...
    return commands


def build_container(
    settings: Settings,
    *,
    queue: JobQueue | None = None,
    connectors: ConnectorRegistry | None = None,
    acks: AcknowledgmentGenerator | None = None,
    parser: IntentParser | None = None,
) -> Container:
    db = Database(settings.sqlite_path)
    db.migrate()
    store = JobStore(db)
    session_log = SessionLog(db)
    queue = queue or RedisJobQueue.from_url(
        settings.redis_url, namespace=settings.queue_name, worker_id=settings.worker_id
    )
    jobs = JobService(store, queue, max_attempts=settings.job_max_retries + 1)
    directory = PeopleDirectory.load(settings.people_directory_file or None)
    callers = CallerDirectory.load(settings.caller_directory_file, {p.key for p in directory})
    parser = parser or EgyptianArabicParser(directory)
    acks = acks or TemplateAcknowledgmentGenerator()
    commands = build_commands(settings)
    connectors = connectors or build_connectors(settings, directory)
    agent = (
        StructuredTaskAgent(
            api_key=settings.agent_api_key,
            model=settings.agent_model,
            directory=directory,
            timeout_seconds=settings.agent_timeout_seconds,
        )
        if settings.agent_api_key
        else None
    )
    engine = ConversationEngine(
        parser=parser,
        acks=acks,
        commands=commands,
        connectors=connectors,
        jobs=jobs,
        session_log=session_log,
        agent=agent,
        directory=directory,
    )
    return Container(
        settings=settings,
        db=db,
        store=store,
        session_log=session_log,
        queue=queue,
        jobs=jobs,
        directory=directory,
        callers=callers,
        parser=parser,
        acks=acks,
        commands=commands,
        connectors=connectors,
        engine=engine,
        agent=agent,
    )


def build_worker(container: Container) -> Worker:
    s = container.settings
    return Worker(
        store=container.store,
        queue=container.queue,
        connectors=container.connectors,
        retry=RetryPolicy(
            base_seconds=s.job_backoff_base_seconds, max_seconds=s.job_backoff_max_seconds
        ),
        execution_timeout=s.job_execution_timeout_seconds,
        lease_seconds=s.job_lease_seconds,
        reconcile_interval=s.job_reconcile_interval_seconds,
    )
