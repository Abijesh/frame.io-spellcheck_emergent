"""Self-check for ai_service.parse_allowlist, the brand/name/slang allowlist
parser. Pure-logic test: no Gemini call, no network. Run directly:
    python backend/tests/test_allowlist.py
(also pytest-discoverable, since the functions are named test_*).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.ai_service import parse_allowlist, _with_allowlist  # noqa: E402


def test_splits_on_comma_and_newline():
    assert parse_allowlist("Zellic, Abijesh\nFrameCheck") == [
        "Zellic",
        "Abijesh",
        "FrameCheck",
    ]


def test_strips_whitespace_and_drops_empties():
    assert parse_allowlist(" Zellic ,, \n  ,Abijesh\n") == ["Zellic", "Abijesh"]


def test_dedupes_case_insensitively_keeping_first_casing():
    assert parse_allowlist("Zellic, zellic, ZELLIC") == ["Zellic"]


def test_none_and_empty_return_empty_list():
    assert parse_allowlist(None) == []
    assert parse_allowlist("") == []
    assert parse_allowlist("   ") == []


def test_with_allowlist_appends_clause_only_when_nonempty():
    base = "SYSTEM PROMPT"
    assert _with_allowlist(base, []) == base
    assert _with_allowlist(base, None) == base
    out = _with_allowlist(base, ["Zellic", "Abijesh"])
    assert out.startswith(base)
    assert "Zellic, Abijesh" in out


if __name__ == "__main__":
    test_splits_on_comma_and_newline()
    test_strips_whitespace_and_drops_empties()
    test_dedupes_case_insensitively_keeping_first_casing()
    test_none_and_empty_return_empty_list()
    test_with_allowlist_appends_clause_only_when_nonempty()
    print("OK")
