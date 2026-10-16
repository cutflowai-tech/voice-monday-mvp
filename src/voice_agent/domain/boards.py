"""Which Monday board a task goes to (``config/boards.json``).

* The default board is the one configured through ``MONDAY_BOARD_ID`` & co. (Request tasks).
* Extra boards (e.g. Account Managers Tasks) are listed with their group, assignee column
  and fixed column values (status, priority) and an optional timeline column.
* ``monday_users`` maps person keys to Monday user ids for People columns.
* ``home_board`` maps a person to a board key, or to ``"ask"``: Sarah must ask the caller
  which board (the server refuses to guess, see ``TaskWindow``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ASK = "ask"
AUTO = "auto"


@dataclass(frozen=True)
class BoardSpec:
    key: str
    name_ar: str
    name_en: str
    board_id: str | None = None  # None: the default board from MONDAY_BOARD_ID
    group_id: str = ""
    assignee_column: str = ""
    assignee_column_type: str = "people"
    values: dict[str, Any] = field(default_factory=dict)  # fixed column values
    timeline_column: str = ""
    due_column: str = ""  # a Date column for the deadline (when there is no timeline)

    @property
    def has_deadline(self) -> bool:
        return bool(self.timeline_column or self.due_column)


class BoardDirectory:
    def __init__(
        self,
        boards: dict[str, BoardSpec],
        *,
        default: str,
        monday_users: dict[str, int] | None = None,
        home_board: dict[str, str] | None = None,
    ):
        if default not in boards:
            raise ValueError(f"default board {default!r} is not listed")
        self.boards = boards
        self.default = default
        self.monday_users = dict(monday_users or {})
        self.home_board = dict(home_board or {})
        for person, home in self.home_board.items():
            if home != ASK and home not in boards:
                raise ValueError(f"home board {home!r} of {person!r} is not listed")

    @classmethod
    def single(cls, name_ar: str = "الريكويستس", name_en: str = "Request tasks") -> BoardDirectory:
        """Only the default board (no boards file configured)."""
        return cls({"requests": BoardSpec("requests", name_ar, name_en)}, default="requests")

    @classmethod
    def load(cls, path: str | None) -> BoardDirectory:
        if not path:
            return cls.single()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        boards = {
            key: BoardSpec(key=key, **{k: v for k, v in spec.items() if k != "key"})
            for key, spec in raw["boards"].items()
        }
        return cls(
            boards,
            default=raw.get("default", "requests"),
            monday_users={k: int(v) for k, v in (raw.get("monday_users") or {}).items()},
            home_board=raw.get("home_board") or {},
        )

    def board_keys(self) -> list[str]:
        return list(self.boards)

    def extra(self) -> dict[str, BoardSpec]:
        """Boards other than the default one (they carry their own Monday settings)."""
        return {k: b for k, b in self.boards.items() if b.board_id}

    def resolve(self, assignee_key: str, requested: str | None) -> str:
        """The board for this task, or ``ASK`` when the caller has to choose."""
        if requested and requested != AUTO:
            return requested
        return self.home_board.get(assignee_key, self.default)

    def name_ar(self, key: str) -> str:
        return self.boards[key].name_ar
