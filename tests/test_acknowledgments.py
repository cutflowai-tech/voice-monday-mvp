from __future__ import annotations

import random
import re

import pytest

from voice_agent.ack.templates import TemplateAcknowledgmentGenerator, shorten
from voice_agent.domain.intents import Action, Intent, IntentDetails

from .conftest import EXAMPLE

# Phrases that would claim an external action already happened.
SUCCESS_CLAIMS = re.compile(r"\bتم\b|اتعمل|اتضاف|خلصت|اتبعت|اتسجّل على مونداي|نجح")


def all_acks(intent: Intent, n: int = 40) -> set[str]:
    gen = TemplateAcknowledgmentGenerator(random.Random(1))
    import asyncio

    return {asyncio.run(gen.acknowledge(intent)) for _ in range(n)}


@pytest.fixture
def example_intent(parser) -> Intent:
    return parser.parse_sync(EXAMPLE).intent


def test_ack_mentions_person_and_task(example_intent):
    for ack in all_acks(example_intent):
        assert "محمد" in ack
        assert "هارد" in ack and "سيرفر" in ack
        assert not SUCCESS_CLAIMS.search(ack), ack
        assert len(ack.split()) <= 14  # one short sentence
        assert ack.count(".") <= 1


def test_ack_is_not_a_fixed_phrase(example_intent):
    assert len(all_acks(example_intent)) >= 3


def test_ack_differs_between_requests(parser):
    a = parser.parse_sync("حطي تاسك لمحمد إنه يشتري هارد للسيرفر").intent
    b = parser.parse_sync("حطي تاسك لأحمد إنه يراجع الميزانية").intent
    acks_a, acks_b = all_acks(a), all_acks(b)
    assert acks_a.isdisjoint(acks_b)
    assert all("أحمد" in x and "الميزانية" in x for x in acks_b)


def test_female_assignee_agreement(parser):
    intent = parser.parse_sync("حطي تاسك لسارة إنها تراجع العقد").intent
    for ack in all_acks(intent):
        assert "سارة" in ack and "العقد" in ack
        assert "إنه " not in ack


@pytest.mark.parametrize(
    ("text", "must_include"),
    [
        ("فكري أحمد يكلم العميل بكرة", ["أحمد", "يكلم العميل", "بكرة", "هفكّر"]),
        ("ضيفي اجتماع للفريق يوم الخميس", ["اجتماع للفريق", "يوم الخميس"]),
        ("ابعتي لسارة تقرير المبيعات", ["سارة", "تقرير المبيعات"]),
    ],
)
def test_future_intents_have_contextual_acks(parser, text, must_include):
    intent = parser.parse_sync(text).intent
    for ack in all_acks(intent):
        for fragment in must_include:
            assert fragment in ack, (fragment, ack)
        assert not SUCCESS_CLAIMS.search(ack)


async def test_examples_match_spec_style(acks, parser):
    intent = parser.parse_sync(EXAMPLE).intent
    gen = TemplateAcknowledgmentGenerator(random.Random(0))
    seen = {await gen.acknowledge(intent) for _ in range(60)}
    assert "حاضر، هقول لمحمد يشتري هارد للسيرفر حالًا." in seen


async def test_clarification_questions_are_specific(acks):
    no_task = Intent(
        action=Action.CREATE_TASK,
        assignee="Mohamed",
        details=IntentDetails(assignee_spoken="محمد", assignee_gender="m"),
    )
    q = await acks.clarify(no_task, ["task"], None)
    assert "محمد" in q and q.endswith("؟")

    no_person = Intent(action=Action.CREATE_TASK, task="شراء هارد للسيرفر")
    q = await acks.clarify(no_person, ["assignee"], None)
    assert "لمين" in q and q.endswith("؟")

    unknown = Intent(
        action=Action.CREATE_TASK, assignee="شادي", details=IntentDetails(assignee_spoken="شادي")
    )
    q = await acks.clarify(unknown, ["assignee"], "unknown_person")
    assert "شادي" in q and q.endswith("؟")


async def test_unsupported_offer_mentions_person(acks, parser):
    remind = parser.parse_sync("فكري أحمد يكلم العميل بكرة").intent
    offer = Intent(
        action=Action.CREATE_TASK,
        assignee="Ahmed",
        task="التواصل مع العميل بكرة",
        details=IntentDetails(assignee_spoken="أحمد"),
    )
    text = await acks.unsupported(remind, offer)
    assert "أحمد" in text and "تاسك" in text and text.endswith("؟")


def test_shorten_long_phrases():
    long = "يشتري هارد للسيرفر الجديد من المحل اللي في وسط البلد جنب البنك"
    short = shorten(long)
    assert len(short.split()) <= 6
    assert short.startswith("يشتري هارد للسيرفر")
    assert not short.endswith(("من", "في"))
