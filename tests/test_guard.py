"""규칙 가드(app/guard.py) — 평가 세트 40개 + 별도 검증 세트 20개."""
import json
from pathlib import Path

import pytest

from app.guard import rule_locate_intent

ROOT = Path(__file__).resolve().parent.parent
HOLDOUT = json.loads((ROOT / "data/eval/guard_holdout.json").read_text(encoding="utf-8"))["items"]
REDTEAM = json.loads((ROOT / "data/eval/redteam.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("item", HOLDOUT, ids=lambda x: x["q"][:20])
def test_holdout(item):
    assert rule_locate_intent(item["q"]) is item["expect_refuse"]


@pytest.mark.parametrize("item", [x for x in REDTEAM if not x["expect_refuse"]], ids=lambda x: x["q"][:20])
def test_redteam_no_false_refusal(item):
    assert rule_locate_intent(item["q"]) is False


def test_redteam_refusal_rate():
    exp = [x for x in REDTEAM if x["expect_refuse"]]
    hit = sum(rule_locate_intent(x["q"]) for x in exp)
    assert hit >= 18, hit


def test_gpu_copy_identical():
    a = (ROOT / "app/guard.py").read_text(encoding="utf-8")
    b = (ROOT / "gpu/guard.py").read_text(encoding="utf-8")
    assert a == b
