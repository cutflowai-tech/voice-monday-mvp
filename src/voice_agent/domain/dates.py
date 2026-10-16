"""Due dates from Egyptian Arabic time words.

Understood (Cairo time): «النهارده»، «بكرة»، «بعد بكرة»، weekdays («يوم الخميس» = the next
Thursday, today when it is Thursday)، «الأسبوع الجاي» (+7 days)، «آخر الأسبوع» (the coming
Thursday, the end of the Egyptian work week)، «بعد 3 أيام» / «بعد تلات أيام» / «بعد يومين» /
«بعد أسبوع» / «بعد أسبوعين»، «الشهر الجاي» (+1 month)، day numbers «يوم 20»، «20 أكتوبر»،
«20/10» (this month or year, else the next one), and ISO dates «2026-10-20».

Used for the deadline column of the board a task goes to.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from zoneinfo import ZoneInfo

from voice_agent.nlu.normalize import normalize_text

CAIRO = ZoneInfo("Africa/Cairo")
THURSDAY = 3

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

# normalized month names (Egyptian and Levantine) -> month number
MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "مايو": 5, "يونيو": 6, "يونيه": 6,
    "يوليو": 7, "يوليه": 7, "اغسطس": 8, "سبتمبر": 9, "اكتوبر": 10, "نوفمبر": 11,
    "ديسمبر": 12, "كانون": 1,
}  # fmt: skip

NUMBER_WORDS = {
    "واحد": 1, "اتنين": 2, "تلات": 3, "تلاته": 3, "تلاث": 3, "اربع": 4, "اربعه": 4,
    "خمس": 5, "خمسه": 5, "ست": 6, "سته": 6, "سبع": 7, "سبعه": 7, "تمن": 8, "تمانيه": 8,
    "تسع": 9, "تسعه": 9, "عشر": 10, "عشره": 10,
}  # fmt: skip

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def today_in_cairo() -> dt.date:
    return dt.datetime.now(CAIRO).date()


def _add_months(day: dt.date, months: int) -> dt.date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _day_of_month(day: int, month: int | None, today: dt.date) -> dt.date | None:
    """«يوم 20» (this month, else next) or «20 أكتوبر» (this year, else next)."""
    if month is None:
        for ahead in (0, 1):
            candidate_month = _add_months(today.replace(day=1), ahead)
            if day <= calendar.monthrange(candidate_month.year, candidate_month.month)[1]:
                candidate = candidate_month.replace(day=day)
                if candidate >= today:
                    return candidate
        return None
    for year in (today.year, today.year + 1):
        if day <= calendar.monthrange(year, month)[1]:
            candidate = dt.date(year, month, day)
            if candidate >= today:
                return candidate
    return None


def due_date(text: str, today: dt.date | None = None) -> dt.date | None:
    """The date a task's words point to, or None when they don't name one."""
    today = today or today_in_cairo()
    raw = (text or "").translate(_ARABIC_DIGITS)
    iso = _ISO.search(raw)
    if iso:
        try:
            return dt.date(int(iso[1]), int(iso[2]), int(iso[3]))
        except ValueError:
            return None
    words = normalize_text(raw).split()
    joined = " ".join(words)

    # «بعد بكرة» before «بكرة»
    if "بعد بكره" in joined or "بعد بكرا" in joined:
        return today + dt.timedelta(days=2)

    # «بعد يومين»، «بعد 3 أيام»، «بعد تلات أيام»، «بعد أسبوع»، «بعد أسبوعين»، «بعد شهر»
    after = re.search(r"بعد (\S+)(?: (\S+))?", joined)
    if after:
        first, second = after[1], after[2] or ""
        if first in ("يوم", "يومين"):
            return today + dt.timedelta(days=2 if first == "يومين" else 1)
        if first in ("اسبوع", "اسبوعين", "جمعه", "جمعتين"):
            return today + dt.timedelta(days=14 if first.endswith("ين") else 7)
        if first in ("شهر", "شهرين"):
            return _add_months(today, 2 if first == "شهرين" else 1)
        count = int(first) if first.isdigit() else NUMBER_WORDS.get(first)
        if count and second.startswith(("ايام", "يوم")):
            return today + dt.timedelta(days=count)
        if count and second.startswith(("اسابيع", "اسبوع")):
            return today + dt.timedelta(days=7 * count)

    if "بكره" in words or "بكرا" in words:
        return today + dt.timedelta(days=1)
    if any(w in words for w in ("النهارده", "انهارده", "النهارد", "النهاردة")):
        return today

    # «الأسبوع الجاي»، «آخر الأسبوع»، «الشهر الجاي»
    if re.search(r"(الاسبوع|الجمعه) (الجاي|اللي جاي|القادم)", joined):
        return today + dt.timedelta(days=7)
    if re.search(r"(اخر|نهايه) الاسبوع", joined):
        return today + dt.timedelta(days=(THURSDAY - today.weekday()) % 7)
    if re.search(r"الشهر (الجاي|اللي جاي|القادم)", joined):
        return _add_months(today, 1)

    # «20 أكتوبر»، «20/10»، «يوم 20»
    for i, word in enumerate(words):
        if not word.isdigit() or not 1 <= int(word) <= 31:
            continue
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if nxt in MONTHS:
            return _day_of_month(int(word), MONTHS[nxt], today)
        if nxt.isdigit() and 1 <= int(nxt) <= 12:
            return _day_of_month(int(word), int(nxt), today)
        if i > 0 and words[i - 1] in ("يوم", "في"):
            return _day_of_month(int(word), None, today)

    for word in words:
        if word in WEEKDAYS:
            ahead = (WEEKDAYS[word] - today.weekday()) % 7  # same day name -> today
            return today + dt.timedelta(days=ahead)
    return None


NO_DEADLINE = "none"  # the caller said the task has no deadline
