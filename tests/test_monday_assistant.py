"""Sarah's Monday assistant: an LLM with Monday tools answers anything about Monday."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from voice_agent.agent_tools.monday_assistant import MondayAssistant
from voice_agent.api.app import create_app
from voice_agent.config import Settings
from voice_agent.connectors.monday_api import MondayAPI, MondayTools, blocked_reason
from voice_agent.domain.callers import CallerDirectory
from voice_agent.domain.people import PeopleDirectory

SECRET = "s" * 40
BOARDS = [
    {
        "id": "5098517650",
        "name": "Account Managers Tasks",
        "type": "board",
        "items_count": 2,
        "workspace": {"name": "Main"},
    },
    {
        "id": "77",
        "name": "Subitems of X",
        "type": "sub_items_board",
        "items_count": 1,
        "workspace": None,
    },
]
ITEMS = [
    {
        "id": "1",
        "name": "Shoot reel",
        "group": {"id": "g1", "title": "This week"},
        "column_values": [
            {"id": "status", "text": "Stuck", "column": {"title": "Status"}},
            {"id": "person", "text": "Ziad", "column": {"title": "Owner"}},
            {"id": "notes", "text": "", "column": {"title": "Notes"}},
        ],
    },
]


class FakeMonday:
    """Answers the GraphQL documents MondayTools sends; records every request."""

    def __init__(self):
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        query = body["query"]
        if "boards(limit" in query:
            data = {"boards": BOARDS}
        elif "users(" in query:
            data = {
                "users": [
                    {"id": "105392408", "name": "Ziad", "email": "z@x"},
                ]
            }
        elif "items_page" in query:
            data = {"boards": [{"items_page": {"cursor": None, "items": ITEMS}}]}
        elif "columns{" in query:
            data = {
                "boards": [
                    {
                        "id": "5098517650",
                        "name": "AM",
                        "groups": [],
                        "columns": [
                            {
                                "id": "status",
                                "title": "Status",
                                "type": "status",
                                "settings": {
                                    "labels": [
                                        {"id": 1, "label": "Done"},
                                        {"id": 2, "label": "Stuck"},
                                        {"id": 3, "label": "Old", "is_deactivated": True},
                                    ]
                                },
                            },
                            {"id": "name", "title": "Name", "type": "name", "settings": {}},
                        ],
                    }
                ]
            }
        elif "change_multiple_column_values" in query:
            data = {"change_multiple_column_values": {"id": "1", "name": "Shoot reel"}}
        else:
            data = {}
        return httpx.Response(200, json={"data": data})


def tools_with(fake: FakeMonday) -> MondayTools:
    return MondayTools(MondayAPI("tok", transport=httpx.MockTransport(fake)))


@pytest.mark.parametrize(
    ("document", "allowed"),
    [
        ("query { boards { id } }", True),
        ('query { items(ids:[1]) { name } }  # "delete_item" in a comment string', True),
        ('mutation { create_group(board_id: 1, group_name: "x") { id } }', True),
        ("mutation { delete_item(item_id: 1) { id } }", False),
        ("mutation { archive_board(board_id: 1) { id } }", False),
        ("mutation { bulk_delete_items(item_ids: [1]) { id } }", False),
        ('mutation { create_team(input: {name: "x"}) { id } }', False),
        ("mutation { regenerate_service_user_token(user_id: 1) { token } }", False),
        ("mutation { add_users_to_board(board_id: 1, user_ids: [2]) { id } }", False),
        ('mutation { create_update(item_id: 1, body: "please delete_item later") { id } }', True),
    ],
)
def test_escape_hatch_refuses_deletions_and_account_changes(document, allowed):
    assert (blocked_reason(document) is None) is allowed


async def test_tools_read_boards_items_and_labels():
    fake = FakeMonday()
    tools = tools_with(fake)
    world = await tools.workspace_map()
    assert "Account Managers Tasks" in world and "Subitems of X" not in world
    assert "Ziad" in world
    assert any("kind:non_guests" in r["query"] for r in fake.requests)

    board = json.loads(await tools.call("get_board", {"board_id": "5098517650"}))
    assert board["columns"][0]["labels"] == ["Done", "Stuck"]  # deactivated label hidden

    items = json.loads(await tools.call("get_items", {"board_id": "5098517650", "search": "reel"}))
    assert items["items"][0]["columns"] == {"Status": "Stuck", "Owner": "Ziad"}
    rules = fake.requests[-1]["variables"]["qp"]["rules"]
    ITEMS.append(
        {
            "id": "2",
            "name": "Old",
            "group": {"id": "g2", "title": "Resolved"},
            "column_values": [{"id": "status", "text": "Done", "column": {"title": "Status"}}],
        }
    )
    try:
        open_items = json.loads(
            await tools.call("get_items", {"board_id": "5098517650", "open_only": True})
        )
    finally:
        ITEMS.pop()
    assert [i["name"] for i in open_items["items"]] == ["Shoot reel"]
    assert open_items["finished_skipped"] == 1
    assert rules == [{"column_id": "name", "compare_value": ["reel"], "operator": "contains_text"}]

    refused = json.loads(
        await tools.call("graphql", {"query": "mutation{delete_board(board_id:1){id}}"})
    )
    assert "refused" in refused["error"]
    assert "delete_board" not in json.dumps(fake.requests)  # never sent to Monday


async def test_tool_errors_come_back_as_text_for_the_llm():
    def broken(request):
        return httpx.Response(200, json={"errors": [{"message": "Column not found"}]})

    tools = MondayTools(MondayAPI("tok", transport=httpx.MockTransport(broken)))
    out = json.loads(
        await tools.call("update_item", {"board_id": "1", "item_id": "2", "values": {}})
    )
    assert out == {"error": "Column not found"}
    assert "unknown tool" in await tools.call("drop_tables", {})


class FakeLLM:
    """A scripted chat-completions endpoint: each reply is a tool call list or final text."""

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        reply = self.replies.pop(0)
        if isinstance(reply, list):
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                    for i, (name, args) in enumerate(reply)
                ],
            }
        else:
            message = {"role": "assistant", "content": reply}
        return httpx.Response(200, json={"choices": [{"message": message}]})


class Signals:
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []

    async def set_saving(self, conversation_id, on):
        self.calls.append((conversation_id, on))


def assistant_with(llm: FakeLLM, fake: FakeMonday | None = None, **kw) -> MondayAssistant:
    directory = PeopleDirectory.load()
    return MondayAssistant(
        tools=tools_with(fake or FakeMonday()),
        api_key="k",
        model="gpt-4.1",
        allowed={"mohamed"},
        callers=CallerDirectory({"101": "mohamed", "102": "ahmed"}, {p.key for p in directory}),
        directory=directory,
        transport=httpx.MockTransport(llm),
        **kw,
    )


async def test_answers_with_tools_and_remembers_the_call():
    fake = FakeMonday()
    llm = FakeLLM(
        [
            [("get_items", {"board_id": "5098517650", "search": "reel"})],
            "عند زياد تاسك واحدة واقفة: Shoot reel.",
            [
                (
                    "update_item",
                    {
                        "board_id": "5098517650",
                        "item_id": "1",
                        "values": {"status": {"label": "Done"}},
                    },
                )
            ],
            "خلاص، خليتها Done.",
        ]
    )
    signals = Signals()
    assistant = assistant_with(llm, fake, activity=signals)

    first = await assistant.ask(
        conversation_id="c1", request="إيه اللي واقف عند زياد؟", caller_id="101"
    )
    assert first == {"status": "ok", "answer": "عند زياد تاسك واحدة واقفة: Shoot reel."}
    system = llm.bodies[0]["messages"][0]["content"]
    assert "Account Managers Tasks" in system and "زياد = Ziad" in system
    assert signals.calls == [("c1", True), ("c1", False)]  # typing sound on, then off

    second = await assistant.ask(conversation_id="c1", request="خليها Done", caller_id="101")
    assert second["answer"] == "خلاص، خليتها Done."
    remembered = [m.get("content") for m in llm.bodies[2]["messages"]]
    assert "إيه اللي واقف عند زياد؟" in remembered  # the follow-up knows what «ها» is
    sent = json.loads(fake.requests[-1]["variables"]["v"])
    assert sent == {"status": {"label": "Done"}}
    await assistant.aclose()


async def test_only_allowed_callers_get_monday_access():
    llm = FakeLLM([])
    assistant = assistant_with(llm)
    assert await assistant.ask(conversation_id="c", request="x", caller_id="102") == {
        "status": "not_allowed"
    }
    assert await assistant.ask(conversation_id="c", request="x", caller_id=None) == {
        "status": "not_allowed"
    }
    assert llm.bodies == []
    await assistant.aclose()


async def test_step_limit_forces_an_answer_and_timeout_fails_cleanly():
    llm = FakeLLM([[("search_items", {"text": "a"})]] * 2 + ["لقيت حاجات كتير، قولي أركز على إيه."])
    assistant = assistant_with(llm, max_steps=3)
    result = await assistant.ask(conversation_id="c", request="دور", caller_id="101")
    assert result["status"] == "ok"
    assert llm.bodies[-1]["tool_choice"] == "none"

    def slow(request):
        raise httpx.ReadTimeout("slow")

    signals = Signals()
    slow_assistant = MondayAssistant(
        tools=tools_with(FakeMonday()),
        api_key="k",
        model="m",
        allowed={"mohamed"},
        callers=CallerDirectory({"101": "mohamed"}, {p.key for p in PeopleDirectory.load()}),
        directory=PeopleDirectory.load(),
        transport=httpx.MockTransport(slow),
        activity=signals,
    )
    assert await slow_assistant.ask(conversation_id="c", request="x", caller_id="101") == {
        "status": "failed"
    }
    assert signals.calls[-1] == ("c", False)  # typing stops even on failure

    stuck = assistant_with(FakeLLM([]), timeout_seconds=0.05)

    async def hang(*_args, **_kw):
        await asyncio.sleep(1)

    stuck._answer = hang
    assert (await stuck.ask(conversation_id="c", request="x", caller_id="101"))[
        "reason"
    ] == "timeout"


def test_endpoint_needs_the_bearer_and_reports_unavailable(container):
    container.settings.agent_tool_secret = SECRET
    with TestClient(create_app(container=container)) as client:
        body = {"conversation_id": "c1", "request": "راجعيلي البورد"}
        assert client.post("/agent/tools/monday_assistant", json=body).status_code == 401
        ok = client.post(
            "/agent/tools/monday_assistant",
            json=body,
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert ok.json() == {"status": "unavailable"}  # no AGENT_API_KEY in tests


def test_sarah_gets_the_tool_and_the_prompt_section():
    from voice_agent.elevenlabs_agent import TOOL_MONDAY, build_tools
    from voice_agent.sarah import build_prompt

    people = list(PeopleDirectory.load())
    settings = Settings(agent_public_url="https://x")
    assert TOOL_MONDAY not in build_tools(settings, people, "sec")
    tool = build_tools(settings, people, "sec", monday_assistant=True)[TOOL_MONDAY]
    assert tool["api_schema"]["url"] == "https://x/agent/tools/monday_assistant"
    assert tool["response_timeout_secs"] == 60

    prompt = build_prompt(people, expressive=False, monday_assistant=True)
    assert "monday_assistant" in prompt and "only help with Monday" in prompt
    plain = build_prompt(people, expressive=False)
    assert "monday_assistant" not in plain and "only take tasks for Monday" in plain
    assert "{monday}" not in plain and "{other}" not in plain


def test_assistant_model_defaults():
    assert Settings().monday_assistant_llm() == "gpt-4.1"
    assert Settings(agent_provider="openrouter").monday_assistant_llm() == "openai/gpt-4.1"
    assert Settings(monday_assistant_callers=" Mohamed, amir ").monday_assistant_allowed() == {
        "mohamed",
        "amir",
    }


async def test_map_is_kept_warm_and_sounds_are_elevenlabs():
    from voice_agent.elevenlabs_agent import build_agent, build_tools

    fake = FakeMonday()
    assistant = assistant_with(FakeLLM([]), fake)
    assistant.start()
    await asyncio.sleep(0.05)
    assert any("boards(limit" in r["query"] for r in fake.requests)  # fetched before any call
    await assistant.aclose()

    people = list(PeopleDirectory.load())
    settings = Settings(agent_public_url="https://x")
    tools = build_tools(settings, people, "sec", monday_assistant=True)
    for tool in (tools["submit_task"], tools["monday_assistant"]):
        assert tool["tool_call_sound"] == "typing"
        assert tool["tool_call_sound_behavior"] == "always"
    assert tools["monday_assistant"]["pre_tool_speech"] == "off"  # no «ثواني أدور»
    config = build_agent(settings, people, [])["conversation_config"]
    assert config["turn"]["soft_timeout_config"] == {"timeout_seconds": -1}
    assert config["conversation"]["background_sound"]["source_id"] == "office2"
    quiet = Settings(elevenlabs_background_sound="", elevenlabs_tool_call_sound="")
    assert build_agent(quiet, people, [])["conversation_config"]["conversation"] == {
        "background_sound": {"source_type": None, "source_id": None}
    }
    assert build_tools(quiet, people, "sec")["submit_task"]["tool_call_sound"] is None


def _cv(title: str, kind: str, text: str | None) -> dict:
    return {"id": title.lower(), "text": text, "type": kind, "column": {"title": title}}


async def test_person_items_does_the_filtering_and_dates_itself():
    import datetime as dt

    small = [
        {
            "id": "1",
            "name": "Late one",
            "group": {"id": "g", "title": "Unresolved"},
            "column_values": [
                _cv("Priority", "status", "High"),
                _cv("Status", "status", "Unresolved"),
                _cv("Timeline", "timeline", "2026-10-10 - 2026-10-12"),
                _cv("Assign", "people", "Michael Aziz, amir mohamed"),
            ],
        },
        {
            "id": "2",
            "name": "Due today",
            "group": {"id": "g", "title": "Ready"},
            "column_values": [
                _cv("Status", "status", "Ready To Start"),
                _cv("Timeline", "timeline", "2026-10-16 - 2026-10-16"),
                _cv("Assign", "people", "Michael Aziz"),
            ],
        },
        {
            "id": "3",
            "name": "Done one",
            "group": {"id": "g", "title": "Ready"},
            "column_values": [
                _cv("Status", "status", "Resolved"),
                _cv("Timeline", "timeline", "2026-10-01 - 2026-10-01"),
                _cv("Assign", "people", "Michael Aziz"),
            ],
        },
        {
            "id": "4",
            "name": "Someone else",
            "group": {"id": "g", "title": "Ready"},
            "column_values": [
                _cv("Status", "status", "Ready To Start"),
                _cv("Assign", "people", "Ziad shams"),
            ],
        },
        {
            "id": "5",
            "name": "Group says done",
            "group": {"id": "g", "title": "Resolved"},
            "column_values": [_cv("Assign", "dropdown", "Michael")],
        },
    ]
    big = [
        {
            "id": "9",
            "name": "Project",
            "group": {"id": "g", "title": "Active"},
            "column_values": [
                _cv("Status", "status", "Sent"),
                _cv("Assign", "people", "Michael Aziz"),
            ],
        },
        {
            "id": "10",
            "name": "Open project",
            "group": {"id": "g", "title": "Active"},
            "column_values": [
                _cv("Requested ETA", "date", "2026-10-14 16:35"),
                _cv("Captions Editor", "status", "Karim"),
                _cv("Assign", "people", "Michael Aziz"),
            ],
        },
    ]
    seen = []

    def handler(request):
        body = json.loads(request.content)
        query, variables = body["query"], body["variables"]
        seen.append(body)
        if "boards(limit" in query:
            data = {
                "boards": [
                    {"id": "1", "name": "Michael Tasks", "type": "board", "items_count": 5},
                    {"id": "2", "name": "Customer Projects", "type": "board", "items_count": 1384},
                ]
            }
        elif "users(" in query:
            data = {
                "users": [
                    {"id": "101717680", "name": "michael", "email": "m"},
                    {"id": "99154857", "name": "Michael Aziz", "email": "a"},
                ]
            }
        elif "change_multiple_column_values" in query:
            data = {"change_multiple_column_values": {"id": "1", "name": "x"}}
        elif "next_items_page" in query:
            data = {"next_items_page": {"cursor": None, "items": big[1:]}}
        elif variables["id"] == ["1"]:
            data = {"boards": [{"items_page": {"cursor": None, "items": small}}]}
        else:  # the big board comes in pages
            data = {"boards": [{"items_page": {"cursor": "next", "items": big[:1]}}]}
        return httpx.Response(200, json={"data": data})

    tools = MondayTools(
        MondayAPI("tok", transport=httpx.MockTransport(handler)),
        today=lambda: dt.date(2026, 10, 16),
    )
    out = json.loads(await tools.call("person_items", {"person": "Michael Aziz"}))
    assert [i["name"] for i in out["items"]] == ["Late one", "Open project", "Due today"]
    assert out["count"] == 3 and out["late"] == 2 and out["due_today"] == 1
    assert out["items"][0] == {
        "board": "Michael Tasks",
        "name": "Late one",
        "status": "Unresolved",
        "due": "2026-10-12",
        "late_days": 4,
    }
    assert "types:[status,people,dropdown,date,timeline]" in seen[-1]["query"]  # light query

    before = len(seen)
    await tools.call("person_items", {"person": "Michael Aziz"})
    assert len(seen) == before  # served from the cache
    await tools.call("update_item", {"board_id": "1", "item_id": "1", "values": {}})
    await tools.call("person_items", {"person": "Michael Aziz"})
    assert sum("items_page" in r["query"] for r in seen[before:]) == 1  # only board 1 re-read


def test_answers_are_made_speakable():
    from voice_agent.agent_tools.monday_assistant import spoken

    listed = 'فيه ١٦ تاسك. أهمهم:\n\n- **الكاميرات** (Critical)\n- "الكهربائي"\n1. الفيديو'
    assert spoken(listed) == "فيه ١٦ تاسك. أهمهم، الكاميرات (Critical)، الكهربائي، الفيديو"
    first = " ".join(["أول"] * 30) + "."
    assert spoken(first + " " + " ".join(["كلمة"] * 80)) == f"{first} أكمل؟"  # sentence end
    cut = spoken(" ".join(["كلمة"] * 80))
    assert cut.endswith(". أكمل؟") and len(cut.split()) == 46  # no sentence end: 45 words
    assert spoken("تمام، خليتها Done.") == "تمام، خليتها Done."


def test_recognition_keywords_carry_monday_words_and_boards():
    from voice_agent.elevenlabs_agent import MAX_ASR_KEYWORDS, asr_keywords

    people = list(PeopleDirectory.load())
    words = asr_keywords(
        people, ["Customer Projects", "Ahmed Tasks", *[f"B{i}" for i in range(200)]]
    )
    assert {"Tasks", "pending", "in progress", "Monday", "Ahmed Tasks", "محمد"} <= set(words)
    assert len(words) == MAX_ASR_KEYWORDS and len(set(words)) == len(words)
