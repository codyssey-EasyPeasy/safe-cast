"""simulate.parse_form 시험(네트워크·GLM 없음)."""

from __future__ import annotations

import simulate

LABELS = {"moved_recently": "최근 이사", "lawsuit": "소송"}


def test_parse_form_ok_and_filters_unknown():
    vt, fl, ok = simulate.parse_form('설명 {"victim_type": "dating", "flags": ["lawsuit", "zzz", "lawsuit"]}', LABELS)
    assert ok and vt == "dating" and fl == ["lawsuit"]


def test_parse_form_bad():
    assert simulate.parse_form("모르겠어요", LABELS) == (None, [], False)
    vt, fl, ok = simulate.parse_form('{"victim_type": "friend", "flags": []}', LABELS)
    assert ok and vt is None and fl == []


def test_form_text_marks_prefill():
    t = simulate._form_text(LABELS, "family", ["lawsuit"])
    assert "family=가정폭력 (선택됨)" in t and "lawsuit: 소송 (체크됨)" in t and "최근 이사 (체크됨)" not in t
