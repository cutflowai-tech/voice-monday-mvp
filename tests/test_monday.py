"""Monday connector: request construction, error classification, idempotency."""

from __future__ import annotations

import json

import httpx
import pytest

from voice_agent.connectors.base import (
    ExecutionContext,
    PermanentConnectorError,
    TransientConnectorError,
)
from voice_agent.connectors.monday import MondayConfig, MondayGraphQLConnector


def test_verified_requester_goes_to_people_column_only_when_configured():
    config = MondayConfig(
        api_token="test",
        board_id="1",
        group_id="g",
        assignee_column="assigned",
        requester_column="person",
    )
    connector = MondayGraphQLConnector(config, {"mohamed": 11}, requester_ids={"ahmed": 22})
    values = connector.column_values(
        title="شراء هارد", person_id=11, idempotency_key="K", requester_id=22
    )
    assert values["person"] == {"personsAndTeams": [{"id": 22, "kind": "person"}]}
    unknown = connector.column_values(title="شراء هارد", person_id=11, idempotency_key="K")
    assert "person" not in unknown


CONFIG = MondayConfig(
    api_token="test-token",
    board_id="1234567890",
    group_id="topics",
    assignee_column="person",
    task_name_column="name",
    api_version="2026-07",
    timeout_seconds=5,
)
PAYLOAD = {"title": "شراء هارد للسيرفر", "assignee": "Mohamed", "assignee_key": "mohamed"}


def ctx(attempt: int = 1, uncertain: bool = False) -> ExecutionContext:
    return ExecutionContext(
        job_id="job-1",
        idempotency_key="vc_abc123",
        attempt=attempt,
        created_at=0,
        prior_attempt_uncertain=uncertain,
    )


def ok_item(item_id: str = "987", replayed: bool = False) -> httpx.Response:
    headers = {"Idempotency-Replayed": "true"} if replayed else {}
    body = {"data": {"create_item": {"id": item_id, "name": "شراء هارد للسيرفر", "url": "u"}}}
    return httpx.Response(200, json=body, headers=headers)


def make(handler, config: MondayConfig = CONFIG) -> MondayGraphQLConnector:
    return MondayGraphQLConnector(config, {"mohamed": 4242}, transport=httpx.MockTransport(handler))


async def test_request_construction():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok_item()

    connector = make(handler)
    result = await connector.execute("create_task", PAYLOAD, ctx())

    request = seen[0]
    assert str(request.url) == "https://api.monday.com/v2"
    assert request.headers["Authorization"] == "test-token"
    assert request.headers["API-Version"] == "2026-07"
    assert request.headers["Idempotency-Key"] == "vc_abc123"
    body = json.loads(request.content)
    assert "create_item(board_id: $boardId" in body["query"]
    variables = body["variables"]
    assert variables["boardId"] == "1234567890"
    assert variables["groupId"] == "topics"
    assert variables["itemName"] == "شراء هارد للسيرفر"
    assert json.loads(variables["columnValues"]) == {
        "person": {"personsAndTeams": [{"id": 4242, "kind": "person"}]}
    }
    assert result.external_id == "987"
    assert result.data["item_id"] == "987"


async def test_optional_text_and_idempotency_columns():
    config = MondayConfig(
        **{**CONFIG.__dict__, "task_name_column": "text_task", "idempotency_column": "text_key"}
    )
    connector = make(lambda r: ok_item(), config)
    values = connector.column_values(title="T", person_id=1, idempotency_key="K")
    assert values["text_task"] == "T"
    assert values["text_key"] == "K"


async def test_dropdown_assignee_column_uses_option_ids():
    config = MondayConfig(
        **{**CONFIG.__dict__, "assignee_column": "dropdown_x", "assignee_column_type": "dropdown"}
    )
    seen = {}

    def handler(request):
        seen["values"] = json.loads(json.loads(request.content)["variables"]["columnValues"])
        return ok_item()

    connector = make(handler, config)
    await connector.execute("create_task", PAYLOAD, ctx())
    assert seen["values"] == {"dropdown_x": {"ids": [4242]}}


async def test_inspect_lists_dropdown_options():
    typed = {
        "labels": [
            {"id": 2, "label": "Mohamed", "is_deactivated": False},
            {"id": 6, "label": "Ahmed", "is_deactivated": False},
            {"id": 7, "label": "Old", "is_deactivated": True},
        ]
    }
    legacy = {"labels": [{"id": 2, "name": "Mohamed"}]}
    columns = [
        {"id": "dropdown_x", "title": "Assigned To", "type": "dropdown", "settings": typed},
        {"id": "person", "title": "Owner", "type": "people", "settings": {}},
        {"id": "dd_str", "title": "Old", "type": "dropdown", "settings": json.dumps(legacy)},
    ]

    def handler(request):
        assert "settings" in json.loads(request.content)["query"]
        return httpx.Response(200, json={"data": {"boards": [{"columns": columns}]}})

    data = await make(handler).inspect()
    cols = {c["id"]: c for c in data["boards"][0]["columns"]}
    assert cols["dropdown_x"]["options"] == [
        {"id": 2, "name": "Mohamed"},
        {"id": 6, "name": "Ahmed"},
    ]
    assert cols["dd_str"]["options"][0] == {"id": 2, "name": "Mohamed"}
    assert "options" not in cols["person"] and "settings" not in cols["person"]


async def test_person_id_is_not_hardcoded():
    connector = make(lambda r: ok_item())
    issues = connector.validate("create_task", PAYLOAD | {"assignee_key": "ahmed"})
    assert [i.code for i in issues] == ["unknown_person"]
    with pytest.raises(PermanentConnectorError):
        await connector.execute("create_task", PAYLOAD | {"assignee_key": "ahmed"}, ctx())


@pytest.mark.parametrize(
    ("response", "error_type", "retry_after", "uncertain"),
    [
        (httpx.Response(429, headers={"Retry-After": "12"}, json={"errors": []}), TransientConnectorError, 12.0, False),
        (httpx.Response(500, text="oops"), TransientConnectorError, None, True),
        (httpx.Response(503, text="down"), TransientConnectorError, None, True),
        (httpx.Response(423, json={"errors": [{"message": "locked"}]}), TransientConnectorError, None, False),
        (
            httpx.Response(
                409,
                headers={"Retry-After": "3"},
                json={"errors": [{"message": "dup", "extensions": {"code": "IDEMPOTENCY_CONFLICT"}}]},
            ),
            TransientConnectorError, 3.0, False,
        ),
        (httpx.Response(401, json={"errors": [{"message": "Unauthorized"}]}), PermanentConnectorError, None, False),
        (httpx.Response(403, text="forbidden"), PermanentConnectorError, None, False),
        (httpx.Response(400, json={"errors": [{"message": "Bad request"}]}), PermanentConnectorError, None, False),
    ],
)  # fmt: skip
async def test_http_errors_are_classified(response, error_type, retry_after, uncertain):
    connector = make(lambda r: response)
    with pytest.raises(error_type) as info:
        await connector.execute("create_task", PAYLOAD, ctx())
    assert info.value.retry_after == retry_after
    assert info.value.uncertain == uncertain


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        ("ColumnValueException", PermanentConnectorError),
        ("InvalidBoardIdException", PermanentConnectorError),
        ("InvalidColumnIdException", PermanentConnectorError),
        ("ResourceNotFoundException", PermanentConnectorError),
        ("COMPLEXITY_BUDGET_EXHAUSTED", TransientConnectorError),
        ("maxConcurrencyExceeded", TransientConnectorError),
        ("API_TEMPORARILY_BLOCKED", TransientConnectorError),
    ],
)
async def test_graphql_errors_are_classified(code, error_type):
    body = {"data": None, "errors": [{"message": "x", "extensions": {"code": code}}]}
    connector = make(lambda r: httpx.Response(200, json=body))
    with pytest.raises(error_type) as info:
        await connector.execute("create_task", PAYLOAD, ctx())
    assert info.value.code == code


async def test_legacy_error_format():
    body = {"error_code": "InvalidUserIdException", "error_message": "bad user"}
    connector = make(lambda r: httpx.Response(200, json=body))
    with pytest.raises(PermanentConnectorError):
        await connector.execute("create_task", PAYLOAD, ctx())


async def test_partial_errors_with_created_item_are_success():
    body = {
        "data": {"create_item": {"id": "55", "name": "x", "url": "u"}},
        "errors": [{"message": "warning", "extensions": {"code": "CorrectedValueException"}}],
    }
    connector = make(lambda r: httpx.Response(200, json=body))
    result = await connector.execute("create_task", PAYLOAD, ctx())
    assert result.external_id == "55"


async def test_timeouts_are_retryable_and_flagged_uncertain():
    def read_timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def connect_error(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TransientConnectorError) as info:
        await make(read_timeout).execute("create_task", PAYLOAD, ctx())
    assert info.value.uncertain  # Monday might have created the item

    with pytest.raises(TransientConnectorError) as info:
        await make(connect_error).execute("create_task", PAYLOAD, ctx())
    assert not info.value.uncertain  # request never left


async def test_retry_reuses_idempotency_key_and_reports_replay():
    keys = []

    def handler(request):
        keys.append(request.headers["Idempotency-Key"])
        if len(keys) == 1:
            raise httpx.ReadTimeout("lost response", request=request)
        return ok_item("777", replayed=True)

    connector = make(handler)
    with pytest.raises(TransientConnectorError):
        await connector.execute("create_task", PAYLOAD, ctx(1))
    result = await connector.execute("create_task", PAYLOAD, ctx(2, uncertain=True))
    assert keys == ["vc_abc123", "vc_abc123"]
    assert result.data["idempotency_replayed"] is True


async def test_uncertain_retry_finds_existing_item_via_key_column():
    config = MondayConfig(**{**CONFIG.__dict__, "idempotency_column": "text_key"})
    queries = []

    def handler(request):
        body = json.loads(request.content)
        queries.append(body["query"])
        assert "items_page_by_column_values" in body["query"]
        assert body["variables"] == {
            "boardId": "1234567890",
            "columnId": "text_key",
            "value": "vc_abc123",
        }
        items = [{"id": "31337", "name": "شراء هارد للسيرفر", "url": "u"}]
        return httpx.Response(200, json={"data": {"items_page_by_column_values": {"items": items}}})

    result = await make(handler, config).execute("create_task", PAYLOAD, ctx(2, uncertain=True))
    assert result.external_id == "31337"
    assert result.data["recovered_existing_item"] is True
    assert len(queries) == 1  # no second create_item


async def test_missing_item_id_is_retryable():
    connector = make(lambda r: httpx.Response(200, json={"data": {"create_item": None}}))
    with pytest.raises(TransientConnectorError):
        await connector.execute("create_task", PAYLOAD, ctx())
