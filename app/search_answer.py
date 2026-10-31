"""검색 조문 종합 답변(/api/search_answer): 검색으로 고른 조문만 근거로 질문에 답한다.

두 방식(같은 입력·출력 가드 뒤에서 돈다):
- "rows"(기본): 검색 조문을 근거(basis)로 인용하는 지식표 행을 역색인으로 찾아 기존 `llm.answer` 를
  그대로 부른다. 조문 컨텍스트는 검색된 조문 목록만 넘긴다(`articles=results`) → 행 + 검색 조문만 근거.
- "articles": 검색 조문 자체를 GPU `/llm/answer` items 로 보낸다(gpu/ 변경 없음). 조문마다 합성 id
  (`Z1`…`Z5`, ITEM_ID_RE 통과, 지식표 id 글자와 겹치지 않음)를 붙이고 measure=법령·조문(제목),
  plain=조문 본문(개정 부기 제거·자름). 돌아온 인용 id 는 조문으로 되짚는다.

어느 쪽이든 사용자에게 보이는 인용(`citations`)은 검색된 조문 안에서만 `{law, article, url}` 로 낸다.
외부 LLM 호출 없음(GPU 내부 서버만). 질문·생성문·조문 본문은 로깅하지 않는다(로거를 쓰지 않는다).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import humanize as _hz
from . import llm as _llm

STRATEGIES = ("rows", "articles")
TOP_K = 5
MAX_ROWS = 5

# 합성 id 글자. 지식표 id 첫 글자(A·D·J·L·P·S)와 겹치면 humanize 가 제도 이름으로 바꿔 버린다.
SYNTH_PREFIX = "Z"
_PLAIN_CHARS = 800      # GPU ItemIn.plain 상한 1000자 안쪽(한글 3바이트 × 5개 ≈ 12KB)
_EXTRACT_CHARS = 400
_EXTRACT_K = 2
_TITLE_RE = re.compile(r"^\s*제\d+조(?:의\d+)?\s*\(([^)]{1,60})\)")
EXTRACT_LEAD = "관련 조문은 다음과 같습니다."
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KST = ZoneInfo("Asia/Seoul")


def _today() -> str:
    return datetime.now(_KST).date().isoformat()


def pending_since(a: dict[str, Any], today: str | None = None) -> str:
    """시행일이 오늘(KST)보다 뒤인 조문이면 그 시행일, 아니면 ''(형식이 다르면 판단하지 않는다)."""
    eff = str(a.get("effective") or "").strip()
    if not _DATE_RE.match(eff):
        return ""
    return eff if eff > (today or _today()) else ""


def pending_note(cited: list[dict[str, Any]], today: str | None = None) -> str:
    """인용 조문 중 시행 전 조문이 있으면 답 끝에 붙일 고정 안내(생성문이 현행처럼 말해도 바로잡는다)."""
    today = today or _today()
    parts = []
    for a in cited:
        eff = pending_since(a, today)
        if eff:
            parts.append(f"{article_label(a)}({eff} 시행)")
    if not parts:
        return ""
    return "※ 시행 전 조문: " + ", ".join(parts) + " — 아직 시행되지 않아 지금은 이 조문으로 신청할 수 없습니다."


def article_key(law: str, article: str) -> tuple[str, str]:
    return (law or "", _llm._article_base(article or ""))


def build_row_index(items: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    """(법령, 조 단위 조문) → 그 조문을 근거로 드는 지식표 행 id(지식표 순서). 시작할 때 한 번."""
    idx: dict[tuple[str, str], list[str]] = {}
    for it in items:
        iid = it.get("id")
        if not iid:
            continue
        for b in it.get("basis") or []:
            key = article_key(b.get("law", ""), b.get("article", ""))
            if not key[1]:
                continue
            ids = idx.setdefault(key, [])
            if iid not in ids:
                ids.append(iid)
    return idx


def rows_for_results(
    results: list[dict[str, Any]],
    row_index: dict[tuple[str, str], list[str]],
    limit: int = MAX_ROWS,
) -> list[str]:
    """검색 순위가 높은 조문을 인용하는 행부터 최대 limit 개."""
    out: list[str] = []
    for a in results:
        for iid in row_index.get(article_key(a.get("law", ""), a.get("article", "")), []):
            if iid not in out:
                out.append(iid)
                if len(out) >= limit:
                    return out
    return out


def user_citation(a: dict[str, Any]) -> dict[str, str]:
    return {"law": str(a.get("law") or ""), "article": str(a.get("article") or ""),
            "url": str(a.get("url") or "")}


def citations_from_rows(
    row_ids: list[str],
    items_by_id: dict[str, dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """인용된 행 → 그 행의 근거 중 검색된 조문만(검색 순위 순)."""
    wanted: set[tuple[str, str]] = set()
    for iid in row_ids:
        for b in (items_by_id.get(iid) or {}).get("basis") or []:
            wanted.add(article_key(b.get("law", ""), b.get("article", "")))
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a in results:
        key = article_key(a.get("law", ""), a.get("article", ""))
        if key in wanted and key not in seen:
            seen.add(key)
            out.append(user_citation(a))
    return out


def _not_found(**extra: Any) -> dict[str, Any]:
    return {"answer": _llm.NOT_FOUND_ANSWER, "citations": [], "grounded": False,
            "ai_generated": True, "mode": "not_found", **extra}


# ---------------------------------------------------------------------------
# 방식 A: 지식표 행
# ---------------------------------------------------------------------------
async def answer_rows(
    question: str,
    results: list[dict[str, Any]],
    *,
    client: httpx.AsyncClient,
    gpu_url: str,
    items_by_id: dict[str, dict[str, Any]],
    row_index: dict[tuple[str, str], list[str]],
) -> dict[str, Any]:
    ids = rows_for_results(results, row_index)
    if not ids:
        return _not_found(rows=[])
    out = await _llm.answer(question, ids, client=client, gpu_url=gpu_url,
                            items_by_id=items_by_id, articles=results)
    if out.get("refused"):
        return {"refused": True, "mode": "refused"}
    row_cites = list(out.get("citations") or [])
    cites = citations_from_rows(row_cites, items_by_id, results) if out.get("grounded") else []
    if out.get("grounded") and not cites:
        # 행은 검색 조문으로 골랐으니 생기지 않아야 하지만, 인용이 검색 집합 밖이면 답하지 않는다.
        return _not_found(rows=ids)
    return {**out, "citations": cites, "rows": ids}


# ---------------------------------------------------------------------------
# 방식 B: 조문 자체
# ---------------------------------------------------------------------------
def article_label(a: dict[str, Any]) -> str:
    return f"{a.get('law', '')} {a.get('article', '')}".strip()


def build_article_items(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """검색 조문 → GPU ItemIn(id·measure·plain). 반환: (items, 합성 id → 조문)."""
    items: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    used = 0
    for n, a in enumerate(results[:TOP_K], start=1):
        text = _llm._clip(_llm.strip_amend_notes(a.get("text") or ""), _PLAIN_CHARS)
        if not text:
            continue
        sid = f"{SYNTH_PREFIX}{n}"
        m = _TITLE_RE.match(a.get("text") or "")
        label = article_label(a)
        measure = f"{label}({m.group(1)})" if m else label
        eff = pending_since(a)
        if eff:
            measure = f"[시행 전: {eff} 시행, 지금은 적용되지 않음] {measure}"
        entry = {"id": sid, "measure": _llm._clip(measure, 400), "plain": text, "articles": []}
        size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
        if used + size > _llm.CONTEXT_BUDGET_BYTES:
            break
        items.append(entry)
        by_id[sid] = a
        used += size
    return items, by_id


def _article_extract(cited: list[dict[str, Any]]) -> str:
    lines = [EXTRACT_LEAD]
    for a in cited[:_EXTRACT_K]:
        eff = pending_since(a)
        lines.append(f"■ {article_label(a)}" + (f" ({eff} 시행 예정, 시행 전)" if eff else ""))
        lines.append(_llm._clip(_llm.strip_amend_notes(a.get("text") or ""), _EXTRACT_CHARS))
    return "\n".join(lines)


async def answer_articles(
    question: str,
    results: list[dict[str, Any]],
    *,
    client: httpx.AsyncClient,
    gpu_url: str,
) -> dict[str, Any]:
    items, by_id = build_article_items(results)
    if not items:
        return _not_found()
    # 조문에는 통지 경고·신청 시점 칸이 없다. llm.answer 와 같은 원칙으로 생성하지 않는다.
    if _llm.is_notify_question(question):
        return {"answer": _llm.NOTIFY_UNKNOWN_ANSWER, "citations": [], "grounded": False,
                "ai_generated": False, "source": "notify", "mode": "not_found"}
    if _llm.is_effect_question(question):
        return _not_found()

    data = await _llm._post(client, f"{gpu_url}/llm/answer",
                            {"question": question, "items": items}, timeout=90.0)
    if data.get("refused") is True:
        return {"refused": True, "mode": "refused"}
    mode = data.get("mode") if data.get("mode") in _llm.ANSWER_MODES else "generated"
    text = _llm._clip(data.get("answer"), 2000)
    sids: list[str] = []
    raw = data.get("citations")
    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, str) and c in by_id and c not in sids:
                sids.append(c)
    grounded = data.get("grounded") is True and bool(sids) and bool(text)
    if not grounded:
        return _not_found()
    cited = [by_id[s] for s in sids]

    if mode == "extractive":
        # GPU 발췌는 행 칸 이름(막는 제도·쉬운 설명)으로 나온다 → 앱에서 조문 원문 발췌로 다시 만든다.
        return {"answer": _article_extract(cited), "citations": [user_citation(a) for a in cited],
                "grounded": True, "ai_generated": False, "mode": "extractive"}

    if await _llm.generated_text_blocked(text, client, gpu_url):
        return {"refused": True, "mode": "refused"}
    if _llm.DENY_NOTIFY_RE.search(text) and not any(
        _llm.DENY_NOTIFY_RE.search(a.get("text") or "") for a in cited
    ):
        return {"answer": _llm.NOTIFY_UNKNOWN_ANSWER, "citations": [], "grounded": False,
                "ai_generated": True, "mode": "not_found"}

    names = {sid: article_label(a) for sid, a in by_id.items()}
    text = _hz.humanize_text(_hz.strip_bracket_ids(text), names)
    return {"answer": text, "citations": [user_citation(a) for a in cited], "grounded": True,
            "ai_generated": True, "mode": mode}


async def synthesize(
    strategy: str,
    question: str,
    results: list[dict[str, Any]],
    *,
    client: httpx.AsyncClient,
    gpu_url: str,
    items_by_id: dict[str, dict[str, Any]],
    row_index: dict[tuple[str, str], list[str]],
) -> dict[str, Any]:
    if not results:
        return _not_found()
    if strategy == "articles":
        out = await answer_articles(question, results, client=client, gpu_url=gpu_url)
    else:
        out = await answer_rows(question, results, client=client, gpu_url=gpu_url,
                                items_by_id=items_by_id, row_index=row_index)
    return _mark_pending(out, results)


def _mark_pending(out: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    """근거 답의 인용에 시행 전 조문이 있으면 고정 안내를 덧붙인다(가드 통과 뒤의 앱 고정문)."""
    if out.get("refused") or not out.get("grounded") or not isinstance(out.get("answer"), str):
        return out
    keys = {(c.get("law"), c.get("article")) for c in out.get("citations") or []}
    cited = [a for a in results if (a.get("law"), a.get("article")) in keys]
    note = pending_note(cited)
    if not note:
        return out
    return {**out, "answer": f"{out['answer']}\n\n{note}"}
