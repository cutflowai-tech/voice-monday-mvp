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
    BoardTarget,
    MockMondayConnector,
    MondayConfig,
    MondayGraphQLConnector,
)
from voice_agent.conversation.agent import StructuredTaskAgent
from voice_agent.conversation.engine import ConversationEngine
from voice_agent.conversation.session_log import SessionLog
from voice_agent.db import Database
from voice_agent.domain.boards import BoardDirectory
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
    boards: BoardDirectory
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


def board_targets(
    boards: BoardDirectory, default_ids: dict[str, int], *, mock: bool = False
) -> dict[str, BoardTarget]:
    """Monday settings for every extra board; People columns use Monday user ids."""
    targets = {}
    for key, spec in boards.extra().items():
        if mock:
            ids = default_ids
        elif spec.assignee_column_type == "people":
            ids = boards.monday_users
        else:
            ids = default_ids
        targets[key] = BoardTarget(
            board_id=str(spec.board_id),
            group_id=spec.group_id,
            assignee_column=spec.assignee_column,
            assignee_column_type=spec.assignee_column_type,
            person_ids=dict(ids),
            values=dict(spec.values),
            timeline_column=spec.timeline_column,
        )
    return targets


def build_connectors(
    settings: Settings, directory: PeopleDirectory, boards: BoardDirectory | None = None
) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    boards = boards or BoardDirectory.load(settings.monday_boards_file or None)
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
                config,
                person_ids,
                requester_ids=settings.monday_requester_ids(),
                boards=board_targets(boards, person_ids),
            )
        )
    else:
        # Mock mode: every directory person gets a fake id unless a real one is set.
        fake_ids = {p.key: 50_000_000 + i for i, p in enumerate(directory)} | person_ids
        registry.register(
            MockMondayConnector(
                fake_ids,
                fail_times=settings.mock_monday_fail_times,
                boards=board_targets(boards, fake_ids, mock=True),
            )
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
    boards = BoardDirectory.load(settings.monday_boards_file or None)
    connectors = connectors or build_connectors(settings, directory, boards)
    agent = (
        StructuredTaskAgent(
            api_key=settings.agent_api_key,
            provider=settings.agent_provider,
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
        review_before_submit=settings.review_before_submit,
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
        boards=boards,
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
