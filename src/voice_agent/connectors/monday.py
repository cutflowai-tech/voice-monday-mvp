"""Monday.com connector (GraphQL API v2).

API facts used here (checked against developer.monday.com, Oct 2026):

* ``POST https://api.monday.com/v2`` with ``Authorization: <token>`` and
  ``API-Version: YYYY-MM`` headers.
* ``create_item(board_id: ID!, group_id: String, item_name: String!, column_values: JSON)``;
  ``column_values`` is a JSON-encoded string.
* People column value: ``{"personsAndTeams": [{"id": 123, "kind": "person"}]}``.
* Dropdown column value: ``{"ids": [2]}`` (label ids, stable across renames).
* ``Column.settings`` (JSON) holds a dropdown's ``labels`` as ``{"id", "label",
  "is_deactivated"}`` (``settings_str`` is deprecated and used ``name``).
* ``Idempotency-Key`` header on mutations: the first response is cached for 30 minutes
  and replayed (``Idempotency-Replayed: true``) for retries with the same key; a
  concurrent duplicate gets ``409`` + ``IDEMPOTENCY_CONFLICT`` + ``Retry-After``.
* Application errors arrive as HTTP 200 with an ``errors`` array whose
  ``extensions.code`` identifies the problem; transport errors use 4xx/5xx.

Duplicate prevention layers:

1. SQLite: a completed job is never executed again; job ids are unique per
   voice-command idempotency key.
2. Monday ``Idempotency-Key`` header = job idempotency key (covers retries within
   30 minutes, which includes our whole backoff schedule).
3. Optional ``MONDAY_IDEMPOTENCY_COLUMN`` (a text column): the key is stored on the
   item, and when an earlier attempt's outcome is unknown the connector looks the
   item up before creating it again (covers worker crashes older than 30 minutes).
"""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from voice_agent.connectors.base import (
    Connector,
    ConnectorResult,
    ExecutionContext,
    PermanentConnectorError,
    TransientConnectorError,
    ValidationIssue,
)
from voice_agent.logging_setup import get_logger

log = get_logger(__name__)

CREATE_TASK = "create_task"
MAX_ITEM_NAME = 255

CREATE_ITEM_MUTATION = """
mutation CreateTask($boardId: ID!, $groupId: String, $itemName: String!, $columnValues: JSON) {
  create_item(board_id: $boardId, group_id: $groupId, item_name: $itemName,
              column_values: $columnValues) {
    id
    name
    url
  }
}
""".strip()

FIND_BY_KEY_QUERY = """
query FindTaskByKey($boardId: ID!, $columnId: String!, $value: String!) {
  items_page_by_column_values(limit: 5, board_id: $boardId,
      columns: [{column_id: $columnId, column_values: [$value]}]) {
    items { id name url }
  }
}
""".strip()

INSPECT_QUERY = """
query Inspect($boardId: [ID!]) {
  me { id name }
  boards(ids: $boardId) {
    id
    name
    groups { id title }
    columns { id title type settings }
  }
  users(limit: 200) { id name email }
}
""".strip()

TRANSIENT_ERROR_CODES = {
    "API_TEMPORARILY_BLOCKED",
    "maxConcurrencyExceeded",
    "COMPLEXITY_BUDGET_EXHAUSTED",
    "ComplexityException",
    "Rate Limit Exceeded",
    "RATE_LIMIT_EXCEEDED",
    "IP_RATE_LIMIT_EXCEEDED",
    "IDEMPOTENCY_CONFLICT",
    "INTERNAL_SERVER_ERROR",
    "Internal Server Error",
}
TRANSIENT_HTTP = {409, 423, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class MondayConfig:
    api_token: str
    board_id: str
    group_id: str
    assignee_column: str
    # "people": MONDAY_<NAME>_PERSON_ID is a Monday user id.
    # "dropdown": MONDAY_<NAME>_PERSON_ID is the id of that person's dropdown label.
    assignee_column_type: str = "people"
    task_name_column: str = "name"
    idempotency_column: str = ""
    requester_column: str = ""
    api_url: str = "https://api.monday.com/v2"
    api_version: str = "2026-07"
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class BoardTarget:
    """An extra board (beyond the default one in ``MondayConfig``) a task can go to."""

    board_id: str
    group_id: str
    assignee_column: str
    assignee_column_type: str  # "people" (user ids) or "dropdown" (option ids)
    person_ids: dict[str, int]
    values: dict[str, Any] = field(default_factory=dict)  # fixed values, e.g. status index
    timeline_column: str = ""  # set from the task's due date when there is one


class MondayConnector(Connector):
    """Monday-specific connector contract (real and mock implementations)."""

    name = "monday"
    operations = frozenset({CREATE_TASK})

    def __init__(
        self,
        person_ids: dict[str, int],
        requester_ids: dict[str, int] | None = None,
        boards: dict[str, BoardTarget] | None = None,
    ):
        self.person_ids = dict(person_ids)
        self.requester_ids = dict(requester_ids or {})
        self.boards = dict(boards or {})  # extra boards by key; no key = default board

    def ids_for(self, board: str | None) -> dict[str, int]:
        """Person ids for an extra board; anything else is the default board."""
        if board and board in self.boards:
            return self.boards[board].person_ids
        return self.person_ids

    def validate(self, operation: str, payload: dict[str, Any]) -> list[ValidationIssue]:
        issues = super().validate(operation, payload)
        if issues:
            return issues
        title = str(payload.get("title") or "").strip()
        if not title:
            issues.append(ValidationIssue("task", "missing", "task title is empty"))
        elif len(title) > MAX_ITEM_NAME:
            issues.append(ValidationIssue("task", "too_long", "task title exceeds 255 chars"))
        board = payload.get("board")
        ids = self.ids_for(board)
        key = payload.get("assignee_key")
        if not key or key not in ids:
            where = f" on board {board!r}" if board else ""
            issues.append(
                ValidationIssue(
                    "assignee",
                    "unknown_person",
                    f"no Monday id configured for {payload.get('assignee')!r}{where}",
                )
            )
        return issues

    async def execute(
        self, operation: str, payload: dict[str, Any], ctx: ExecutionContext
    ) -> ConnectorResult:
        issues = self.validate(operation, payload)
        if issues:
            raise PermanentConnectorError("; ".join(i.message for i in issues), code="validation")
        board = payload.get("board")
        return await self.create_task(
            title=payload["title"].strip(),
            person_id=self.ids_for(board)[payload["assignee_key"]],
            ctx=ctx,
            requester_id=self.requester_ids.get(payload.get("requester_key")),
            board=board if board in self.boards else None,
            due=payload.get("due"),
        )

    @abstractmethod
    async def create_task(
        self,
        *,
        title: str,
        person_id: int,
        ctx: ExecutionContext,
        requester_id: int | None = None,
        board: str | None = None,
        due: str | None = None,
    ) -> ConnectorResult: ...


class MondayGraphQLConnector(MondayConnector):
    def __init__(
        self,
        config: MondayConfig,
        person_ids: dict[str, int],
        requester_ids: dict[str, int] | None = None,
        *,
        boards: dict[str, BoardTarget] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(person_ids, requester_ids, boards)
        self.config = config
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds, connect=5.0),
            transport=transport,
        )

    # ------------------------------------------------------------------ requests
    def column_values(
        self,
        *,
        title: str,
        person_id: int,
        idempotency_key: str,
        requester_id: int | None = None,
        board: str | None = None,
        due: str | None = None,
    ) -> dict:
        target = self.boards.get(board) if board else None
        assignee_type = target.assignee_column_type if target else self.config.assignee_column_type
        if assignee_type == "dropdown":
            assignee: dict[str, Any] = {"ids": [person_id]}
        else:
            assignee = {"personsAndTeams": [{"id": person_id, "kind": "person"}]}
        if target is not None:
            values: dict[str, Any] = {target.assignee_column: assignee, **target.values}
            if target.timeline_column and due:
                values[target.timeline_column] = {"from": due, "to": due}
            return values
        values = {self.config.assignee_column: assignee}
        if self.config.task_name_column and self.config.task_name_column != "name":
            values[self.config.task_name_column] = title
        if self.config.idempotency_column:
            values[self.config.idempotency_column] = idempotency_key
        if self.config.requester_column and requester_id is not None:
            values[self.config.requester_column] = {
                "personsAndTeams": [{"id": requester_id, "kind": "person"}]
            }
        return values

    def build_create_request(
        self,
        *,
        title: str,
        person_id: int,
        idempotency_key: str,
        requester_id: int | None = None,
        board: str | None = None,
        due: str | None = None,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        target = self.boards.get(board) if board else None
        headers = self._headers()
        headers["Idempotency-Key"] = idempotency_key
        body = {
            "query": CREATE_ITEM_MUTATION,
            "variables": {
                "boardId": target.board_id if target else self.config.board_id,
                "groupId": target.group_id if target else self.config.group_id,
                "itemName": title[:MAX_ITEM_NAME],
                "columnValues": json.dumps(
                    self.column_values(
                        title=title,
                        person_id=person_id,
                        idempotency_key=idempotency_key,
                        requester_id=requester_id,
                        board=board,
                        due=due,
                    ),
                    ensure_ascii=False,
                ),
            },
        }
        return headers, body

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self.config.api_token,
            "API-Version": self.config.api_version,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ operations
    async def create_task(
        self,
        *,
        title: str,
        person_id: int,
        ctx: ExecutionContext,
        requester_id: int | None = None,
        board: str | None = None,
        due: str | None = None,
    ) -> ConnectorResult:
        if ctx.prior_attempt_uncertain and self.config.idempotency_column and not board:
            existing = await self.find_by_key(ctx.idempotency_key)
            if existing:
                log.info("monday.item_already_exists", job_id=ctx.job_id, item_id=existing["id"])
                return self._result(existing, replayed=True, recovered=True)

        headers, body = self.build_create_request(
            title=title,
            person_id=person_id,
            idempotency_key=ctx.idempotency_key,
            requester_id=requester_id,
            board=board,
            due=due,
        )
        response, data = await self._post(headers, body, mutation=True)
        item = (data or {}).get("create_item")
        if not item or not item.get("id"):
            raise TransientConnectorError(
                "Monday response did not include create_item.id", uncertain=True
            )
        replayed = response.headers.get("Idempotency-Replayed", "").lower() == "true"
        log.info("monday.item_created", job_id=ctx.job_id, item_id=item["id"], replayed=replayed)
        return self._result(item, replayed=replayed)

    async def find_by_key(self, key: str) -> dict | None:
        body = {
            "query": FIND_BY_KEY_QUERY,
            "variables": {
                "boardId": self.config.board_id,
                "columnId": self.config.idempotency_column,
                "value": key,
            },
        }
        _, data = await self._post(self._headers(), body, mutation=False)
        items = ((data or {}).get("items_page_by_column_values") or {}).get("items") or []
        return items[0] if items else None

    async def inspect(self) -> dict:
        body = {"query": INSPECT_QUERY, "variables": {"boardId": [self.config.board_id]}}
        _, data = await self._post(self._headers(), body, mutation=False)
        data = data or {}
        for board in data.get("boards") or []:
            for column in board.get("columns") or []:
                settings = column.pop("settings", None)
                if isinstance(settings, str):
                    try:
                        settings = json.loads(settings)
                    except ValueError:
                        settings = None
                if column.get("type") == "dropdown" and isinstance(settings, dict):
                    column["options"] = [
                        # typed ``settings`` uses "label"; legacy ``settings_str`` used "name"
                        {"id": label.get("id"), "name": label.get("label") or label.get("name")}
                        for label in settings.get("labels") or []
                        if isinstance(label, dict) and not label.get("is_deactivated")
                    ]
        return data

    def _result(self, item: dict, *, replayed: bool, recovered: bool = False) -> ConnectorResult:
        return ConnectorResult(
            external_id=str(item["id"]),
            data={
                "item_id": str(item["id"]),
                "item_name": item.get("name"),
                "url": item.get("url"),
                "board_id": self.config.board_id,
                "group_id": self.config.group_id,
                "idempotency_replayed": replayed,
                "recovered_existing_item": recovered,
            },
        )

    # ------------------------------------------------------------------ transport
    async def _post(
        self, headers: dict[str, str], body: dict[str, Any], *, mutation: bool
    ) -> tuple[httpx.Response, dict | None]:
        try:
            response = await self._client.post(self.config.api_url, headers=headers, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The request never reached Monday: retrying cannot duplicate anything.
            raise TransientConnectorError(f"Monday connection failed: {exc!r}") from exc
        except httpx.TimeoutException as exc:
            raise TransientConnectorError(
                f"Monday request timed out: {exc!r}", uncertain=mutation
            ) from exc
        except httpx.TransportError as exc:
            raise TransientConnectorError(
                f"Monday transport error: {exc!r}", uncertain=mutation
            ) from exc

        retry_after = _retry_after(response)
        status = response.status_code
        if status != 200:
            code, message = _first_error(_safe_json(response))
            detail = f"Monday HTTP {status}: {code or ''} {message or ''}".strip()
            if status in TRANSIENT_HTTP or status >= 500:
                raise TransientConnectorError(
                    detail,
                    retry_after=retry_after,
                    uncertain=mutation and status >= 500,
                    code=code or str(status),
                )
            raise PermanentConnectorError(detail, code=code or str(status))

        payload = _safe_json(response)
        if payload is None:
            raise TransientConnectorError("Monday returned invalid JSON", uncertain=mutation)
        data = payload.get("data")
        errors = payload.get("errors") or []
        if payload.get("error_code") or payload.get("error_message"):
            errors.append(
                {
                    "message": payload.get("error_message"),
                    "extensions": {"code": payload.get("error_code")},
                }
            )
        if errors:
            if data and data.get("create_item", {}) and data["create_item"].get("id"):
                log.warning("monday.partial_errors", errors=errors)  # item exists; keep it
                return response, data
            code, message = _first_error(payload | {"errors": errors})
            retry_after = retry_after or _retry_in_seconds(errors)
            detail = f"Monday GraphQL error {code}: {message}"
            if code in TRANSIENT_ERROR_CODES:
                raise TransientConnectorError(detail, retry_after=retry_after, code=code)
            raise PermanentConnectorError(detail, code=code)
        return response, data

    async def check_ready(self) -> bool:
        return bool(self.config.api_token and self.config.board_id and self.person_ids)

    async def aclose(self) -> None:
        await self._client.aclose()


class MockMondayConnector(MondayConnector):
    """In-memory Monday used by the simulator and tests. Honors idempotency keys."""

    def __init__(
        self,
        person_ids: dict[str, int],
        *,
        fail_times: int = 0,
        boards: dict[str, BoardTarget] | None = None,
    ):
        super().__init__(person_ids, boards=boards)
        self.fail_times = fail_times
        self.items: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self._next_id = 9_000_000_001

    async def create_task(
        self,
        *,
        title: str,
        person_id: int,
        ctx: ExecutionContext,
        requester_id: int | None = None,
        board: str | None = None,
        due: str | None = None,
    ) -> ConnectorResult:
        self.calls.append(
            {
                "title": title,
                "person_id": person_id,
                "requester_id": requester_id,
                "board": board,
                "due": due,
                "key": ctx.idempotency_key,
                "attempt": ctx.attempt,
            }
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TransientConnectorError("simulated Monday outage (HTTP 503)", code="503")
        replayed = ctx.idempotency_key in self.items
        if not replayed:
            self.items[ctx.idempotency_key] = {
                "id": str(self._next_id),
                "name": title,
                "person_id": person_id,
                "url": f"https://example.monday.com/boards/mock/pulses/{self._next_id}",
            }
            self._next_id += 1
        item = self.items[ctx.idempotency_key]
        return ConnectorResult(
            external_id=item["id"],
            data={
                "item_id": item["id"],
                "item_name": item["name"],
                "url": item["url"],
                "assignee_person_id": person_id,
                "idempotency_replayed": replayed,
                "mock": True,
            },
        )


# ---------------------------------------------------------------------- helpers
def _safe_json(response: httpx.Response) -> dict | None:
    try:
        value = response.json()
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _first_error(payload: dict | None) -> tuple[str | None, str | None]:
    if not payload:
        return None, None
    errors = payload.get("errors") or []
    if errors and isinstance(errors, list):
        first = errors[0] or {}
        code = (first.get("extensions") or {}).get("code")
        return code or first.get("message"), first.get("message")
    return payload.get("error_code"), payload.get("error_message")


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _retry_in_seconds(errors: list[dict]) -> float | None:
    for error in errors:
        ext = error.get("extensions") or {}
        for source in (ext, ext.get("error_data") or {}):
            value = source.get("retry_in_seconds")
            if isinstance(value, int | float):
                return float(value)
    return None
