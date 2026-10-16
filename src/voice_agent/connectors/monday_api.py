"""Monday.com as a toolbox for Sarah's Monday assistant (read, search and change anything).

``MondayAPI`` runs GraphQL documents; ``MondayTools`` turns the common jobs into small,
safe tools an LLM can call (function-calling schemas in ``MondayTools.SCHEMAS``), plus a
``graphql`` escape hatch for everything else.

API facts used here (developer.monday.com, API version 2026-07):

* ``boards(limit, page, state: active)`` → ``id name type items_count workspace {name}``;
  ``type`` is ``board``, ``sub_items_board`` or ``document``.
* ``items_page(limit, query_params: {rules: [{column_id, compare_value, operator}]})``;
  ``column_id: "name"`` + ``operator: contains_text`` searches item names.
* ``column_values { id type text column { title } }``; ``Column.settings`` is JSON (status
  and dropdown labels live in it).
* ``change_multiple_column_values(board_id, item_id, column_values: JSON,
  create_labels_if_missing)``: status ``{"label": "Done"}``, date ``{"date": "YYYY-MM-DD"}``,
  people ``{"personsAndTeams": [{"id": 1, "kind": "person"}]}``, text as a plain string,
  the item name under ``"name"``.
* ``create_update(item_id, body)``, ``move_item_to_group(item_id, group_id)``,
  ``archive_item(item_id)`` (archived items can be restored from Monday for 30 days).

Safety: nothing here deletes. The escape hatch refuses ``delete_*`` and account/permission
mutations, so a misheard sentence can at worst archive an item, never destroy data.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, ClassVar

import httpx

from voice_agent.logging_setup import get_logger

log = get_logger(__name__)

RESULT_LIMIT = 24_000  # characters of tool output handed back to the LLM
MAP_TTL_SECONDS = 300  # boards and users are re-read every 5 minutes
# Status texts (any column) or group titles that mean an item is finished
CLOSED = {"done", "resolved", "completed", "complete", "closed", "finished", "تم", "خلصت"}
SEARCH_BOARDS = 60  # most recently used boards searched by search_items
SEARCH_CONCURRENCY = 6

# Mutations the escape hatch never runs: deletions, account and permission changes.
BLOCKED_MUTATION = re.compile(
    r"\b(\w*(?:delete|remove|deactivat|activate_users|add_users|update_users|update_multiple_users|invite|clear_|_role|permission|owner|"
    r"team|service_user|department|webhook|subscription|bulk_archive|archive_board|"
    r"archive_group|archive_workspace|archive_object)\w*)\b"
)


class MondayAPIError(Exception):
    """Monday refused or failed the request; the message is safe to show the LLM."""


class MondayAPI:
    def __init__(
        self,
        token: str,
        *,
        url: str = "https://api.monday.com/v2",
        version: str = "2026-07",
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._token = token
        self.url = url
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            transport=transport,
            headers={"API-Version": version, "Content-Type": "application/json"},
        )

    async def run(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        try:
            response = await self._client.post(
                self.url,
                headers={"Authorization": self._token},
                json={"query": query, "variables": variables or {}},
            )
        except httpx.HTTPError as exc:
            raise MondayAPIError(f"Monday unreachable: {type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        errors = list(payload.get("errors") or [])
        if payload.get("error_message"):
            errors.append({"message": payload["error_message"]})
        if response.status_code != 200 or errors:
            message = "; ".join(str(e.get("message", e))[:300] for e in errors[:3])
            raise MondayAPIError(message or f"Monday HTTP {response.status_code}")
        return payload.get("data") or {}

    async def aclose(self) -> None:
        await self._client.aclose()


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


_ID = {"type": "string", "description": "Monday id (digits)."}


class MondayTools:
    """The tools the Monday assistant's LLM can call. Every result is JSON-serialisable."""

    SCHEMAS: ClassVar[list[dict]] = [
        {
            "name": "get_board",
            "description": (
                "A board's groups and columns (ids, titles, types, and the labels of status "
                "and dropdown columns). Read it before filtering or changing items."
            ),
            "parameters": _obj({"board_id": _ID}, ["board_id"]),
        },
        {
            "name": "get_items",
            "description": (
                "Items of one board with every column as text. open_only: true drops "
                "finished items (Done/Resolved…). Optional: search (part of "
                "the item name), group_id, rules (Monday items_page rules, e.g. "
                '[{"column_id":"status","compare_value":[1],"operator":"any_of"}] or '
                '[{"column_id":"person","compare_value":["person-123"],"operator":"any_of"}]).'
            ),
            "parameters": _obj(
                {
                    "board_id": _ID,
                    "search": {"type": "string"},
                    "group_id": {"type": "string"},
                    "rules": {"type": "array", "items": {"type": "object"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "open_only": {"type": "boolean"},
                },
                ["board_id"],
            ),
        },
        {
            "name": "search_items",
            "description": (
                "Find items by part of their name across all boards (when you don't know "
                "the board)."
            ),
            "parameters": _obj({"text": {"type": "string"}}, ["text"]),
        },
        {
            "name": "get_item",
            "description": "One item in full: columns, subitems and its latest updates.",
            "parameters": _obj({"item_id": _ID}, ["item_id"]),
        },
        {
            "name": "update_item",
            "description": (
                "Change one item's columns. values maps column id to value: status "
                '{"label":"Done"}, date {"date":"2026-10-20"}, timeline '
                '{"from":"2026-10-20","to":"2026-10-22"}, people '
                '{"personsAndTeams":[{"id":123,"kind":"person"}]}, text/numbers as a '
                'string, the item name as "name". Use labels that exist on the column.'
            ),
            "parameters": _obj(
                {"board_id": _ID, "item_id": _ID, "values": {"type": "object"}},
                ["board_id", "item_id", "values"],
            ),
        },
        {
            "name": "create_item",
            "description": "Create an item on a board (values as in update_item).",
            "parameters": _obj(
                {
                    "board_id": _ID,
                    "name": {"type": "string"},
                    "group_id": {"type": "string"},
                    "values": {"type": "object"},
                },
                ["board_id", "name"],
            ),
        },
        {
            "name": "add_update",
            "description": "Post an update (comment) on an item.",
            "parameters": _obj({"item_id": _ID, "text": {"type": "string"}}, ["item_id", "text"]),
        },
        {
            "name": "move_item",
            "description": "Move an item to another group of its board.",
            "parameters": _obj(
                {"item_id": _ID, "group_id": {"type": "string"}}, ["item_id", "group_id"]
            ),
        },
        {
            "name": "archive_item",
            "description": "Archive an item (Monday keeps it restorable). Nothing is deleted.",
            "parameters": _obj({"item_id": _ID}, ["item_id"]),
        },
        {
            "name": "graphql",
            "description": (
                "Run any other Monday GraphQL query or mutation (API 2026-07) when the tools "
                "above can't do it: subitems, docs, dashboards, workspaces, activity logs, "
                "creating groups or columns. Deletions and user/permission changes are refused."
            ),
            "parameters": _obj(
                {"query": {"type": "string"}, "variables": {"type": "object"}}, ["query"]
            ),
        },
    ]

    def __init__(self, api: MondayAPI, *, clock=time.monotonic):
        self.api = api
        self._clock = clock
        self._boards: tuple[float, list[dict]] | None = None
        self._users: tuple[float, list[dict]] | None = None

    # ------------------------------------------------------------------ workspace map
    async def boards(self, refresh: bool = False) -> list[dict]:
        if not refresh and self._boards and self._clock() - self._boards[0] < MAP_TTL_SECONDS:
            return self._boards[1]
        found: list[dict] = []
        for page in range(1, 6):
            data = await self.api.run(
                "query($page:Int!){boards(limit:200,page:$page,state:active,order_by:used_at)"
                "{id name type items_count workspace{name}}}",
                {"page": page},
            )
            batch = data.get("boards") or []
            found += [
                {
                    "id": b["id"],
                    "name": b["name"],
                    "workspace": (b.get("workspace") or {}).get("name") or "Main",
                    "items": b.get("items_count"),
                }
                for b in batch
                if b.get("type") in (None, "board")
            ]
            if len(batch) < 200:
                break
        self._boards = (self._clock(), found)
        return found

    async def users(self, refresh: bool = False) -> list[dict]:
        if not refresh and self._users and self._clock() - self._users[0] < MAP_TTL_SECONDS:
            return self._users[1]
        data = await self.api.run("query{users(limit:500,kind:non_guests){id name email}}")
        users = [
            {"id": u["id"], "name": u["name"], "email": u.get("email")}
            for u in data.get("users") or []
        ]
        self._users = (self._clock(), users)
        return users

    async def workspace_map(self, refresh: bool = False) -> str:
        boards, users = await asyncio.gather(self.boards(refresh), self.users(refresh))
        board_lines = "\n".join(
            f"- {b['id']} | {b['name']} | {b['workspace']} | {b['items']} items" for b in boards
        )
        user_lines = "\n".join(f"- {u['id']} | {u['name']} | {u['email']}" for u in users)
        return (
            f"## Boards (id | name | workspace | items)\n{board_lines}\n\n"
            f"## Users (id | name | email)\n{user_lines}"
        )

    # ------------------------------------------------------------------ dispatch
    async def call(self, name: str, args: dict[str, Any]) -> str:
        """Run one tool; errors come back as text so the LLM can recover."""
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return json.dumps({"error": f"unknown tool {name}"})
        try:
            result = await handler(**args)
        except MondayAPIError as exc:
            result = {"error": str(exc)}
        except (TypeError, ValueError, KeyError) as exc:
            result = {"error": f"bad arguments: {exc}"}
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > RESULT_LIMIT:
            text = text[:RESULT_LIMIT] + ' …(truncated: narrow the request)"'
        return text

    # ------------------------------------------------------------------ tools
    async def _t_get_board(self, board_id: str) -> dict:
        data = await self.api.run(
            "query($id:[ID!]){boards(ids:$id){id name description workspace{name}"
            " groups{id title} columns{id title type settings}}}",
            {"id": [str(board_id)]},
        )
        boards = data.get("boards") or []
        if not boards:
            return {"error": "board not found"}
        board = boards[0]
        board["columns"] = [_column_summary(c) for c in board.get("columns") or []]
        return board

    async def _t_get_items(
        self,
        board_id: str,
        search: str | None = None,
        group_id: str | None = None,
        rules: list[dict] | None = None,
        limit: int = 200,
        open_only: bool = False,
    ) -> dict:
        all_rules = list(rules or [])
        if search:
            all_rules.append(
                {"column_id": "name", "compare_value": [search], "operator": "contains_text"}
            )
        params = {"rules": all_rules} if all_rules else None
        page = (
            "items_page(limit:$limit,query_params:$qp){cursor items{id name group{id title}"
            " column_values{id text column{title}}}}"
        )
        if group_id:
            query = (
                "query($id:[ID!],$g:[String],$limit:Int!,$qp:ItemsQuery){boards(ids:$id)"
                f"{{groups(ids:$g){{{page}}}}}}}"
            )
            variables = {"g": [group_id]}
        else:
            query = f"query($id:[ID!],$limit:Int!,$qp:ItemsQuery){{boards(ids:$id){{{page}}}}}"
            variables = {}
        data = await self.api.run(
            query,
            {"id": [str(board_id)], "limit": max(1, min(int(limit), 200)), "qp": params}
            | variables,
        )
        boards = data.get("boards") or []
        if not boards:
            return {"error": "board not found"}
        holder = boards[0]
        if group_id:
            groups = holder.get("groups") or []
            if not groups:
                return {"error": "group not found"}
            holder = groups[0]
        page_data = holder.get("items_page") or {}
        items = [_item_summary(i) for i in page_data.get("items") or []]
        result: dict[str, Any] = {"more": bool(page_data.get("cursor"))}
        if open_only:
            result["finished_skipped"] = len(items)
            items = [i for i in items if not _finished(i)]
            result["finished_skipped"] -= len(items)
        return {"count": len(items), **result, "items": items}

    async def _t_search_items(self, text: str) -> dict:
        boards = [b for b in await self.boards() if b.get("items")][:SEARCH_BOARDS]
        gate = asyncio.Semaphore(SEARCH_CONCURRENCY)

        async def one(board: dict) -> list[dict]:
            async with gate:
                try:
                    found = await self._t_get_items(board["id"], search=text, limit=10)
                except MondayAPIError:
                    return []
            return [
                {"board_id": board["id"], "board": board["name"]} | item
                for item in found.get("items", [])
            ]

        results = [i for found in await asyncio.gather(*map(one, boards)) for i in found]
        return {"count": len(results), "items": results[:25]}

    async def _t_get_item(self, item_id: str) -> dict:
        data = await self.api.run(
            "query($id:[ID!]){items(ids:$id){id name url created_at updated_at board{id name}"
            " group{id title} column_values{id text column{title}}"
            " subitems{id name column_values{text column{title}}}"
            " updates(limit:5){text_body created_at creator{name}}}}",
            {"id": [str(item_id)]},
        )
        items = data.get("items") or []
        if not items:
            return {"error": "item not found"}
        item = items[0]
        item["columns"] = _columns(item.pop("column_values", []))
        item["subitems"] = [
            {"id": s["id"], "name": s["name"], "columns": _columns(s.get("column_values", []))}
            for s in item.get("subitems") or []
        ]
        return item

    async def _t_update_item(self, board_id: str, item_id: str, values: dict) -> dict:
        data = await self.api.run(
            "mutation($b:ID!,$i:ID!,$v:JSON!){change_multiple_column_values(board_id:$b,"
            "item_id:$i,column_values:$v,create_labels_if_missing:false){id name}}",
            {"b": str(board_id), "i": str(item_id), "v": json.dumps(values, ensure_ascii=False)},
        )
        log.info("monday_tools.update_item", board_id=board_id, item_id=item_id)
        return {"updated": data.get("change_multiple_column_values")}

    async def _t_create_item(
        self, board_id: str, name: str, group_id: str | None = None, values: dict | None = None
    ) -> dict:
        data = await self.api.run(
            "mutation($b:ID!,$g:String,$n:String!,$v:JSON){create_item(board_id:$b,"
            "group_id:$g,item_name:$n,column_values:$v){id name}}",
            {
                "b": str(board_id),
                "g": group_id,
                "n": name[:255],
                "v": json.dumps(values, ensure_ascii=False) if values else None,
            },
        )
        log.info("monday_tools.create_item", board_id=board_id)
        return {"created": data.get("create_item")}

    async def _t_add_update(self, item_id: str, text: str) -> dict:
        data = await self.api.run(
            "mutation($i:ID!,$t:String!){create_update(item_id:$i,body:$t){id}}",
            {"i": str(item_id), "t": text},
        )
        return {"posted": data.get("create_update")}

    async def _t_move_item(self, item_id: str, group_id: str) -> dict:
        data = await self.api.run(
            "mutation($i:ID!,$g:String!){move_item_to_group(item_id:$i,group_id:$g){id}}",
            {"i": str(item_id), "g": group_id},
        )
        return {"moved": data.get("move_item_to_group")}

    async def _t_archive_item(self, item_id: str) -> dict:
        data = await self.api.run(
            "mutation($i:ID!){archive_item(item_id:$i){id}}", {"i": str(item_id)}
        )
        log.info("monday_tools.archive_item", item_id=item_id)
        return {"archived": data.get("archive_item")}

    async def _t_graphql(self, query: str, variables: dict | None = None) -> dict:
        reason = blocked_reason(query)
        if reason:
            return {"error": reason}
        return await self.api.run(query, variables)


def blocked_reason(document: str) -> str | None:
    """Why the escape hatch refuses this document, or None when it may run."""
    without_strings = re.sub(r'"(?:\\.|[^"\\])*"', '""', document)
    if not re.search(r"\bmutation\b", without_strings):
        return None  # a query
    match = BLOCKED_MUTATION.search(without_strings)
    if match:
        return (
            f"refused: {match.group(1)} is not allowed from the phone. Nothing is ever "
            "deleted; use archive_item to remove an item."
        )
    return None


def _labels(settings: Any) -> list[str] | None:
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            return None
    if not isinstance(settings, dict):
        return None
    labels = settings.get("labels")
    if isinstance(labels, dict):
        return [str(v) for v in labels.values() if v]
    if isinstance(labels, list):
        return [
            str(lab.get("label") or lab.get("name"))
            for lab in labels
            if isinstance(lab, dict) and not lab.get("is_deactivated")
        ]
    return None


def _column_summary(column: dict) -> dict:
    summary = {"id": column["id"], "title": column["title"], "type": column["type"]}
    labels = _labels(column.get("settings"))
    if labels:
        summary["labels"] = labels
    return summary


def _columns(values: list[dict]) -> dict[str, str]:
    return {
        (v.get("column") or {}).get("title") or v.get("id", "?"): v.get("text") or ""
        for v in values
        if v.get("text")
    }


def _finished(item: dict) -> bool:
    texts = [*item["columns"].values(), item.get("group") or ""]
    return any(str(t).strip().lower() in CLOSED for t in texts)


def _item_summary(item: dict) -> dict:
    return {
        "id": item["id"],
        "name": item["name"],
        "group": (item.get("group") or {}).get("title"),
        "group_id": (item.get("group") or {}).get("id"),
        "columns": _columns(item.get("column_values") or []),
    }
