"""내부 LLM 기능 프록시(SPEC §8). 운영 API → GPU `/llm/*`.

GPU 쪽 계약(이 모듈이 보내고 기대하는 형태):
- `GET  /health`        → `{..., "llm": true|false}` (또는 `{"llm": {"ok": true}}`). 없으면 꺼진 것으로 본다.
- `POST /llm/intake`    `{text}` → `{victim_type|null, flags:[id], confidence:float, unclear:[str]}`
- `POST /llm/answer`    `{question, item_ids, context:[{id, text}]}` → `{answer:str, citations:[id], grounded:bool}`
- `POST /llm/rewrite`   `{text, spans:[{start,end,label}]}` → `{rewritten:str}`

원칙
- 본문·프롬프트·생성문은 저장·로깅하지 않는다(이 모듈은 로거를 쓰지 않는다).
- 입력 가드: intake·answer 는 기존 `retrieval.intent()` 로 먼저 검사(fail-closed).
- 출력 가드: intake 의 질문 문구·answer 의 답은 `intent()` 로 한 번 더 검사해 걸리면 거절 문구로 바꾼다.
- answer 컨텍스트는 지식표 행 + `exposable` 조문(발췌 우선)만. 잔여 위험 행은 exposure·limits 를 뺀다.
- GPU 실패·형식 오류는 503 → 화면은 체크박스 방식으로 그대로 동작.
- 어떤 제도를 권할지는 규칙 엔진이 정한다. intake 결과는 체크박스 '미리 선택'에만 쓰인다.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

from .retrieval import intent

# GPU 서버 본문 상한(20KB) 안쪽으로 컨텍스트를 자른다.
CONTEXT_BUDGET_BYTES = 14 * 1024
_ITEM_FIELDS = ("measure", "plain", "path", "timing", "where", "documents", "notify_warning")
_ITEM_FIELDS_IN_FORCE_EXTRA = ("limits",)
_ARTICLE_CHARS = 700
_FIELD_CHARS = 400

ITEM_ID_RE = r"^[A-Z]{1,3}[0-9]{1,3}$"
_ITEM_ID_PAT = re.compile(ITEM_ID_RE)

NOT_FOUND_ANSWER = (
    "제공된 자료에서는 확인되지 않습니다. "
    "1366(24시간)이나 법률구조공단 132에 문의해 보세요."
)

_FEATURE_TTL = 30.0


class LLMUnavailable(Exception):
    """GPU LLM 이 응답하지 않거나 형식이 맞지 않는다."""


def llm_disabled_by_env() -> bool:
    return os.environ.get("LLM_FEATURES", "1").strip() in ("0", "false", "off", "no")


class FeatureProbe:
    """GPU `/health` 의 llm 표시를 짧게 캐시한다."""

    def __init__(self) -> None:
        self._value: bool = False
        self._at: float = 0.0

    def mark_down(self) -> None:
        self._value = False
        self._at = time.monotonic()

    async def llm(self, client: httpx.AsyncClient, gpu_url: str) -> bool:
        if llm_disabled_by_env():
            return False
        now = time.monotonic()
        if self._at and now - self._at < _FEATURE_TTL:
            return self._value
        value = False
        try:
            r = await client.get(f"{gpu_url}/health", timeout=3.0)
            r.raise_for_status()
            data = r.json()
            flag = data.get("llm") if isinstance(data, dict) else None
            if isinstance(flag, dict):
                value = flag.get("ok") is True
            else:
                value = flag is True
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            value = False
        self._value = value
        self._at = now
        return value


async def _post(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    try:
        r = await client.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise LLMUnavailable() from e
    if not isinstance(data, dict):
        raise LLMUnavailable()
    return data


def _clip(s: Any, n: int) -> str:
    if not isinstance(s, str):
        return ""
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------------------
# intake
# ---------------------------------------------------------------------------
async def intake(
    text: str,
    *,
    client: httpx.AsyncClient,
    gpu_url: str,
    known_flags: set[str],
    victim_types: tuple[str, ...],
) -> dict[str, Any]:
    data = await _post(client, f"{gpu_url}/llm/intake", {"text": text}, timeout=60.0)

    vt = data.get("victim_type")
    victim_type = vt if isinstance(vt, str) and vt in victim_types else None

    raw_flags = data.get("flags")
    flags: list[str] = []
    if isinstance(raw_flags, list):
        for f in raw_flags:
            if isinstance(f, str) and f in known_flags and f not in flags:
                flags.append(f)

    conf = data.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        confidence = 0.0

    unclear: list[str] = []
    raw_unclear = data.get("unclear")
    if isinstance(raw_unclear, list):
        for q in raw_unclear[:3]:
            q = _clip(q, 200)
            if not q:
                continue
            # 출력 가드: 되묻는 질문에 탐색 표현이 섞이면 버린다.
            if await intent(q, client, gpu_url):
                continue
            unclear.append(q)

    return {
        "victim_type": victim_type,
        "flags": flags,
        "confidence": confidence,
        "unclear": unclear,
        "ai_generated": True,
    }


# ---------------------------------------------------------------------------
# answer
# ---------------------------------------------------------------------------
def _article_base(article: str) -> str:
    """'제29조 제6항' → '제29조', '제10조의2 제3항' → '제10조의2'."""
    m = re.match(r"^(제\d+조(?:의\d+)?)", (article or "").strip())
    return m.group(1) if m else ""


def build_context(
    item_ids: list[str],
    items_by_id: dict[str, dict[str, Any]],
    articles: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """지식표 행 + 노출 가능 조문(발췌 우선)만으로 컨텍스트를 만든다.

    `articles` 는 Retriever 가 이미 exposable 만 남기고 excerpt 로 치환한 목록이다.
    """
    art_index: dict[tuple[str, str], dict[str, Any]] = {}
    for a in articles:
        art_index[(a.get("law", ""), a.get("article", ""))] = a

    out: list[dict[str, str]] = []
    used = 0
    seen_articles: set[tuple[str, str]] = set()
    for iid in item_ids:
        item = items_by_id.get(iid)
        if not item:
            continue
        fields = list(_ITEM_FIELDS)
        if item.get("status") != "none":
            fields += list(_ITEM_FIELDS_IN_FORCE_EXTRA)
        lines = []
        for k in fields:
            v = _clip(item.get(k), _FIELD_CHARS)
            if v:
                lines.append(f"{k}: {v}")
        basis = []
        for b in item.get("basis") or []:
            law = b.get("law", "")
            art = b.get("article", "")
            basis.append(f"{law} {art}".strip())
        if basis:
            lines.append("basis: " + "; ".join(x for x in basis if x))
        entry = {"id": iid, "text": "\n".join(lines)}
        size = len(entry["text"].encode("utf-8")) + 32
        if used + size > CONTEXT_BUDGET_BYTES:
            break
        out.append(entry)
        used += size

        for b in item.get("basis") or []:
            key = (b.get("law", ""), _article_base(b.get("article", "")))
            if not key[1] or key in seen_articles:
                continue
            a = art_index.get(key)
            if not a:
                continue
            seen_articles.add(key)
            text = _clip(a.get("text"), _ARTICLE_CHARS)
            if not text:
                continue
            aentry = {"id": f"{iid}:{key[0]} {key[1]}", "text": text}
            asize = len(text.encode("utf-8")) + 64
            if used + asize > CONTEXT_BUDGET_BYTES:
                continue
            out.append(aentry)
            used += asize
    return out


async def answer(
    question: str,
    item_ids: list[str],
    *,
    client: httpx.AsyncClient,
    gpu_url: str,
    items_by_id: dict[str, dict[str, Any]],
    articles: list[dict[str, Any]],
) -> dict[str, Any]:
    ids = [i for i in item_ids if i in items_by_id]
    context = build_context(ids, items_by_id, articles)
    if not context:
        return {"answer": NOT_FOUND_ANSWER, "citations": [], "grounded": False, "ai_generated": True}

    data = await _post(
        client,
        f"{gpu_url}/llm/answer",
        {"question": question, "item_ids": ids, "context": context},
        timeout=90.0,
    )
    text = _clip(data.get("answer"), 2000)
    allowed = {c["id"] for c in context}
    citations: list[str] = []
    raw = data.get("citations")
    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, str) and c in allowed and c not in citations:
                citations.append(c)
    grounded = data.get("grounded") is True and bool(citations) and bool(text)

    if not text or not grounded:
        return {"answer": NOT_FOUND_ANSWER, "citations": [], "grounded": False, "ai_generated": True}

    # 출력 가드: 생성문에 탐색 의도 표현이 섞이면 내보내지 않는다.
    if await intent(text, client, gpu_url):
        return {"refused": True}

    return {"answer": text, "citations": citations, "grounded": True, "ai_generated": True}


# ---------------------------------------------------------------------------
# rewrite
# ---------------------------------------------------------------------------
async def rewrite(
    text: str,
    spans: list[dict[str, Any]],
    *,
    client: httpx.AsyncClient,
    gpu_url: str,
) -> dict[str, Any]:
    clean_spans = []
    for s in spans:
        start, end = s["start"], s["end"]
        if 0 <= start < end <= len(text):
            clean_spans.append({"start": start, "end": end, "label": s.get("label") or ""})
    data = await _post(
        client,
        f"{gpu_url}/llm/rewrite",
        {"text": text, "spans": clean_spans},
        timeout=90.0,
    )
    out = data.get("rewritten")
    if not isinstance(out, str) or not out.strip():
        raise LLMUnavailable()
    out = out[: len(text) * 2 + 200]

    # 규칙 필터: 원래 단서 문자열이 그대로 남았는지 표시한다(새로 가리지는 않는다).
    remaining = []
    for s in clean_spans:
        frag = text[s["start"] : s["end"]].strip()
        if len(frag) >= 2 and frag in out and frag not in remaining:
            remaining.append(frag)
    return {"rewritten": out, "remaining": remaining, "ai_generated": True}
