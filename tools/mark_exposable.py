"""조문 검색 결과 노출 여부 표시.

`data/laws/articles.jsonl` 을 제자리에서 고쳐 모든 레코드에 두 키를 추가한다.
- exposable: 검색 결과로 노출해도 되는지 (bool)
- excerpt: 노출할 발췌문 (str | null)

열람·교부·발급 절차 조문은 DENY 에 넣어 절대 노출하지 않는다.
일부 조문은 EXCERPT 에 넣어 보호 조항이 포함된 문단만 발췌해 노출한다.

재실행해도 같은 파일이 나오도록 멱등이다(기존 키 순서를 유지한 채 끝에
두 키를 덮어쓴다).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DATA_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "laws" / "articles.jsonl"
)

# 절차 조문: 검색 결과에서 원천 차단한다.
DENY: set[tuple[str, str]] = {
    ("가사소송법", "제10조의2"),
    ("가정보호심판규칙", "제43조"),
    ("가정보호심판규칙", "제87조"),
    ("가족관계의 등록 등에 관한 규칙", "제27조"),
    ("가족관계의 등록 등에 관한 법률", "제15조"),
    ("개인정보 보호법", "제35조"),
    ("개인정보 보호법", "제38조"),
    ("검사와 사법경찰관의 상호협력과 일반적 수사준칙에 관한 규정", "제69조"),
    ("경찰수사규칙", "제87조"),
    ("공직선거관리규칙", "제13조"),
    ("공직선거법", "제37조"),
    ("공직선거법", "제40조"),
    ("공직선거법", "제46조"),
    ("민사소송규칙", "제37조의3"),
    ("민사소송법", "제162조"),
    ("민사소송법", "제163조의2"),
    ("민사소송법", "제208조"),
    ("부동산등기법", "제19조"),
    ("부동산등기법", "제48조"),
    ("부동산등기법", "제74조"),
    ("상법", "제317조"),
    ("위치정보의 보호 및 이용 등에 관한 법률", "제19조"),
    ("위치정보의 보호 및 이용 등에 관한 법률", "제26조"),
    ("위치정보의 보호 및 이용 등에 관한 법률", "제29조"),
    ("자동차관리법", "제12조의2"),
    ("자동차관리법", "제7조"),
    ("자동차등록규칙", "제10조"),
    ("자동차등록규칙", "제12조"),
    ("자동차등록령", "제22조"),
    ("재판기록 열람·복사 규칙", "제7조"),
    ("주민등록법", "제29조의2"),
    ("주민등록법 시행령", "제49조의2"),
    ("주택임대차보호법", "제3조의6"),
    ("주택임대차보호법 시행령", "제5조"),
    ("출입국관리법", "제88조"),
    ("출입국관리법", "제88조의3"),
    ("출입국관리법 시행규칙", "제75조"),
    ("특정범죄신고자 등 보호법", "제9조"),
    ("형사소송법", "제266조의3"),
    ("형사소송법", "제35조"),
    ("형사소송법", "제59조의2"),
    ("형사소송법", "제59조의3"),
    ("국민기초생활 보장법", "제8조의2"),
}

# 보호 조항: 지정한 문단만 발췌해 노출한다.
EXCERPT: dict[tuple[str, str], list[int]] = {
    ("주민등록법", "제29조"): [6, 7, 8],
    ("가족관계의 등록 등에 관한 법률", "제14조"): [8, 9, 10],
    ("가족관계의 등록 등에 관한 법률", "제14조의2"): [3],
    ("주민등록법 시행령", "제47조"): [8],
    # 2026-10-31 리뷰: 아래 조문의 나머지 문단은 제한 해제·제한조치 배제·기록 열람 허용 경로다
    # (제한대상자가 소명해 제한조치를 하지 말 것을 신청, 제3자의 보호결정 취소 신청 등).
    ("주민등록법 시행령", "제47조의2"): [1],
    ("주민등록법 시행규칙", "제13조의2"): [1],
    ("민사소송법", "제163조"): [1, 2, 3],
}

# 문단 시작: ①..⑳ (U+2460..U+2473). 줄 시작 우선, 없으면 공백 선행도 허용.
_PARA_LINE = re.compile(r"(?m)^\s*([\u2460-\u2473])")
_PARA_ANY = re.compile(r"(?:^|(?<=\s))([\u2460-\u2473])")


def _extract_title(text: str) -> str:
    """본문 첫머리의 조문 제목 줄을 뽑는다."""
    m = re.match(r"제\d+조(?:의\d+)?\([^)]*\)", text.strip())
    return m.group(0) if m else ""


def _extract_paragraphs(text: str) -> dict[int, str]:
    """문단 번호(1부터) -> 문단 본문(strip) 매핑을 만든다."""
    marks = list(_PARA_LINE.finditer(text))
    if not marks:
        marks = list(_PARA_ANY.finditer(text))
    elif marks[0].group(1) != "\u2460":
        # 제목 줄과 같은 줄에 붙은 ①(예: 민사소송법 제163조 '…제한) ①다음 각호')을 보탠다.
        first = next((m for m in _PARA_ANY.finditer(text, 0, marks[0].start())
                      if m.group(1) == "\u2460"), None)
        if first is not None:
            marks.insert(0, first)
    out: dict[int, str] = {}
    for i, m in enumerate(marks):
        num = ord(m.group(1)) - 0x2460 + 1
        start = m.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[start:end]
        # 개정 부기("["로 시작하는 줄)에서 자른다.
        cut = body.find("\n[")
        if cut != -1:
            body = body[:cut]
        out[num] = body.strip()
    return out


def _make_excerpt(text: str, paras: list[int], law: str, article: str) -> str:
    """제목 + 지정 문단만 붙인 발췌문을 만든다."""
    title = _extract_title(text)
    if not title:
        raise SystemExit(f"제목 줄을 찾을 수 없음: {law} {article}")
    found = _extract_paragraphs(text)
    missing = [p for p in paras if p not in found]
    if missing:
        raise SystemExit(f"문단 누락: {law} {article} -> {missing}")
    blocks = [found[p] for p in paras]
    return title + " (보호 조항 발췌)\n" + "\n".join(blocks)


def main() -> int:
    if not DATA_PATH.exists():
        raise SystemExit(f"파일 없음: {DATA_PATH}")

    # 1) 전부 읽어 둔다(쓰기 전에 원본을 잃지 않도록).
    with DATA_PATH.open("r", encoding="utf-8") as f:
        raw_lines = [ln for ln in f.read().splitlines() if ln.strip()]

    seen: set[tuple[str, str]] = set()
    records: list[dict] = []
    denied = 0
    excerpted = 0

    for line in raw_lines:
        obj = json.loads(line)
        law = obj.get("law", "")
        article = obj.get("article", "")
        key = (law, article)
        seen.add(key)

        if key in DENY:
            obj["exposable"] = False
            obj["excerpt"] = None
            denied += 1
        elif key in EXCERPT:
            obj["exposable"] = True
            obj["excerpt"] = _make_excerpt(
                obj.get("text", ""), EXCERPT[key], law, article
            )
            excerpted += 1
        else:
            obj["exposable"] = True
            obj["excerpt"] = None
        records.append(obj)

    missing = (DENY | set(EXCERPT)) - seen
    if missing:
        sample = sorted(missing)[:3]
        raise SystemExit(f"목록에 없는 조문: {sample}")

    # 2) 다시 쓴다(한 줄에 한 레코드).
    with DATA_PATH.open("w", encoding="utf-8") as f:
        for obj in records:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    total = len(records)
    print(f"total={total} denied={denied} excerpted={excerpted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
