from __future__ import annotations

import random

import pytest

from voice_agent.ack.templates import TemplateAcknowledgmentGenerator
from voice_agent.config import Settings
from voice_agent.connectors.base import ConnectorRegistry
from voice_agent.connectors.monday import MockMondayConnector
from voice_agent.container import Container, build_container, build_worker
from voice_agent.domain.people import PeopleDirectory
from voice_agent.jobs.queue import InMemoryJobQueue
from voice_agent.jobs.worker import Worker
from voice_agent.nlu.egyptian_parser import EgyptianArabicParser

EXAMPLE = "حطي تاسك لمحمد إنه يشتري هارد للسيرفر"
PERSON_IDS = {"mohamed": 111, "ahmed": 222, "sara": 333}


@pytest.fixture
def directory() -> PeopleDirectory:
    return PeopleDirectory.load()


@pytest.fixture
def parser(directory: PeopleDirectory) -> EgyptianArabicParser:
    return EgyptianArabicParser(directory)


@pytest.fixture
def acks() -> TemplateAcknowledgmentGenerator:
    return TemplateAcknowledgmentGenerator(random.Random(7))


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        monday_mode="mock",
        job_backoff_base_seconds=0.01,
        job_backoff_max_seconds=0.05,
        job_reconcile_interval_seconds=0.05,
    )


@pytest.fixture
def mock_monday() -> MockMondayConnector:
    return MockMondayConnector(PERSON_IDS)


@pytest.fixture
def container(settings: Settings, mock_monday: MockMondayConnector, acks) -> Container:
    connectors = ConnectorRegistry()
    connectors.register(mock_monday)
    return build_container(settings, queue=InMemoryJobQueue(), connectors=connectors, acks=acks)


@pytest.fixture
def worker(container: Container) -> Worker:
    return build_worker(container)
