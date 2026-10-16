"""Tasks on several Monday boards: Request tasks, Account Managers, Senior Account Managers."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from voice_agent.agent_tools.window import TaskWindow
from voice_agent.connectors.base import ConnectorRegistry, ExecutionContext
from voice_agent.connectors.monday import MockMondayConnector, MondayConfig, MondayGraphQLConnector
from voice_agent.container import board_targets
from voice_agent.domain.boards import ASK, BoardDirectory
from voice_agent.domain.dates import due_date

from .conftest import PERSON_IDS
from .test_agent_tools import running_worker

BOARDS_FILE = str(Path(__file__).resolve().parents[1] / "config" / "boards.json")
THURSDAY = dt.date(2026, 10, 15)


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("اشتري كاميرات بكرة", dt.date(2026, 10, 16)),
        ("ييجي المكتب بعد بكرة", dt.date(2026, 10, 17)),
        ("خلص الريبورت النهاردة", THURSDAY),
        ("سلم الفيديو يوم السبت", dt.date(2026, 10, 17)),
        ("اجتماع يوم الخميس", THURSDAY),  # same day name: today
        ("اتصل بالعميل", None),
    ],
)
def test_due_dates_from_egyptian_time_words(words, expected):
    assert due_date(words, today=THURSDAY) == expected


@pytest.fixture
def boards() -> BoardDirectory:
    return BoardDirectory.load(BOARDS_FILE)


def test_board_rules(boards):
    assert boards.board_keys() == ["requests", "account_managers", "senior_account_managers"]
    assert boards.resolve("amir", "auto") == ASK  # Sarah must ask
    assert boards.resolve("ziad", "auto") == "requests"  # everyone else: the usual board
    assert boards.resolve("ziad", "account_managers") == "account_managers"  # named board
    assert boards.monday_users["mohamed"] == 104901271  # Mohamed Mohsen


def ctx() -> ExecutionContext:
    return ExecutionContext(
        job_id="j1",
        idempotency_key="key-1",
        attempt=1,
        created_at="t",
        prior_attempt_uncertain=False,
    )


async def test_account_managers_board_gets_its_own_columns(boards):
    seen = {}

    def handler(request):
        seen["variables"] = json.loads(request.content)["variables"]
        return httpx.Response(200, json={"data": {"create_item": {"id": "77", "name": "x"}}})

    config = MondayConfig(
        api_token="t",
        board_id="5105567103",
        group_id="topics",
        assignee_column="dropdown_mm7wmgq3",
        assignee_column_type="dropdown",
    )
    connector = MondayGraphQLConnector(
        config,
        {"ziad": 5},
        boards=board_targets(boards, {"ziad": 5}),
        transport=httpx.MockTransport(handler),
    )
    payload = {
        "title": "راجع البورد",
        "assignee": "Ziad",
        "assignee_key": "ziad",
        "board": "account_managers",
        "due": "2026-10-16",
    }
    await connector.execute("create_task", payload, ctx())
    variables = seen["variables"]
    assert variables["boardId"] == "5098517650" and variables["groupId"] == "group_mm2qmjvq"
    assert json.loads(variables["columnValues"]) == {
        "multiple_person_mm33zmm3": {"personsAndTeams": [{"id": 105392408, "kind": "person"}]},
        "color_mm2qe34s": {"index": 2},  # Status: Ready To Start
        "status": {"index": 3},  # Priority: Medium
        "timerange_mm2qg82": {"from": "2026-10-16", "to": "2026-10-16"},
    }

    await connector.execute("create_task", {**payload, "board": "requests"}, ctx())
    assert seen["variables"]["boardId"] == "5105567103"  # the usual board, unchanged
    assert json.loads(seen["variables"]["columnValues"]) == {"dropdown_mm7wmgq3": {"ids": [5]}}


@pytest.fixture
def board_container(container, boards):
    ids = PERSON_IDS | {"amir": 4}
    mock = MockMondayConnector(ids, boards=board_targets(boards, ids, mock=True))
    registry = ConnectorRegistry()
    registry.register(mock)
    container.connectors = registry
    container.boards = boards
    container.mock = mock
    return container


async def test_amir_needs_a_board_before_anything_is_saved(board_container):
    window = TaskWindow(board_container, grace_seconds=0.01, done_wait_seconds=0.05)
    reply = await window.submit(conversation_id="c1", assignee_key="amir", task="راجع البوردات")
    assert reply["status"] == "need_board" and reply["assignee"] == "أمير"
    assert [o["board"] for o in reply["options"]] == boards_keys()
    assert board_container.store.status_counts() == {}


def boards_keys():
    return ["requests", "account_managers", "senior_account_managers"]


async def test_named_board_creates_the_task_there_with_a_due_date(board_container, worker):
    window = TaskWindow(board_container, grace_seconds=0.01, done_wait_seconds=1, poll_seconds=0.01)
    async with running_worker(worker):
        result = await window.submit(
            conversation_id="c2",
            assignee_key="amir",
            task="يراجع البوردات بكرة",
            board="senior_account_managers",
        )
    assert result["status"] == "created" and result["board"] == "السينيور أكاونت مانجرز"
    call = board_container.mock.calls[-1]
    assert call["board"] == "senior_account_managers" and call["due"] == result["due"]
    assert call["title"] == "راجع البوردات بكرة"


async def test_others_go_to_the_usual_board(board_container, worker):
    window = TaskWindow(board_container, grace_seconds=0.01, done_wait_seconds=1, poll_seconds=0.01)
    async with running_worker(worker):
        result = await window.submit(conversation_id="c3", assignee_key="ahmed", task="يجيب الورق")
    assert result["status"] == "created" and result["board"] == "الريكويستس"
    assert "due" not in result  # the usual board has no timeline column
    assert board_container.mock.calls[-1]["board"] is None


async def test_unknown_board_is_rejected(board_container):
    window = TaskWindow(board_container, grace_seconds=0.01)
    reply = await window.submit(
        conversation_id="c4", assignee_key="ahmed", task="مهمة", board="marketing"
    )
    assert reply["status"] == "invalid"


def test_sarah_knows_the_boards_and_asks_for_amir(boards):
    from voice_agent.domain.people import PeopleDirectory
    from voice_agent.elevenlabs_agent import build_tools
    from voice_agent.sarah import build_prompt

    people = [p for p in PeopleDirectory.load() if p.key in boards.monday_users]
    prompt = build_prompt(people, expressive=False, boards=boards)
    assert "# Boards" in prompt and "أحطها لـأمير على أنهي بورد؟" in prompt
    assert "الأكاونت مانجرز" in prompt and "السينيور أكاونت مانجرز" in prompt
    assert "# Boards" not in build_prompt(people, expressive=False)  # single board: no section

    from voice_agent.config import Settings

    tools = build_tools(Settings(agent_public_url="https://x"), people, "sec", boards=boards)
    board = tools["submit_task"]["api_schema"]["request_body_schema"]["properties"]["board"]
    assert board["enum"] == ["auto", *boards_keys()]
