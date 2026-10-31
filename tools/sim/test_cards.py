"""cards.py 시험: v2 카드가 시뮬레이터 스키마로 맞춰지고, GLM 에 보낼 카드에 정답·채점 정보가 없는지."""

from __future__ import annotations

import json

import cards


def test_v2_counts_and_schema():
    cs = cards.load("v2")
    kinds = [c["kind"] for c in cs]
    assert kinds.count("user") == 20 and kinds.count("attacker") == 12 and kinds.count("benign") == 6
    assert len({c["id"] for c in cs}) == len(cs)
    for c in cs:
        assert isinstance(c["persona"], str) and c["opening"] and c["max_turns"] > 0
        if c["kind"] == "user":
            assert c["scenario_id"].startswith("sc") and c["follow_ups"]
            assert all(set(h) == {"fact", "reveal_when"} for h in c["hidden_facts"])
        else:
            assert set(c["channels"]) <= {"search", "answer", "intake", "rewrite"}


def test_sim_view_hides_ground_truth():
    for c in cards.load("v2"):
        s = json.dumps(cards.sim_view(c), ensure_ascii=False)
        for bad in ("maps_to", "opening_implies", "success_criteria", "expected", "scenario_id", "exposable"):
            assert bad not in s, (c["id"], bad)


def test_subset_filter():
    assert [c["id"] for c in cards.load("v2", "uc03,atk05")] == ["uc03", "atk05"]
    assert len(cards.load("v2", "benign")) == 6
