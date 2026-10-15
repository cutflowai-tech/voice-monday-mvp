"""Spoken tasks become clean Monday instructions."""

from __future__ import annotations

import pytest

from voice_agent.agent_tools.task_text import normalize_task


@pytest.mark.parametrize(
    ("spoken", "title"),
    [
        ("يشتري كاميرات بكرة", "اشتري كاميرات بكرة"),
        ("إنه يجيب الكهربائي بكرة", "هات الكهربائي بكرة"),
        ("يتصل بيكي ويعمل test", "اتصل بسارة واعمل test"),
        ("ييجي المكتب بعد بكرة.", "تعالى المكتب بعد بكرة"),
        ("يراجع الميزانية ويبعتها", "راجع الميزانية ويبعتها"),  # «يبعتها»: not in table
        ("الاجتماع بكرة الساعة تلاتة", "الاجتماع بكرة الساعة تلاتة"),
        ("اتصل بالعميل عشان يشتري", "اتصل بالعميل عشان يشتري"),
    ],
)
def test_titles_are_instructions_to_the_assignee(spoken, title):
    assert normalize_task(spoken) == title


def test_caller_pronouns_become_the_callers_name():
    assert normalize_task("يتصل بيا", caller_name="محمد") == "اتصل بمحمد"
    assert normalize_task("يبعت الورق ليا", caller_name="محمد") == "ابعت الورق لمحمد"
    assert normalize_task("يتصل بيا") == "اتصل بيا"  # unknown caller: left as said


def test_female_assignee_and_name_stripping():
    assert normalize_task("تجيب الورق", assignee_female=True) == "هاتي الورق"
    assert normalize_task("أحمد يشتري هارد", assignee_names=("أحمد",)) == "اشتري هارد"
    assert normalize_task("لأحمد إنه يشتري هارد", assignee_names=("أحمد",)) == "اشتري هارد"
