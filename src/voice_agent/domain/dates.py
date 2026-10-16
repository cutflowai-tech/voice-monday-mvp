"""Due dates from Egyptian Arabic time words («بكرة», «بعد بكرة», «يوم الخميس»).

Used for the Timeline column on boards that have one. Dates are in Cairo time.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from voice_agent.nlu.normalize import normalize_text

CAIRO = ZoneInfo("Africa/Cairo")

# normalized weekday words -> Python weekday (Monday = 0)
WEEKDAYS = {
    "الاتنين": 0,
    "الاثنين": 0,
    "التلات": 1,
    "الثلاثاء": 1,
    "التلاتاء": 1,
    "الاربع": 2,
    "الاربعاء": 2,
    "الخميس": 3,
    "الجمعه": 4,
    "السبت": 5,
    "الحد": 6,
    "الاحد": 6,
}


def today_in_cairo() -> dt.date:
    return dt.datetime.now(CAIRO).date()


def due_date(text: str, today: dt.date | None = None) -> dt.date | None:
    """The date a task's words point to, or None when they don't name one."""
    today = today or today_in_cairo()
    words = normalize_text(text).split()
    joined = " ".join(words)
    if "بعد بكره" in joined or "بعد بكرا" in joined:
        return today + dt.timedelta(days=2)
    if "بكره" in words or "بكرا" in words:
        return today + dt.timedelta(days=1)
    if "النهارده" in words or "النهارده،" in words or "انهارده" in words:
        return today
    for word in words:
        if word in WEEKDAYS:
            ahead = (WEEKDAYS[word] - today.weekday()) % 7  # same day name -> today
            return today + dt.timedelta(days=ahead)
    return None
