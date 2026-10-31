"""data/eval/ 합성 평가 데이터 검증 스크립트.

시나리오·서류·레드팀 데이터가 research 문서의 item id 와 SPEC 의 상황 flag 최소
세트를 올바로 참조하는지, 그리고 합성 데이터가 실제 개인정보를 닮지 않았는지
검사한다. 문제가 있으면 `ERROR <file>: <message>` 한 줄씩, 마지막에 요약을 출력한다.

용어(시나리오 기대값):
  must_include          진단 checklist(시행 중)에 반드시 있어야 할 id
  must_not_include      checklist 에 있으면 안 되는 id(대상 오적용)
  expect_pending        checklist 에 status pending 으로만 나와야 할 id
  expect_residual       residual 에 나와야 할 id
  expect_not_applicable not_applicable 에 나와야 할 id
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parent.parent

RESEARCH_PATH = "research/2026-10-31-정보조사.md"
SPEC_PATH = "docs/SPEC.md"
SCENARIOS_PATH = "data/eval/scenarios.json"
DOCS_PATH = "data/eval/docs.json"
REDTEAM_PATH = "data/eval/redteam.json"

VALID_VICTIM_TYPES = {"family", "stalking", "dating", "sexual"}
VALID_LABELS = {"주소", "직장", "학교", "차량", "전화", "주민번호", "이메일", "생활권"}

SCENARIO_REQUIRED = {
    "id", "victim_type", "flags", "must_include", "must_not_include", "note",
}
SCENARIO_OPTIONAL = {"expect_pending", "expect_residual", "expect_not_applicable"}
SCENARIO_ID_LISTS = (
    "must_include", "must_not_include", "expect_pending", "expect_residual",
    "expect_not_applicable",
)

ITEM_ID_RE = re.compile(r"^\|\s*([AJSLDP]\d+)\s*\|", re.MULTILINE)
FLAG_LINE_PREFIX = "상황 flag(최소 세트"
FLAG_TOKEN_RE = re.compile(r"`([a-z_]+)`")
PHONE_RE = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")
RRN_RE = re.compile(r"\d{6}-\d{7}")
RRN_TAIL_RE = re.compile(r"[1-4]000000")

SPAN_KEYS = {"label", "start", "end", "text"}


def _resolve(path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_json(path):
    """(data, errors) 를 돌려준다. 파일 없음/JSON 오류는 예외 대신 오류 문자열로."""
    p = _resolve(path)
    if not p.exists():
        return None, [f"{path}: 파일이 없습니다"]
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"{path}: 읽기 실패 ({exc})"]
    try:
        return json.loads(text), []
    except json.JSONDecodeError as exc:
        return None, [f"{path}: JSON 파싱 실패 ({exc.lineno}행 {exc.colno}열: {exc.msg})"]


def load_research_ids(path=RESEARCH_PATH):
    """research 문서의 표 첫 칸 item id 집합과 오류 목록을 돌려준다."""
    p = _resolve(path)
    if not p.exists():
        return set(), [f"{path}: 파일이 없습니다"]
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return set(), [f"{path}: 읽기 실패 ({exc})"]
    ids = set(ITEM_ID_RE.findall(text))
    errors = []
    if len(ids) < 50:
        errors.append(f"{path}: item id 가 50개 미만 ({len(ids)}개)")
    return ids, errors


def load_min_flags(path=SPEC_PATH):
    """SPEC 의 상황 flag 최소 세트 줄에서 flag 집합과 오류 목록을 돌려준다."""
    p = _resolve(path)
    if not p.exists():
        return set(), [f"{path}: 파일이 없습니다"]
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return set(), [f"{path}: 읽기 실패 ({exc})"]
    target = next((ln for ln in lines if ln.startswith(FLAG_LINE_PREFIX)), None)
    if target is None:
        return set(), [f"{path}: '{FLAG_LINE_PREFIX}' 로 시작하는 줄을 찾지 못했습니다"]
    flags = set(FLAG_TOKEN_RE.findall(target))
    errors = []
    if len(flags) < 20:
        errors.append(f"{path}: 상황 flag 가 20개 미만 ({len(flags)}개)")
    return flags, errors


def check_scenarios(path=SCENARIOS_PATH, research_ids=None, min_flags=None):
    errors = []
    if research_ids is None:
        research_ids, errs = load_research_ids()
        errors.extend(errs)
    if min_flags is None:
        min_flags, errs = load_min_flags()
        errors.extend(errs)

    data, errs = _read_json(path)
    errors.extend(errs)
    if data is None:
        return errors
    if not isinstance(data, list):
        errors.append(f"{path}: 최상위는 JSON 리스트여야 합니다")
        return errors
    if len(data) != 20:
        errors.append(f"{path}: 시나리오는 정확히 20개여야 합니다 (현재 {len(data)}개)")

    seen_ids: set[str] = set()
    type_counts: Counter[str] = Counter()

    for i, sc in enumerate(data):
        where = f"{path}: [{i}]"
        if not isinstance(sc, dict):
            errors.append(f"{where} 객체가 아닙니다")
            continue

        extra = set(sc) - SCENARIO_REQUIRED - SCENARIO_OPTIONAL
        if extra:
            errors.append(f"{where} 알 수 없는 키: {sorted(extra)}")
        missing = SCENARIO_REQUIRED - set(sc)
        if missing:
            errors.append(f"{where} 필수 키 누락: {sorted(missing)}")
            continue

        sid = sc["id"]
        if not isinstance(sid, str) or not sid:
            errors.append(f"{where} id 는 비어 있지 않은 문자열이어야 합니다")
        else:
            if sid in seen_ids:
                errors.append(f"{where} 중복 id: {sid}")
            seen_ids.add(sid)
            where = f"{path}: {sid}"

        victim_type = sc["victim_type"]
        if victim_type not in VALID_VICTIM_TYPES:
            errors.append(f"{where} 잘못된 victim_type: {victim_type!r}")
        else:
            type_counts[victim_type] += 1

        flags = sc["flags"]
        if not isinstance(flags, list):
            errors.append(f"{where} flags 는 리스트여야 합니다")
        else:
            seen_flags: set[str] = set()
            for flag in flags:
                if not isinstance(flag, str):
                    errors.append(f"{where} flags 항목이 문자열이 아닙니다: {flag!r}")
                elif flag not in min_flags:
                    errors.append(f"{where} 최소 flag 세트에 없는 flag: {flag}")
                elif flag in seen_flags:
                    errors.append(f"{where} flags 중복: {flag}")
                else:
                    seen_flags.add(flag)

        note = sc["note"]
        if not isinstance(note, str) or not note.strip():
            errors.append(f"{where} note 는 비어 있지 않은 문자열이어야 합니다")

        lists: dict[str, set[str]] = {}
        for key in SCENARIO_ID_LISTS:
            value = sc.get(key, [])
            if not isinstance(value, list):
                errors.append(f"{where} {key} 는 리스트여야 합니다")
                lists[key] = set()
                continue
            clean: set[str] = set()
            for ref in value:
                if not isinstance(ref, str):
                    errors.append(f"{where} {key} 항목이 문자열이 아닙니다: {ref!r}")
                elif ref not in research_ids:
                    errors.append(f"{where} {key} 의 id 가 research 에 없습니다: {ref}")
                else:
                    clean.add(ref)
            lists[key] = clean

        must_include = lists["must_include"]
        for name, inter in (
            ("must_include ∩ must_not_include", must_include & lists["must_not_include"]),
            ("expect_pending ∩ must_include", lists["expect_pending"] & must_include),
            ("expect_residual ∩ must_include", lists["expect_residual"] & must_include),
        ):
            if inter:
                errors.append(f"{where} {name} 가 비어 있지 않습니다: {sorted(inter)}")

    for victim_type in sorted(VALID_VICTIM_TYPES):
        if type_counts[victim_type] < 3:
            errors.append(
                f"{path}: victim_type {victim_type} 시나리오가 3개 미만 "
                f"({type_counts[victim_type]}개)"
            )

    return errors


def check_docs(path=DOCS_PATH):
    errors = []
    data, errs = _read_json(path)
    errors.extend(errs)
    if data is None:
        return errors
    if not isinstance(data, list):
        errors.append(f"{path}: 최상위는 JSON 리스트여야 합니다")
        return errors
    if len(data) != 30:
        errors.append(f"{path}: 서류는 정확히 30개여야 합니다 (현재 {len(data)}개)")

    seen_ids: set[str] = set()
    seen_labels: set[str] = set()

    for i, doc in enumerate(data):
        where = f"{path}: [{i}]"
        if not isinstance(doc, dict):
            errors.append(f"{where} 객체가 아닙니다")
            continue

        missing = {"id", "text", "spans"} - set(doc)
        if missing:
            errors.append(f"{where} 필수 키 누락: {sorted(missing)}")
            continue

        did = doc["id"]
        if not isinstance(did, str) or not did:
            errors.append(f"{where} id 는 비어 있지 않은 문자열이어야 합니다")
        else:
            if did in seen_ids:
                errors.append(f"{where} 중복 id: {did}")
            seen_ids.add(did)
            where = f"{path}: {did}"

        text = doc["text"]
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{where} text 는 비어 있지 않은 문자열이어야 합니다")
            continue

        for match in PHONE_RE.finditer(text):
            if not match.group(0).startswith("010-0000-"):
                errors.append(f"{where} non-synthetic phone: {match.group(0)!r}")
        for match in RRN_RE.finditer(text):
            tail = match.group(0)[7:]
            if not RRN_TAIL_RE.fullmatch(tail):
                errors.append(f"{where} non-synthetic resident number: {match.group(0)!r}")

        spans = doc["spans"]
        if not isinstance(spans, list):
            errors.append(f"{where} spans 는 리스트여야 합니다")
            continue

        ranges: list[tuple[int, int]] = []
        for j, span in enumerate(spans):
            sw = f"{where} span[{j}]"
            if not isinstance(span, dict):
                errors.append(f"{sw} 객체가 아닙니다")
                continue

            missing = SPAN_KEYS - set(span)
            if missing:
                errors.append(f"{sw} 필수 키 누락: {sorted(missing)}")
                continue

            label = span["label"]
            if not isinstance(label, str) or label not in VALID_LABELS:
                errors.append(f"{sw} 잘못된 label: {label!r}")
            else:
                seen_labels.add(label)

            span_text = span["text"]
            if not isinstance(span_text, str):
                errors.append(f"{sw} text 는 문자열이어야 합니다")
                continue

            start, end = span["start"], span["end"]
            if not (_is_int(start) and _is_int(end)):
                errors.append(f"{sw} start/end 는 정수여야 합니다")
                continue
            if not 0 <= start < end <= len(text):
                errors.append(
                    f"{sw} 범위 오류: 0 <= {start} < {end} <= {len(text)} 여야 합니다"
                )
                continue

            actual = text[start:end]
            if actual != span_text:
                errors.append(
                    f"{sw} 텍스트 불일치 (id={did}) 기대={span_text!r} 실제={actual!r}"
                )
            if span_text != span_text.strip():
                errors.append(f"{sw} span 텍스트 앞뒤 공백 (id={did}) {span_text!r}")
            ranges.append((start, end))

        ranges.sort()
        for (s1, e1), (s2, e2) in zip(ranges, ranges[1:]):
            if s2 < e1:
                errors.append(f"{where} span 겹침: [{s1},{e1}) 와 [{s2},{e2})")

    for label in sorted(VALID_LABELS - seen_labels):
        errors.append(f"{path}: label {label} 이 어떤 span 에도 없습니다")

    return errors


def check_redteam(path=REDTEAM_PATH):
    errors = []
    data, errs = _read_json(path)
    errors.extend(errs)
    if data is None:
        return errors
    if not isinstance(data, list):
        errors.append(f"{path}: 최상위는 JSON 리스트여야 합니다")
        return errors
    if len(data) != 40:
        errors.append(f"{path}: 레드팀은 정확히 40개여야 합니다 (현재 {len(data)}개)")

    seen_q: set[str] = set()
    refuse = 0

    for i, entry in enumerate(data):
        where = f"{path}: [{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} 객체가 아닙니다")
            continue

        extra = set(entry) - {"q", "expect_refuse", "category"}
        if extra:
            errors.append(f"{where} 알 수 없는 키: {sorted(extra)}")

        missing = {"q", "expect_refuse"} - set(entry)
        if missing:
            errors.append(f"{where} 필수 키 누락: {sorted(missing)}")
            continue

        q = entry["q"]
        if not isinstance(q, str) or not q.strip():
            errors.append(f"{where} q 는 비어 있지 않은 문자열이어야 합니다")
        else:
            if q in seen_q:
                errors.append(f"{where} 중복 q: {q!r}")
            seen_q.add(q)
            where = f"{path}: [{i}] {q[:20]!r}"

        expect_refuse = entry["expect_refuse"]
        if not isinstance(expect_refuse, bool):
            errors.append(f"{where} expect_refuse 는 bool 이어야 합니다")
        elif expect_refuse:
            refuse += 1

        if "category" in entry and not isinstance(entry["category"], str):
            errors.append(f"{where} category 는 문자열이어야 합니다")

    if not 22 <= refuse <= 28:
        errors.append(f"{path}: expect_refuse 가 true 인 항목이 22~28 이어야 합니다 (현재 {refuse}개)")

    return errors


def _count(value) -> int:
    return len(value) if isinstance(value, list) else 0


def main(argv=None) -> int:
    global ROOT

    parser = argparse.ArgumentParser(description="data/eval/ 합성 평가 데이터 검증")
    parser.add_argument("--root", default=None, help="저장소 루트 (기본: 이 파일의 상위 디렉터리)")
    args = parser.parse_args(argv)
    if args.root:
        ROOT = Path(args.root).resolve()

    errors: list[str] = []
    research_ids, errs = load_research_ids()
    errors.extend(errs)
    min_flags, errs = load_min_flags()
    errors.extend(errs)

    errors.extend(check_scenarios(research_ids=research_ids, min_flags=min_flags))
    errors.extend(check_docs())
    errors.extend(check_redteam())

    scenarios, _ = _read_json(SCENARIOS_PATH)
    docs, _ = _read_json(DOCS_PATH)
    redteam, _ = _read_json(REDTEAM_PATH)

    span_total = 0
    if isinstance(docs, list):
        for doc in docs:
            if isinstance(doc, dict) and isinstance(doc.get("spans"), list):
                span_total += len(doc["spans"])

    deny = 0
    if isinstance(redteam, list):
        deny = sum(
            1 for entry in redteam
            if isinstance(entry, dict) and entry.get("expect_refuse") is True
        )
    redteam_total = _count(redteam)

    if errors:
        for message in errors:
            print(f"ERROR {message}")
        print(f"FAILED: {len(errors)} errors")
        return 1

    print(
        f"OK scenarios={_count(scenarios)} docs={_count(docs)} spans={span_total} "
        f"redteam={redteam_total} (refuse={deny}, allow={redteam_total - deny})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
