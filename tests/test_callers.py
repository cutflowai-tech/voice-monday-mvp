from __future__ import annotations

import pytest

from voice_agent.domain.callers import CallerDirectory, normalize_caller


def test_caller_numbers_and_internal_extensions():
    callers = CallerDirectory({"01012345678": "ahmed", "101": "mohamed"}, {"ahmed", "mohamed"})
    assert normalize_caller("+20 101 234 5678") == "201012345678"
    assert callers.lookup("0020-101-234-5678") == "ahmed"
    assert callers.lookup("101") == "mohamed"
    assert callers.lookup("102") is None


def test_unverified_caller_mapping_is_rejected():
    with pytest.raises(ValueError, match="invalid caller mapping"):
        CallerDirectory({"101": "not_in_people_directory"}, {"ahmed"})
