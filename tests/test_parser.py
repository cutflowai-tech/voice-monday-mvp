from __future__ import annotations

import pytest

from voice_agent.domain.intents import Action
from voice_agent.nlu.normalize import normalize_text

from .conftest import EXAMPLE


async def test_example_command_produces_expected_intent(parser):
    result = await parser.parse(EXAMPLE)
    assert result.status == "ok"
    assert result.intent.public_dict() | {"details": None} == {
        "action": "create_task",
        "assignee": "Mohamed",
        "task": "شراء هارد للسيرفر",
        "details": None,
    }
    assert result.intent.details.assignee_spoken == "محمد"
    assert result.intent.details.assignee_key == "mohamed"
    assert result.intent.details.action_phrase == "يشتري هارد للسيرفر"


@pytest.mark.parametrize(
    "text",
    [
        "حطي تاسك لمحمد إنه يشتري هارد للسيرفر",
        "حطي تاسك لمحمد انه يشتري هارد للسيرفر",
        "حُطّي تاسك لمُحمّد إنّه يشتري هارد للسيرفر.",
        "لو سمحتي حطي تاسك لمحمد إنه يشتري هارد للسيرفر",
        "حطي تاسك لمحمد إنه يشتري هارد للسيرفر لو سمحتي",
        "اعملي تاسك لمحمد يشتري هارد للسيرفر",
        "ضيفي تاسك جديدة لمحمد إنه يشتري هارد للسيرفر",
        "سجلي مهمة لمحمد إنه يشتري هارد للسيرفر",
        "حطي لمحمد تاسك إنه يشتري هارد للسيرفر",
        "عايز تاسك لمحمد إنه يشتري هارد للسيرفر",
        "حطي تاسك ل محمد إنه يشتري هارد للسيرفر",
        "حطي task لمحمد إنه يشتري هارد للسيرفر",
        "حطي تاسك لمحمد إنه لازم يشتري هارد للسيرفر",
        "حطي تاسك لمحمد إنه هيشتري هارد للسيرفر",
        "قولي لمحمد يشتري هارد للسيرفر",
        "خلي محمد يشتري هارد للسيرفر",
        "عايز محمد يشتري هارد للسيرفر",
        "حطي تاسك إنه يشتري هارد للسيرفر لمحمد",
        "حطي تاسك لحمادة إنه يشتري هارد للسيرفر",
        "حطي تاسك للباشمهندس محمد إنه يشتري هارد للسيرفر",
    ],
)
async def test_wording_variations(parser, text):
    result = await parser.parse(text)
    assert result.status == "ok", text
    assert result.intent.action is Action.CREATE_TASK
    assert result.intent.assignee == "Mohamed"
    assert result.intent.task == "شراء هارد للسيرفر"


async def test_noun_task_is_kept_and_spoken_back_as_verb(parser):
    result = await parser.parse("اعملي مهمة جديدة لمحمد: شراء هارد للسيرفر")
    assert result.status == "ok"
    assert result.intent.task == "شراء هارد للسيرفر"
    assert result.intent.details.action_phrase == "يشتري هارد للسيرفر"


@pytest.mark.parametrize(
    ("text", "task"),
    [
        ("حطي تاسك لأحمد إنه يكلم العميل", "التواصل مع العميل"),
        ("حطي تاسك لأحمد يراجع الميزانية", "مراجعة الميزانية"),
        ("حطي تاسك لمحمد إنه يجيب الورق من البنك", "إحضار الورق من البنك"),
        ("حطي تاسك لأحمد يروح البنك", "يروح البنك"),  # unknown verb: kept verbatim
    ],
)
async def test_verb_to_task_title(parser, text, task):
    result = await parser.parse(text)
    assert result.status == "ok"
    assert result.intent.task == task


async def test_female_assignee(parser):
    result = await parser.parse("حطي تاسك لسارة إنها تراجع العقد")
    assert result.intent.assignee == "Sara"
    assert result.intent.details.assignee_gender == "f"
    assert result.intent.task == "مراجعة العقد"


async def test_unknown_person_is_extracted_not_guessed(parser):
    result = await parser.parse("حطي تاسك لشادي إنه يشتري هارد للسيرفر")
    assert result.status == "ok"
    assert result.intent.assignee == "شادي"
    assert result.intent.details.assignee_key is None


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        ("حطي تاسك", ["assignee", "task"]),
        ("حطي تاسك لمحمد", ["task"]),
        ("حطي تاسك إنه يشتري هارد للسيرفر", ["assignee"]),
    ],
)
async def test_incomplete_commands_report_missing_fields(parser, text, missing):
    result = await parser.parse(text)
    assert result.status == "incomplete"
    assert result.missing == missing


async def test_other_intents_are_recognised(parser):
    remind = (await parser.parse("فكري أحمد يكلم العميل بكرة")).intent
    assert remind.action is Action.REMIND
    assert remind.assignee == "Ahmed"
    assert remind.details.action_phrase == "يكلم العميل"
    assert remind.details.when == "بكرة"

    meeting = (await parser.parse("ضيفي اجتماع للفريق يوم الخميس")).intent
    assert meeting.action is Action.SCHEDULE_MEETING
    assert meeting.details.subject == "اجتماع للفريق"
    assert meeting.details.when == "يوم الخميس"

    send = (await parser.parse("ابعتي لسارة تقرير المبيعات")).intent
    assert send.action is Action.SEND_MESSAGE
    assert send.assignee == "Sara"
    assert send.details.subject == "تقرير المبيعات"


@pytest.mark.parametrize("text", ["مع السلامة", "خلاص كده شكرا", "اقفلي الخط", "باي"])
async def test_end_call(parser, text):
    assert (await parser.parse(text)).intent.action is Action.END_CALL


@pytest.mark.parametrize("text", ["السلام عليكم", "شكرا", "ألو"])
async def test_greetings_do_not_end_the_call(parser, text):
    assert (await parser.parse(text)).intent.action is Action.SMALL_TALK


@pytest.mark.parametrize("text", ["الجو حلو النهارده", "ايه الأخبار في الماتش", "بلا بلا"])
async def test_unrelated_text_is_unknown(parser, text):
    assert (await parser.parse(text)).status == "unknown"


async def test_confirm_and_deny(parser):
    assert (await parser.parse("آه حطيها")).intent.action is Action.CONFIRM
    assert (await parser.parse("لأ")).intent.action is Action.DENY
    assert (await parser.parse("مش عارف")).status == "unknown"  # not a refusal


async def test_clarification_helpers(parser):
    person = await parser.parse_person("لمحمد")
    assert person.intent.assignee == "Mohamed"
    task = await parser.parse_task_answer("إنه يشتري هارد للسيرفر")
    assert task.intent.task == "شراء هارد للسيرفر"


def test_normalization():
    assert normalize_text("حُطّي تاسك لأحمد، إنّه") == "حطي تاسك لاحمد انه"
