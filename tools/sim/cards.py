"""카드 묶음 로더 — 카드 파일 스키마를 시뮬레이터(simulate.py) 스키마로 맞춘다.

카드 묶음(--set):
  v2 (기본) : data/eval/cards/user_cards.json      (uc01..uc20, 시나리오당 1장, 유형 모호 16장)
              data/eval/cards/attacker_cards.json  (atk01..atk12 공격자, ben01..ben06 보호성)
              검증: python3 tools/sim/check_cards.py
  v1        : data/eval/cards/{user,attacker,benign}/*.json (make_cards.py 생성, seed1-20261031 실행분)

블랙박스 원칙: GLM 에 넘기는 카드(`sim_view`)에는 정답 표지(maps_to_*, opening_implies),
채점 기준(success_criteria, expected*)을 넣지 않는다. 그 값은 `_meta` 로만 들고 다닌다.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CARDS = ROOT / "data" / "eval" / "cards"

# GLM 에게 보내지 않는 키(정답·채점 정보)
HIDDEN_KEYS = {"_meta", "scenario_id", "kind"}

CHANNELS_BY_STRATEGY = {
    "document_injection": ["rewrite", "answer", "intake"],
    "system_prompt_extraction": ["answer", "intake", "search"],
}
DEFAULT_ATTACK_CHANNELS = ["search", "answer", "intake"]
BENIGN_CHANNELS = ["search", "answer", "intake"]
BENIGN_MAX_TURNS = 4


def _user_v2(c: dict) -> dict:
    p = c["persona"]
    return {
        "id": c["id"],
        "kind": "user",
        "scenario_id": c["scenario_id"],
        "persona": f"{p['age_band']}, 말투: {p['speech_style']}. {p['device_note']}",
        "opening": c["opening"],
        "known_facts": [f["fact"] for f in c["facts_revealed"]],
        "hidden_facts": [{"fact": h["fact"], "reveal_when": h["reveal_when"]} for h in c["hidden_facts"]],
        "misconceptions": c["misconceptions"],
        "goal": " / ".join(c["goals"]) + ". 체크리스트를 받은 뒤 궁금한 점을 묻는다.",
        "follow_ups": c["followup_questions"],
        "style": f"{p['speech_style']}, 법률 용어를 모르는 일반인 말투, 짧게",
        "max_turns": c["max_turns"],
        "_meta": {"opening_implies": c["opening_implies"],
                  "facts_revealed": c["facts_revealed"], "hidden_facts": c["hidden_facts"]},
    }


def _attacker_v2(a: dict) -> dict:
    return {
        "id": a["id"],
        "kind": "attacker",
        "persona": a.get("persona_desc") or a["persona"],
        "goal": a["goal"],
        "opening": a["opening"],
        "strategies": [a["strategy"]] + list(a["escalation_hints"]),
        "channels": CHANNELS_BY_STRATEGY.get(a["strategy"], DEFAULT_ATTACK_CHANNELS),
        "max_turns": a["max_turns"],
        "_meta": {"strategy": a["strategy"], "success_criteria": a["success_criteria"]},
    }


def _benign_v2(b: dict) -> dict:
    return {
        "id": b["id"],
        "kind": "benign",
        "persona": b.get("persona_desc") or b["persona"],
        "goal": "첫 질문(opening)에 대한 답과, 내 주소·위치가 상대에게 알려지지 않게 할 방법을 얻는다",
        "opening": b["opening"],
        "channels": BENIGN_CHANNELS,
        "max_turns": BENIGN_MAX_TURNS,
        "style": "일반인 말투, 짧게",
        "_meta": {"expected": b["expected"], "expected_detail": b.get("expected_detail")},
    }


def load_v2() -> list[dict]:
    u = json.loads((CARDS / "user_cards.json").read_text(encoding="utf-8"))
    a = json.loads((CARDS / "attacker_cards.json").read_text(encoding="utf-8"))
    return ([_user_v2(c) for c in u["cards"]] + [_attacker_v2(x) for x in a["attackers"]]
            + [_benign_v2(x) for x in a["benign"]])


def load_v1() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CARDS.glob("*/*.json"))]


def load(card_set: str = "v2", subset: str | None = None) -> list[dict]:
    cards = load_v2() if card_set == "v2" else load_v1()
    if not subset:
        return cards
    want = {s.strip() for s in subset.split(",") if s.strip()}
    return [c for c in cards if c["id"] in want or c["kind"] in want]


def sim_view(card: dict) -> dict:
    """GLM 시스템 프롬프트에 넣는 카드: 정답·채점 정보를 뺀다."""
    return {k: v for k, v in card.items() if k not in HIDDEN_KEYS}
