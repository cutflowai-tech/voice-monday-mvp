"""People directory: maps spoken Arabic names to stable person keys.

The directory is connector-agnostic. Connectors map ``key`` to their own ids
(e.g. Monday person ids come from ``MONDAY_<KEY>_PERSON_ID``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from voice_agent.nlu.normalize import normalize_token


@dataclass(frozen=True)
class Person:
    key: str
    name_en: str
    name_ar: str
    gender: str = "m"
    aliases: tuple[str, ...] = field(default_factory=tuple)


class PeopleDirectory:
    def __init__(self, people: list[Person]):
        self._people = {p.key: p for p in people}
        self._by_alias: dict[str, Person] = {}
        self._ambiguous_aliases: set[str] = set()
        for person in people:
            for alias in (person.name_ar, person.name_en, *person.aliases):
                normalized = normalize_token(alias)
                existing = self._by_alias.get(normalized)
                if existing and existing.key != person.key:
                    self._ambiguous_aliases.add(normalized)
                else:
                    self._by_alias[normalized] = person

    def lookup(self, spoken: str) -> Person | None:
        normalized = normalize_token(spoken)
        return None if normalized in self._ambiguous_aliases else self._by_alias.get(normalized)

    def get(self, key: str) -> Person | None:
        return self._people.get(key)

    def __iter__(self):
        return iter(self._people.values())

    @classmethod
    def from_json(cls, data: list[dict]) -> PeopleDirectory:
        people = [
            Person(
                key=item["key"],
                name_en=item["name_en"],
                name_ar=item["name_ar"],
                gender=item.get("gender", "m"),
                aliases=tuple(item.get("aliases", [])),
            )
            for item in data
        ]
        return cls(people)

    @classmethod
    def load(cls, path: str | Path | None = None) -> PeopleDirectory:
        """Load from ``path`` or the packaged default directory."""
        if path:
            raw = Path(path).read_text(encoding="utf-8")
        else:
            raw = resources.files("voice_agent").joinpath("people.json").read_text("utf-8")
        return cls.from_json(json.loads(raw))
