"""data/eval/cards/user_cards.json 검증 스크립트.

다중 턴 시뮬레이터용 사용자 카드가 깊이 1 시나리오(정답)와 정확히 맞는지 본다.

검사 항목:
  - 스키마(필수 키·타입), 카드 id 중복, max_turns == 6, followup_questions 2~4개
  - scenario_id 가 sc01..sc20 를 하나씩 덮는지
  - 모든 flag id 가 data/knowledge.json 의 flags 에 있는지, victim_type 이 4종 중 하나인지
  - opening_implies + facts_revealed + hidden_facts 의 maps_to_flags 합집합 == 시나리오 flags
  - maps_to_victim_type 이 모두 시나리오 victim_type 과 같고, 적어도 한 곳에서 드러나는지
  - 시작 시 피해 유형이 모호한 카드(opening_implies.victim_type 이 null)가 6장 이상인지
  - 첫 메시지가 모든 것을 드러내지 않는지(숨은 사실 중 flag 또는 유형을 담은 것이 하나 이상)
  - 첫 메시지에 법률 용어가 없는지, 실명·연락처처럼 보이는 문자열이 없는지

문제가 있으면 `ERROR <card>: <message>` 를 한 줄씩 출력하고 종료 코드 1.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CARDS = ROOT / "data/eval/cards/user_cards.json"
SCENARIOS = ROOT / "data/eval/scenarios.json"
KNOWLEDGE = ROOT / "data/knowledge.json"

VICTIM_TYPES = {"family", "stalking", "dating", "sexual"}
SPEECH_STYLES = {"겁먹음", "용어 모름", "돌려 말함", "잘못 알고 있음", "급함"}
CARD_KEYS = {
    "id": str, "scenario_id": str, "persona": dict, "opening": str,
    "opening_implies": dict, "facts_revealed": list, "hidden_facts": list,
    "misconceptions": list, "goals": list, "followup_questions": list,
    "max_turns": int,
}
PERSONA_KEYS = ("age_band", "speech_style", "device_note")
MIN_AMBIGUOUS = 6
# 첫 메시지는 일상어여야 한다: 제도·법률 용어가 나오면 안 된다.
LEGAL_TERMS = (
    "보호명령", "잠정조치", "교부제한", "교부 제한", "열람제한", "사실혼", "피부양자",
    "송달", "사용본거지", "가명조서", "가정폭력처벌법", "스토킹처벌법", "법 제", "조항",
    "임차권등기", "주민등록법", "소송구조", "비실명",
)
# 실제 개인정보처럼 보이는 것: 전화번호·주민번호 형태
PII_PATTERNS = (
    re.compile(r"01[016789]-?\d{3,4}-?\d{4}"),
    re.compile(r"\d{6}-?[1-4]\d{6}"),
)


def _load(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def check(cards_path: Path = CARDS) -> list[str]:
    errors: list[str] = []
    raw = _load(cards_path)
    cards = raw["cards"] if isinstance(raw, dict) else raw
    scenarios = {s["id"]: s for s in _load(SCENARIOS)}
    flag_ids = {f["id"] for f in _load(KNOWLEDGE)["flags"]}

    def err(cid: str, msg: str) -> None:
        errors.append(f"ERROR {cid}: {msg}")

    seen_ids: set[str] = set()
    seen_sc: dict[str, str] = {}
    ambiguous = 0

    for i, c in enumerate(cards):
        cid = c.get("id", f"#{i}")
        for k, t in CARD_KEYS.items():
            if k not in c:
                err(cid, f"필수 키 없음: {k}")
            elif not isinstance(c[k], t) or (t is int and isinstance(c[k], bool)):
                err(cid, f"{k} 타입이 {t.__name__} 가 아님")
        if any(k not in c or not isinstance(c[k], CARD_KEYS[k]) for k in CARD_KEYS):
            continue
        if cid in seen_ids:
            err(cid, "카드 id 중복")
        seen_ids.add(cid)

        sid = c["scenario_id"]
        if sid not in scenarios:
            err(cid, f"없는 scenario_id: {sid}")
            continue
        if sid in seen_sc:
            err(cid, f"scenario_id {sid} 가 {seen_sc[sid]} 와 중복")
        seen_sc[sid] = cid
        sc = scenarios[sid]

        p = c["persona"]
        for k in PERSONA_KEYS:
            if not isinstance(p.get(k), str) or not p.get(k):
                err(cid, f"persona.{k} 없음")
        if p.get("speech_style") not in SPEECH_STYLES:
            err(cid, f"persona.speech_style 이 목록 밖: {p.get('speech_style')}")

        if c["max_turns"] != 6:
            err(cid, "max_turns 는 6 이어야 함")
        if not 2 <= len(c["followup_questions"]) <= 4:
            err(cid, "followup_questions 는 2~4개")
        if not c["goals"]:
            err(cid, "goals 비어 있음")
        for name in ("misconceptions", "goals", "followup_questions"):
            if not all(isinstance(x, str) and x for x in c[name]):
                err(cid, f"{name} 항목은 비어 있지 않은 문자열이어야 함")

        # 사실 → flag·유형 합집합
        sources: list[tuple[str, dict]] = [("opening_implies", c["opening_implies"])]
        sources += [(f"facts_revealed[{j}]", f) for j, f in enumerate(c["facts_revealed"])]
        sources += [(f"hidden_facts[{j}]", f) for j, f in enumerate(c["hidden_facts"])]
        flags: set[str] = set()
        victims: list[str] = []
        for where, src in sources:
            if not isinstance(src, dict):
                err(cid, f"{where} 는 객체여야 함")
                continue
            fl = src.get("flags") if where == "opening_implies" else src.get("maps_to_flags")
            vt = src.get("victim_type") if where == "opening_implies" else src.get("maps_to_victim_type")
            if not isinstance(fl, list):
                err(cid, f"{where} flag 목록 없음")
                fl = []
            if where != "opening_implies" and not isinstance(src.get("fact"), str):
                err(cid, f"{where}.fact 없음")
            if where.startswith("hidden") and not isinstance(src.get("reveal_when"), str):
                err(cid, f"{where}.reveal_when 없음")
            for f in fl:
                if f not in flag_ids:
                    err(cid, f"{where}: knowledge.json 에 없는 flag {f}")
            flags.update(fl)
            if vt is not None:
                if vt not in VICTIM_TYPES:
                    err(cid, f"{where}: 잘못된 victim_type {vt}")
                victims.append(vt)

        want = set(sc["flags"])
        if flags != want:
            missing, extra = sorted(want - flags), sorted(flags - want)
            err(cid, f"flag 합집합 불일치 (빠짐 {missing}, 넘침 {extra})")
        if not victims:
            err(cid, "victim_type 이 어디서도 드러나지 않음")
        elif set(victims) != {sc["victim_type"]}:
            err(cid, f"victim_type 불일치: {sorted(set(victims))} != {sc['victim_type']}")

        if c["opening_implies"].get("victim_type") is None:
            ambiguous += 1
        if not any(h.get("maps_to_flags") or h.get("maps_to_victim_type")
                   for h in c["hidden_facts"] if isinstance(h, dict)):
            err(cid, "숨은 사실 중 flag·유형을 담은 것이 없음(첫 메시지가 다 드러냄)")

        text_all = json.dumps(c, ensure_ascii=False)
        for term in LEGAL_TERMS:
            if term in c["opening"]:
                err(cid, f"첫 메시지에 법률 용어: {term}")
        for pat in PII_PATTERNS:
            if pat.search(text_all):
                err(cid, f"개인정보처럼 보이는 문자열: {pat.pattern}")

    for sid in scenarios:
        if sid not in seen_sc:
            errors.append(f"ERROR cards: 시나리오 {sid} 의 카드 없음")
    if ambiguous < MIN_AMBIGUOUS:
        errors.append(f"ERROR cards: 시작 시 유형 모호 카드 {ambiguous}장 < {MIN_AMBIGUOUS}")

    print(f"cards={len(cards)} scenarios_covered={len(seen_sc)}/{len(scenarios)} "
          f"ambiguous_at_start={ambiguous} errors={len(errors)}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cards", type=Path, default=CARDS)
    args = ap.parse_args()
    errors = check(args.cards)
    for e in errors:
        print(e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
