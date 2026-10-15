"""Turn a spoken task into a clean Monday title.

The caller says «حطي تاسك لأحمد يشتري كاميرات بكرة» and the title should read like an
instruction to Ahmed: «اشتري كاميرات بكرة». Pronouns are relative to the call, so they are
resolved to names: «يتصل بيا» (the caller) -> «اتصل بمحمد», «يتصل بيكي» (Sarah) ->
«اتصل بسارة».

The agent's prompt asks for this wording already; this is the deterministic safety net that
runs on the server before anything reaches Monday. It is deliberately conservative: only
verbs it knows, only at the start of the task or right after «و», and caller pronouns
only when the caller is known.
"""

from __future__ import annotations

import re

from voice_agent.nlu.normalize import normalize_token

# third-person imperfect (normalized) -> (imperative to a man, imperative to a woman)
IMPERATIVES: dict[str, tuple[str, str]] = {
    "يشتري": ("اشتري", "اشتري"),
    "يجيب": ("هات", "هاتي"),
    "يتصل": ("اتصل", "اتصلي"),
    "يكلم": ("كلّم", "كلّمي"),
    "يراجع": ("راجع", "راجعي"),
    "ييجي": ("تعالى", "تعالي"),
    "يبعت": ("ابعت", "ابعتي"),
    "يروح": ("روح", "روحي"),
    "يعمل": ("اعمل", "اعملي"),
    "يخلص": ("خلّص", "خلّصي"),
    "يجهز": ("جهّز", "جهّزي"),
    "ياخد": ("خد", "خدي"),
    "ينزل": ("انزل", "انزلي"),
    "يركب": ("ركّب", "ركّبي"),
    "يصلح": ("صلّح", "صلّحي"),
    "يكتب": ("اكتب", "اكتبي"),
    "يرد": ("رد", "ردي"),
    "يتابع": ("تابع", "تابعي"),
    "يدفع": ("ادفع", "ادفعي"),
    "يحجز": ("احجز", "احجزي"),
    "يطبع": ("اطبع", "اطبعي"),
    "يرفع": ("ارفع", "ارفعي"),
    "ينظم": ("نظّم", "نظّمي"),
    "يرتب": ("رتّب", "رتّبي"),
    "يسال": ("اسأل", "اسألي"),
    "يشوف": ("شوف", "شوفي"),
    "يقابل": ("قابل", "قابلي"),
    "يحضر": ("احضر", "احضري"),
    "يجرب": ("جرّب", "جرّبي"),
    "يتاكد": ("اتأكد", "اتأكدي"),
    "يبلغ": ("بلّغ", "بلّغي"),
    "يفكر": ("فكّر", "فكّري"),
    "يحط": ("حط", "حطي"),
    "يشيل": ("شيل", "شيلي"),
    "يغير": ("غيّر", "غيّري"),
    "يسلم": ("سلّم", "سلّمي"),
    "يقول": ("قول", "قولي"),
    "يفتح": ("افتح", "افتحي"),
    "يقفل": ("اقفل", "اقفلي"),
    "ينضف": ("نضّف", "نضّفي"),
    "يرسل": ("ارسل", "ارسلي"),
    "يحول": ("حوّل", "حوّلي"),
    "يجدد": ("جدّد", "جدّدي"),
    "يطلب": ("اطلب", "اطلبي"),
    "يستلم": ("استلم", "استلمي"),
    "يوصل": ("وصّل", "وصّلي"),
}
LEADING_FILLERS = {"انه", "انها", "ان", "انو"}

# preposition + pronoun about the CALLER ("me") -> preposition + caller's name
CALLER_PRONOUNS = {"بيا": "ب", "ليا": "ل", "عليا": "على ", "معايا": "مع ", "عندي": "عند "}
# preposition + pronoun about SARAH ("you", said to her) -> preposition + سارة
AGENT_PRONOUNS = {"بيكي": "ب", "ليكي": "ل", "عليكي": "على ", "معاكي": "مع ", "عندك": "عند "}
AGENT_NAME = "سارة"


def _imperative(token: str, female: bool) -> str | None:
    key = normalize_token(token)
    if key.startswith("ت") and len(key) > 2:  # «تشتري» (she buys) -> «يشتري»
        key = "ي" + key[1:]
    forms = IMPERATIVES.get(key)
    return (forms[1] if female else forms[0]) if forms else None


def normalize_task(
    task: str,
    *,
    assignee_names: tuple[str, ...] = (),
    assignee_female: bool = False,
    caller_name: str | None = None,
) -> str:
    words = task.strip().strip("،,.!؟?").split()
    assignee_keys = {normalize_token(n) for n in assignee_names if n}
    # drop «إنه/إنها» and the assignee's own name at the start
    while words and (
        normalize_token(words[0]) in LEADING_FILLERS
        or normalize_token(words[0]).removeprefix("ل") in assignee_keys
    ):
        words.pop(0)

    out: list[str] = []
    for i, word in enumerate(words):
        has_waw = word.startswith("و") and len(word) > 2
        bare = word[1:] if has_waw else word
        if i == 0 or has_waw:
            verb = _imperative(bare, assignee_female)
            if verb:
                out.append(("و" + verb) if has_waw else verb)
                continue
        key = normalize_token(bare)
        prefix = "و" if has_waw else ""
        if key in {normalize_token(k) for k in AGENT_PRONOUNS}:
            prep = next(v for k, v in AGENT_PRONOUNS.items() if normalize_token(k) == key)
            out.append(f"{prefix}{prep}{AGENT_NAME}")
            continue
        if caller_name and key in {normalize_token(k) for k in CALLER_PRONOUNS}:
            prep = next(v for k, v in CALLER_PRONOUNS.items() if normalize_token(k) == key)
            out.append(f"{prefix}{prep}{caller_name}")
            continue
        out.append(word)
    title = " ".join(out)
    return re.sub(r"\s+", " ", title).strip() or task.strip()
