"""Deterministic, rule-based parser for Egyptian Arabic voice commands.

Supported shapes (and many spelling/ASR variations of them)::

    حطي تاسك لمحمد إنه يشتري هارد للسيرفر          -> create_task
    اعملي مهمة جديدة لمحمد: شراء هارد للسيرفر        -> create_task
    قولي لمحمد يشتري هارد للسيرفر                   -> create_task
    خلي محمد يشتري هارد للسيرفر                      -> create_task
    فكري أحمد يكلم العميل بكرة                      -> remind
    ضيفي اجتماع للفريق يوم الخميس                    -> schedule_meeting
    ابعتي لسارة تقرير المبيعات                        -> send_message
    مع السلامة / اقفلي الخط                           -> end_call

The parser never guesses missing information: it returns ``incomplete`` with the
list of missing fields so the conversation layer can ask one clarification question.
"""

from __future__ import annotations

from dataclasses import dataclass

from voice_agent.domain.intents import Action, Intent, IntentDetails, ParseResult
from voice_agent.domain.people import PeopleDirectory
from voice_agent.nlu.base import IntentParser
from voice_agent.nlu.normalize import Token, join_orig, normalize_token, tokenize

# --------------------------------------------------------------------------- lexicon
# All entries are in *normalized* form (see normalize_token).

TASK_WORDS = {"تاسك", "تسك", "التاسك", "تاسكه", "task", "مهمه", "المهمه", "todo"}
NEW_WORDS = {"جديد", "جديده"}
CREATE_VERBS = {
    "حطي", "حط", "حطيلي", "حطلي", "اعملي", "اعمل", "اعمليلي", "اعمللي", "ضيفي", "ضيف",
    "ضيفيلي", "ضيفلي", "سجلي", "سجل", "سجليلي", "اكتبي", "اكتب", "اكتبيلي", "افتحي",
    "افتح", "زودي", "زود", "نزلي", "add", "create",
}  # fmt: skip
WANT_WORDS = {
    "عايز", "عاوز", "عايزه", "عاوزه", "عايزين", "عاوزين", "محتاج", "محتاجه", "محتاجين",
    "حابب", "حابه",
}  # fmt: skip
RELAY_LAM_VERBS = {"قولي", "قول", "قوليله", "بلغي", "بلغ", "وصلي"}
RELAY_DIRECT_VERBS = {"خلي", "خليلي", "كلفي", "كلف", "خليه"}
RELAY_MIN_VERBS = {"اطلبي", "اطلب"}  # "اطلبي من محمد ..."
REMIND_VERBS = {"فكري", "فكر", "فكريلي", "ذكري", "ذكر", "نبهي", "نبه"}
SEND_VERBS = {"ابعتي", "ابعت", "ابعتيلي", "بعتي", "ارسلي", "ارسل", "ابعتيله", "ابعتيلها"}
MEETING_WORDS = {"اجتماع", "ميتنج", "ميتينج", "ميتينغ", "meeting", "اجتماعات", "الاجتماع"}
MEETING_VERBS = CREATE_VERBS | {"حددي", "حدد", "رتبي", "رتب", "ظبطي", "ظبط", "احجزي", "احجز"}
BENEFACTIVE = {"لي", "ليا", "لينا"}
CONNECTORS = {"انه", "انها", "ان", "بان", "بانه", "بانها", "عشان", "علشان", "يعني", "انهم", "انو"}
FILLERS = {"لازم", "ضروري", "هو", "هي", "يقدر", "تقدر", "المفروض", "محتاج", "محتاجه"}
HONORIFICS = {
    "استاذ", "الاستاذ", "مستر", "باشمهندس", "الباشمهندس", "مهندس", "المهندس", "دكتور",
    "الدكتور", "مدام", "استاذه", "الاستاذه", "كابتن", "الكابتن", "انسه",
}  # fmt: skip
NOT_VERBS = {"يوم", "يونيو", "يوليو", "يناير", "ياريت", "يعني", "يا", "يلا"}
# Words that look like "ل + word" but are not names.
NOT_NAMES = {"حد", "اي", "كل", "سه", "حين", "ما", "ان", "انه", "انها", "يه", "ده", "دي"}

POLITE_PREFIXES = [
    ("لو", "سمحتي"), ("لو", "سمحت"), ("من", "فضلك"), ("بعد", "اذنك"), ("ممكن",),
    ("معلش",), ("please",), ("الو",), ("طيب",), ("بقولك",), ("بصي",), ("بص",),
    ("ياريت",), ("يا", "ريت"), ("اه",), ("ايوه",),
]  # fmt: skip
POLITE_SUFFIXES = [
    ("لو", "سمحتي"), ("لو", "سمحت"), ("من", "فضلك"), ("بعد", "اذنك"), ("please",),
    ("شكرا",), ("متشكر",), ("متشكره",), ("حالا",), ("بسرعه",), ("لو", "سمحتي", "شكرا"),
]  # fmt: skip

END_SEQUENCES = [
    ("مع", "السلامه"), ("باي",), ("bye",), ("سلام",), ("اقفلي",), ("اقفل",), ("اقفلي", "الخط"),
    ("انهي", "المكالمه"), ("خلاص", "كده"), ("كفايه", "كده"), ("يلا", "سلام"),
    ("مفيش", "حاجه", "تاني"), ("مش", "عايز", "حاجه", "تاني"), ("مش", "عايزه", "حاجه", "تاني"),
    ("كده", "تمام", "شكرا"),
]  # fmt: skip
SMALL_TALK = {
    "شكرا", "متشكر", "متشكره", "مرسي", "تسلمي", "تسلم", "الو", "اهلا", "السلام", "عليكم",
    "ازيك", "صباح", "الخير", "مساء", "النور", "الف",
}  # fmt: skip
CONFIRM_WORDS = {
    "اه", "ايوه", "ايوا", "ماشي", "تمام", "اوكي", "ok", "okay", "yes", "حطيها", "حطها",
    "يلا", "طبعا", "اكيد", "موافق", "ياريت", "اعمليها", "ضيفيها",
}  # fmt: skip
DENY_WORDS = {"لا", "لاء", "no", "بلاش", "لالا"}  # not "مش": "مش عارف" is not a refusal

DAYS = {
    "السبت", "الحد", "الاحد", "الاتنين", "الاثنين", "التلات", "الثلاثاء", "التلاتاء",
    "الاربع", "الاربعاء", "الخميس", "الجمعه",
}  # fmt: skip
TIME_SINGLE = {
    "بكره", "النهارده", "انهارده", "الصبح", "بالليل", "العصر", "المغرب", "الضهر", "دلوقتي",
    "بدري", "الساعه", "الاسبوع", "الشهر", "قبل", "يوم",
} | DAYS  # fmt: skip
AFTER_TIME = {"بكره", "الضهر", "العصر", "ساعه", "ساعتين", "يومين", "اسبوع", "المغرب", "الشغل"}
TRAILING_DROP = {"في", "على", "علي", "من", "و", "مع", "ل", "عشان", "اللي", "بتاع"}

# Imperfect-verb stem (normalized, without the ي/ت prefix) -> verbal noun (masdar).
VERB_TO_NOUN = {
    "شتري": "شراء", "جيب": "إحضار", "كلم": "التواصل مع", "تصل": "الاتصال", "بعت": "إرسال",
    "رسل": "إرسال", "راجع": "مراجعة", "صلح": "إصلاح", "ركب": "تركيب", "جهز": "تجهيز",
    "حضر": "تحضير", "خلص": "إنهاء", "تابع": "متابعة", "طلب": "طلب", "دفع": "دفع",
    "كتب": "كتابة", "غير": "تغيير", "نضف": "تنظيف", "حجز": "حجز", "جدد": "تجديد",
    "سلم": "تسليم", "نزل": "تنزيل", "رفع": "رفع", "ظبط": "ضبط", "ضبط": "ضبط",
    "حدث": "تحديث", "عمل": "عمل", "فحص": "فحص", "جرب": "تجربة", "قابل": "مقابلة",
    "زور": "زيارة", "شحن": "شحن", "نقل": "نقل", "صور": "تصوير", "رد": "الرد",
    "سال": "سؤال", "شيل": "إزالة", "مسح": "مسح", "صمم": "تصميم", "طبع": "طباعة",
    "حول": "تحويل", "ستلم": "استلام", "شوف": "مراجعة", "شغل": "تشغيل", "وقف": "إيقاف",
    "فتح": "فتح", "قفل": "قفل", "نظم": "تنظيم", "رتب": "ترتيب", "بلغ": "إبلاغ",
    "سدد": "سداد", "حسب": "حساب", "حل": "حل", "كمل": "استكمال", "عدل": "تعديل",
}  # fmt: skip
# Verbal noun -> stem, used to speak a noun task back as a verb ("شراء" -> "يشتري").
NOUN_TO_STEM: dict[str, str] = {}
for _stem, _noun in VERB_TO_NOUN.items():
    if " " not in _noun and _stem not in {"سال", "ستلم", "شوف", "ضبط"}:
        NOUN_TO_STEM.setdefault(normalize_token(_noun), _stem)
NOUNS_NORM = {normalize_token(n.split()[0]) for n in VERB_TO_NOUN.values()}


@dataclass(frozen=True)
class PersonMatch:
    assignee: str
    spoken: str
    key: str | None
    gender: str | None


@dataclass(frozen=True)
class TaskPhrase:
    task: str
    phrase: str | None


class EgyptianArabicParser(IntentParser):
    """Rule-based parser. Pure CPU, sub-millisecond, no external calls."""

    def __init__(self, directory: PeopleDirectory):
        self.directory = directory

    # ------------------------------------------------------------------ public API
    async def parse(self, text: str) -> ParseResult:
        return self.parse_sync(text)

    async def parse_person(self, text: str) -> ParseResult:
        tokens = self._strip_polite(tokenize(text))
        for i in range(len(tokens)):
            match = self._person_at(tokens, i, lam="optional", allow_unknown=False)
            if match:
                person, _ = match
                return ParseResult(
                    status="incomplete",
                    intent=self._intent(Action.CREATE_TASK, person, None),
                    missing=["task"],
                    confidence=0.8,
                )
        # A single unknown word is most likely a name we do not know yet.
        if len(tokens) == 1:
            word = tokens[0]
            name = word.orig[1:] if word.norm.startswith("ل") and len(word.norm) > 3 else word.orig
            person = PersonMatch(assignee=name, spoken=name, key=None, gender=None)
            return ParseResult(
                status="incomplete",
                intent=self._intent(Action.CREATE_TASK, person, None),
                missing=["task"],
                confidence=0.4,
            )
        return ParseResult.unknown()

    async def parse_task_answer(self, text: str) -> ParseResult:
        tokens = self._strip_polite(tokenize(text))
        tokens = self._skip_leading(tokens, CONNECTORS | FILLERS | WANT_WORDS | {"خليه", "خليها"})
        phrase = self._task_from_rest(tokens, gender=None)
        if not phrase:
            return ParseResult.unknown()
        return ParseResult(
            status="incomplete",
            intent=self._intent(Action.CREATE_TASK, None, phrase),
            missing=["assignee"],
            confidence=0.7,
        )

    def parse_sync(self, text: str) -> ParseResult:
        all_tokens = tokenize(text)
        normalized = " ".join(t.norm for t in all_tokens)
        if not all_tokens:
            return ParseResult.unknown(normalized)
        tokens = self._strip_polite(all_tokens)

        result: ParseResult | None = None
        if tokens:
            for matcher in (
                self._match_meeting,
                self._match_remind,
                self._match_send,
                self._match_create,
                self._match_relay,
                self._match_want_person,
                self._match_bare_person,
            ):
                result = matcher(tokens)
                if result:
                    break
        if result is None:
            result = self._match_conversational(all_tokens)
        if result is None:
            return ParseResult.unknown(normalized)
        result.normalized_text = normalized
        return result

    # ------------------------------------------------------------------ matchers
    def _match_create(self, toks: list[Token]) -> ParseResult | None:
        n, i = len(toks), 0
        cue_want = cue_verb = cue_task = False
        if i < n and toks[i].norm in WANT_WORDS:
            cue_want, i = True, i + 1
        if i < n and toks[i].norm in CREATE_VERBS:
            cue_verb, i = True, i + 1
        while i < n and toks[i].norm in BENEFACTIVE:
            i += 1
        if i < n and toks[i].norm in TASK_WORDS:
            cue_task, i = True, self._skip_new(toks, i + 1)

        person: PersonMatch | None = None
        match = self._person_at(toks, i, lam="required", allow_unknown=True)
        if match:
            person, i = match[0], i + match[1]
        if not cue_task and i < n and toks[i].norm in TASK_WORDS:
            cue_task, i = True, self._skip_new(toks, i + 1)

        rest = self._skip_leading(toks[i:], CONNECTORS | FILLERS)
        # Without a task word, only "حطي لمحمد يشتري هارد" (verb + person + verb phrase) counts.
        if not cue_task and not (cue_verb and person and rest and self._verb_info(rest[0])[0]):
            return None
        if not (cue_task or cue_verb or cue_want):
            return None

        if person is None and rest:
            # Person mentioned at the end: "حطي تاسك إنه يشتري هارد لمحمد"
            tail = self._person_at(rest, len(rest) - 1, lam="required", allow_unknown=False)
            if tail:
                person, rest = tail[0], rest[:-1]
        gender = person.gender if person else None
        phrase = self._task_from_rest(rest, gender)
        return self._build(Action.CREATE_TASK, person, phrase, confidence=0.9)

    def _match_relay(self, toks: list[Token]) -> ParseResult | None:
        head = toks[0].norm
        if head in RELAY_LAM_VERBS:
            match = self._person_at(toks, 1, lam="required", allow_unknown=True)
        elif head in RELAY_DIRECT_VERBS:
            match = self._person_at(toks, 1, lam="optional", allow_unknown=True)
        elif head in RELAY_MIN_VERBS and len(toks) > 2 and toks[1].norm == "من":
            match = self._person_at(toks, 2, lam="none", allow_unknown=True)
            if match:
                match = (match[0], match[1] + 1)
        else:
            return None
        if not match:
            return None
        person, used = match
        rest = self._skip_leading(toks[1 + used :], CONNECTORS | FILLERS)
        if rest and not self._verb_info(rest[0])[0] and not self._is_ba_noun(rest[0]):
            return None  # "قولي لمحمد إن الاجتماع اتلغى" is a message, not a task
        phrase = self._task_from_rest(rest, person.gender)
        return self._build(Action.CREATE_TASK, person, phrase, confidence=0.8)

    def _match_want_person(self, toks: list[Token]) -> ParseResult | None:
        if toks[0].norm not in WANT_WORDS or len(toks) < 3:
            return None
        match = self._person_at(toks, 1, lam="none", allow_unknown=False)
        if not match:
            return None
        person, used = match
        rest = self._skip_leading(toks[1 + used :], CONNECTORS | FILLERS)
        if not rest or not self._verb_info(rest[0])[0]:
            return None
        return self._build(
            Action.CREATE_TASK, person, self._task_from_rest(rest, person.gender), 0.75
        )

    def _match_bare_person(self, toks: list[Token]) -> ParseResult | None:
        match = self._person_at(toks, 0, lam="none", allow_unknown=False)
        if not match:
            return None
        person, used = match
        rest = self._skip_leading(toks[used:], CONNECTORS | FILLERS)
        if not rest or not self._verb_info(rest[0])[0]:
            return None
        return self._build(
            Action.CREATE_TASK, person, self._task_from_rest(rest, person.gender), 0.6
        )

    def _match_remind(self, toks: list[Token]) -> ParseResult | None:
        if toks[0].norm not in REMIND_VERBS:
            return None
        match = self._person_at(toks, 1, lam="optional", allow_unknown=True)
        person, used = match if match else (None, 0)
        rest = self._skip_leading(toks[1 + used :], CONNECTORS | FILLERS)
        core, when = self._split_when(rest)
        gender = person.gender if person else None
        if core and self._is_ba_noun(core[0]):
            core = [Token(orig=core[0].orig[1:], norm=core[0].norm[1:]), *core[1:]]
        phrase = self._task_from_rest(core, gender)
        result = self._build(Action.REMIND, person, phrase, confidence=0.85)
        result.intent.details.when = when
        return result

    def _match_meeting(self, toks: list[Token]) -> ParseResult | None:
        idx = next((k for k, t in enumerate(toks[:4]) if t.norm in MEETING_WORDS), None)
        if idx is None:
            return None
        if idx > 0 and toks[0].norm not in MEETING_VERBS | WANT_WORDS:
            return None
        core, when = self._split_when(toks[idx:])
        subject = join_orig(self._trim_trailing(core))[:255]
        intent = Intent(
            action=Action.SCHEDULE_MEETING,
            task=subject,
            details=IntentDetails(subject=subject, when=when),
        )
        return ParseResult(status="ok", intent=intent, confidence=0.85)

    def _match_send(self, toks: list[Token]) -> ParseResult | None:
        if toks[0].norm not in SEND_VERBS:
            return None
        rest = toks[1:]
        match = self._person_at(rest, 0, lam="required", allow_unknown=True)
        person: PersonMatch | None = None
        if match:
            person, rest = match[0], rest[match[1] :]
        elif rest:
            tail = self._person_at(rest, len(rest) - 1, lam="required", allow_unknown=False)
            if tail:
                person, rest = tail[0], rest[:-1]
        rest = self._trim_trailing(self._skip_leading(rest, CONNECTORS))
        subject = join_orig(rest)[:255] or None
        intent = Intent(
            action=Action.SEND_MESSAGE,
            assignee=person.assignee if person else None,
            task=subject,
            details=IntentDetails(
                assignee_spoken=person.spoken if person else None,
                assignee_key=person.key if person else None,
                assignee_gender=person.gender if person else None,
                subject=subject,
            ),
        )
        missing = [f for f, v in (("assignee", person), ("task", subject)) if not v]
        return ParseResult(
            status="incomplete" if missing else "ok",
            intent=intent,
            missing=missing,
            confidence=0.8,
        )

    def _match_conversational(self, toks: list[Token]) -> ParseResult | None:
        norms = [t.norm for t in toks]
        for seq in END_SEQUENCES:
            if self._contains(norms, seq):
                return ParseResult(status="ok", intent=Intent(action=Action.END_CALL), confidence=1)
        if len(norms) <= 3:
            if all(n in CONFIRM_WORDS for n in norms):
                return ParseResult(status="ok", intent=Intent(action=Action.CONFIRM), confidence=1)
            if norms[0] in DENY_WORDS:
                return ParseResult(status="ok", intent=Intent(action=Action.DENY), confidence=1)
        if all(n in SMALL_TALK for n in norms):
            return ParseResult(status="ok", intent=Intent(action=Action.SMALL_TALK), confidence=1)
        return None

    # ------------------------------------------------------------------ helpers
    def _build(
        self,
        action: Action,
        person: PersonMatch | None,
        phrase: TaskPhrase | None,
        confidence: float,
    ) -> ParseResult:
        missing = []
        if person is None:
            missing.append("assignee")
        if phrase is None:
            missing.append("task")
        return ParseResult(
            status="incomplete" if missing else "ok",
            intent=self._intent(action, person, phrase),
            missing=missing,
            confidence=confidence,
        )

    @staticmethod
    def _intent(action: Action, person: PersonMatch | None, phrase: TaskPhrase | None) -> Intent:
        return Intent(
            action=action,
            assignee=person.assignee if person else None,
            task=phrase.task[:255] if phrase else None,
            details=IntentDetails(
                assignee_spoken=person.spoken if person else None,
                assignee_key=person.key if person else None,
                assignee_gender=person.gender if person else None,
                action_phrase=phrase.phrase if phrase else None,
            ),
        )

    def _person_at(
        self, toks: list[Token], i: int, *, lam: str, allow_unknown: bool
    ) -> tuple[PersonMatch, int] | None:
        """Match a person starting at ``toks[i]``.

        ``lam``: "required" (لمحمد), "optional" (محمد / لمحمد) or "none" (محمد).
        Returns (match, tokens consumed).
        """
        if i < 0 or i >= len(toks):
            return None
        tok = toks[i]
        norm, orig, used, had_lam = tok.norm, tok.orig, 1, False
        if lam != "none":
            if norm == "ل" and i + 1 < len(toks):
                inner = self._person_at(toks, i + 1, lam="none", allow_unknown=allow_unknown)
                return (inner[0], inner[1] + 1) if inner else None
            if norm.startswith("لل") and len(norm) > 3:
                norm, orig, had_lam = "ال" + norm[2:], "ال" + orig[2:], True
            elif norm.startswith("ل") and len(norm) > 2:
                candidate = norm[1:]
                if candidate in HONORIFICS or self.directory.lookup(candidate) or allow_unknown:
                    norm, orig, had_lam = candidate, orig[1:], True
        if lam == "required" and not had_lam:
            return None
        if norm in HONORIFICS:
            nxt = self._person_at(toks, i + 1, lam="none", allow_unknown=allow_unknown)
            return (nxt[0], nxt[1] + used) if nxt else None

        person = self.directory.lookup(norm)
        if person:
            match = PersonMatch(person.name_en, person.name_ar, person.key, person.gender)
            return match, used
        if (
            allow_unknown
            and had_lam
            and len(norm) >= 3
            and not norm.startswith("ال")
            and norm not in NOT_NAMES
            and norm not in NOUNS_NORM
            and norm not in TASK_WORDS
            and not self._verb_info(Token(orig, norm))[0]
        ):
            return PersonMatch(orig, orig, None, None), used
        return None

    @staticmethod
    def _verb_info(tok: Token) -> tuple[bool, str | None]:
        """Return (looks like an imperfect verb, verbal noun if known)."""
        n = tok.norm
        candidates = [n]
        if len(n) > 3 and n[0] in "هب" and n[1] in "يت":
            candidates.append(n[1:])  # هيشتري / بيشتري
        for c in candidates:
            if len(c) >= 3 and c[0] in "يت" and c[1:] in VERB_TO_NOUN:
                return True, VERB_TO_NOUN[c[1:]]
        for c in candidates:
            if len(c) >= 3 and c[0] == "ي" and c not in NOT_VERBS:
                return True, None
        return False, None

    @staticmethod
    def _is_ba_noun(tok: Token) -> bool:
        return tok.norm.startswith("ب") and len(tok.norm) > 3

    def _task_from_rest(self, rest: list[Token], gender: str | None) -> TaskPhrase | None:
        rest = self._trim_trailing(self._strip_polite(rest))
        if not rest:
            return None
        head = rest[0]
        is_verb, noun = self._verb_info(head)
        if is_verb:
            phrase = join_orig(rest)
            task = f"{noun} {join_orig(rest[1:])}".strip() if noun else phrase
            return TaskPhrase(task=task, phrase=phrase)
        if self._is_ba_noun(head) and head.norm[1:] in NOUNS_NORM:
            rest = [Token(orig=head.orig[1:], norm=head.norm[1:]), *rest[1:]]
            head = rest[0]
        task = join_orig(rest)
        stem = NOUN_TO_STEM.get(head.norm)
        phrase = None
        if stem:
            prefix = "ت" if gender == "f" else "ي"
            phrase = f"{prefix}{stem} {join_orig(rest[1:])}".strip()
        return TaskPhrase(task=task, phrase=phrase)

    @staticmethod
    def _split_when(toks: list[Token]) -> tuple[list[Token], str | None]:
        for idx in range(1, len(toks)):
            norm = toks[idx].norm
            nxt = toks[idx + 1].norm if idx + 1 < len(toks) else ""
            if norm == "بعد":
                if nxt in AFTER_TIME:
                    return toks[:idx], join_orig(toks[idx:])
                continue
            if norm == "يوم" and nxt not in DAYS and nxt not in {"الجمعه", "الحد"}:
                continue
            if norm in TIME_SINGLE:
                core = EgyptianArabicParser._trim_trailing(toks[:idx])
                return core, join_orig(toks[idx:])
        return toks, None

    @staticmethod
    def _skip_new(toks: list[Token], i: int) -> int:
        while i < len(toks) and toks[i].norm in NEW_WORDS:
            i += 1
        return i

    @staticmethod
    def _skip_leading(toks: list[Token], words: set[str]) -> list[Token]:
        i = 0
        while i < len(toks) and toks[i].norm in words:
            i += 1
        return toks[i:]

    @staticmethod
    def _trim_trailing(toks: list[Token]) -> list[Token]:
        end = len(toks)
        while end > 0 and toks[end - 1].norm in TRAILING_DROP:
            end -= 1
        return toks[:end]

    @staticmethod
    def _strip_polite(toks: list[Token]) -> list[Token]:
        changed = True
        while changed and toks:
            changed = False
            norms = [t.norm for t in toks]
            for seq in POLITE_PREFIXES:
                if len(norms) > len(seq) and tuple(norms[: len(seq)]) == seq:
                    toks, changed = toks[len(seq) :], True
                    break
            norms = [t.norm for t in toks]
            for seq in sorted(POLITE_SUFFIXES, key=len, reverse=True):
                if len(norms) > len(seq) and tuple(norms[-len(seq) :]) == seq:
                    toks, changed = toks[: -len(seq)], True
                    break
        return toks

    @staticmethod
    def _contains(norms: list[str], seq: tuple[str, ...]) -> bool:
        k = len(seq)
        return any(tuple(norms[j : j + k]) == seq for j in range(len(norms) - k + 1))
