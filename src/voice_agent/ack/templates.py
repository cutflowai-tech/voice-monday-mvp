"""Deterministic, intent-aware Egyptian Arabic responses.

Every acknowledgment is assembled from the parsed intent fields (person, action
phrase, task, subject, time). Several phrasings exist per situation and one is
chosen at random so the assistant does not sound robotic. Inject a seeded
``random.Random`` for reproducible output in tests.
"""

from __future__ import annotations

import random

from voice_agent.ack.base import AcknowledgmentGenerator, FarewellReason
from voice_agent.domain.intents import Action, Intent

MAX_SPOKEN_WORDS = 8
_TRAILING_WEAK = {"من", "في", "على", "علي", "و", "مع", "عشان", "اللي", "بتاع", "ل", "عن"}


def shorten(text: str | None, max_words: int = MAX_SPOKEN_WORDS) -> str | None:
    """Keep spoken confirmations short: cap long phrases at a natural boundary."""
    if not text:
        return text
    words = text.split()
    if len(words) <= max_words:
        return text
    words = words[: max_words - 2]
    while words and words[-1] in _TRAILING_WEAK:
        words.pop()
    return " ".join(words)


def _name(intent: Intent) -> str | None:
    return intent.details.assignee_spoken or intent.assignee


def _is_female(intent: Intent) -> bool:
    return intent.details.assignee_gender == "f"


def _suffix(text: str | None) -> str:
    return f" {text}" if text else ""


class TemplateAcknowledgmentGenerator(AcknowledgmentGenerator):
    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()

    def _pick(self, options: list[str]) -> str:
        return self._rng.choice(options)

    # ------------------------------------------------------------ acknowledgments
    async def acknowledge(self, intent: Intent) -> str:
        builder = {
            Action.CREATE_TASK: self._ack_create_task,
            Action.REMIND: self._ack_remind,
            Action.SCHEDULE_MEETING: self._ack_meeting,
            Action.SEND_MESSAGE: self._ack_send,
        }.get(intent.action)
        if builder is None:
            raise ValueError(f"No acknowledgment template for action {intent.action}")
        return builder(intent)

    def _ack_create_task(self, intent: Intent) -> str:
        name = _name(intent)
        task = shorten(intent.task)
        phrase = shorten(intent.details.action_phrase)
        that = "إنها" if _is_female(intent) else "إنه"
        if phrase:
            return self._pick(
                [
                    f"حاضر، هقول ل{name} {phrase} حالًا.",
                    f"تمام، هبلّغ {name} {that} {phrase}.",
                    f"ماشي، هحط ل{name} تاسك {that} {phrase}.",
                    f"حاضر، هسجّل ل{name} تاسك {task} على طول.",
                ]
            )
        return self._pick(
            [
                f"حاضر، هحط ل{name} تاسك {task}.",
                f"تمام، هسجّل تاسك {task} ل{name} حالًا.",
                f"ماشي، هضيف ل{name} تاسك {task}.",
            ]
        )

    def _ack_remind(self, intent: Intent) -> str:
        name = _name(intent)
        phrase = shorten(intent.details.action_phrase or intent.task)
        when = _suffix(intent.details.when)
        that = "إنها" if _is_female(intent) else "إنه"
        return self._pick(
            [
                f"حاضر، هفكّر {name} {phrase}{when}.",
                f"تمام، هفكّر {name} {that} {phrase}{when}.",
            ]
        )

    def _ack_meeting(self, intent: Intent) -> str:
        subject = shorten(intent.details.subject or intent.task) or "الاجتماع"
        when = _suffix(intent.details.when)
        return self._pick([f"تمام، هجهّز {subject}{when}.", f"حاضر، هرتّب {subject}{when}."])

    def _ack_send(self, intent: Intent) -> str:
        name = _name(intent)
        subject = shorten(intent.details.subject or intent.task)
        return self._pick(
            [f"حاضر، هجهّز إرسال {subject} ل{name}.", f"تمام، هبعت {subject} ل{name} حالًا."]
        )

    # ------------------------------------------------------------ clarifications
    async def clarify(self, intent: Intent | None, missing: list[str], reason: str | None) -> str:
        action = intent.action if intent else Action.CREATE_TASK
        name = _name(intent) if intent else None
        task = shorten(intent.task) if intent else None

        if reason == "unknown_person" and name:
            found = "موجودة" if intent and _is_female(intent) else "موجود"
            return self._pick(
                [
                    f"معلش، {name} مش {found} عندي في السيستم لسه. أحطها لمين؟",
                    f"آسفة، مش لاقية {name} عندي. التاسك دي لمين؟",
                ]
            )
        if action is Action.SEND_MESSAGE:
            return "حاضر، أبعت لمين؟" if "assignee" in missing else f"تمام، أبعت إيه ل{name}؟"
        if action is Action.REMIND:
            if "assignee" in missing:
                return self._pick(["حاضر، أفكّر مين؟", "تمام، مين اللي أفكّره؟"])
            return f"حاضر، أفكّر {name} بإيه بالظبط؟"

        if "assignee" in missing and "task" in missing:
            return self._pick(
                ["حاضر، التاسك دي لمين وعن إيه بالظبط؟", "تمام، أحط التاسك لمين، والمطلوب إيه؟"]
            )
        if "assignee" in missing:
            if task:
                return self._pick(
                    [f"تمام، تاسك {task} دي لمين؟", f"حاضر، مين اللي أحطله تاسك {task}؟"]
                )
            return "تمام، التاسك دي لمين؟"
        verb = "تعمل" if intent and _is_female(intent) else "يعمل"
        return self._pick(
            [f"حاضر، {name} {verb} إيه بالظبط؟", f"تمام، التاسك ل{name} عن إيه بالظبط؟"]
        )

    async def unsupported(self, intent: Intent | None, offer: Intent | None) -> str:
        if intent is None:
            return await self.not_understood()
        if offer is not None:
            name = _name(offer)
            what = {
                Action.REMIND: "أبعت تذكيرات",
                Action.SEND_MESSAGE: "أبعت رسايل",
                Action.SCHEDULE_MEETING: "أحجز اجتماعات",
            }.get(intent.action, "أعمل الطلب ده")
            return self._pick(
                [
                    f"لسه مقدرش {what}، بس ممكن أحطها تاسك ل{name}. أحطها؟",
                    f"أنا لسه مبعرفش {what}، تحب أسجّلها تاسك ل{name}؟",
                ]
            )
        if intent.action is Action.SCHEDULE_MEETING:
            subject = shorten(intent.details.subject or intent.task) or "الاجتماع"
            return f"لسه مقدرش أحجز {subject}، بس أقدر أسجّل تاسكات. فيه حاجة تانية؟"
        if intent.action is Action.SEND_MESSAGE:
            subject = shorten(intent.details.subject) or "رسايل"
            return f"لسه مقدرش أبعت {subject}، بس أقدر أحط تاسكات. فيه حاجة تانية؟"
        return self._pick(
            [
                "الطلب ده لسه مش متاح عندي، بس أقدر أحط تاسكات. فيه حاجة تانية؟",
                "معلش، لسه مبعرفش أعمل ده. أقدر أسجّل تاسكات لو تحب.",
            ]
        )

    # ------------------------------------------------------------ conversational
    async def greeting(self) -> str:
        return self._pick(
            [
                "أهلاً بحضرتك، أنا جاهزة. تحب أسجّل إيه؟",
                "أهلاً، أنا معاك. قولّي محتاج إيه وأنا أسجّله.",
            ]
        )

    async def farewell(self, reason: FarewellReason) -> str:
        if reason == "timeout":
            return "مش سامعة حاجة، هقفل دلوقتي. مع السلامة."
        if reason == "technical":
            return "معلش فيه مشكلة تقنية، هقفل دلوقتي وهتابع الطلبات اللي قولتها."
        return self._pick(
            ["مع السلامة، وهتابع كل الطلبات اللي قولتها.", "تمام، مع السلامة. هتابع الطلبات بنفسي."]
        )

    async def small_talk(self) -> str:
        return self._pick(["العفو، تحت أمرك. فيه حاجة تانية؟", "ولا يهمك. محتاج حاجة تانية؟"])

    async def declined(self) -> str:
        return self._pick(["ماشي، مش هسجّلها. فيه حاجة تانية؟", "تمام، لغيتها. تحب حاجة تانية؟"])

    async def not_understood(self) -> str:
        return self._pick(
            ["معلش مسمعتش كويس، ممكن تعيد تاني؟", "آسفة، مفهمتش قصدك. ممكن تقولها بشكل تاني؟"]
        )

    async def give_up_clarifying(self) -> str:
        return "معلش مش قادرة أفهم، ممكن تقول الطلب كله مرة واحدة؟"

    async def system_error(self) -> str:
        return "معلش حصلت مشكلة عندي ومتسجلش الطلب، ممكن تقوله تاني؟"
