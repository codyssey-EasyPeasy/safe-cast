"""내부 LLM 기능 프록시(SPEC §8). 운영 API → GPU `/llm/*`.

GPU 쪽 계약(이 모듈이 보내고 기대하는 형태):
- `GET  /health`        → `{..., "llm": true|false}` (또는 `{"llm": {"ok": true}}`). 없으면 꺼진 것으로 본다.
- `POST /llm/intake`    `{text}` → `{victim_type|null, flags:[id], confidence:float, unclear:[str]}`
- `POST /llm/answer`    `{question, items:[{id, measure?, plain?, path?, ..., articles:[{law, article, text}]}]}`
                         → `{answer:str, citations:[id], grounded:bool}` 또는 `{refused:true, ...}`
                         (item id 는 `^[A-Z][0-9]{1,3}$`, 항목 최대 8개, 필드 500자·plain 1000자, 조문 최대 6개·2000자)
- `POST /llm/rewrite`   `{text, spans:[{start,end,label}]}` → `{suggested:str, method, forced}` 또는 `{refused:true}`
                         (구버전 `{rewritten}` 도 받는다)

원칙
- 본문·프롬프트·생성문은 저장·로깅하지 않는다(이 모듈은 로거를 쓰지 않는다).
- 입력 가드: intake·answer 는 `retrieval.intent(..., thresh=LLM_GUARD_THRESH)` 로 먼저 검사.
- 출력 가드: intake 의 질문 문구·answer 의 답은 같은 방식으로 한 번 더 검사해 걸리면 거절한다.
  GPU `/llm/answer` 도 자체로 규칙 OR 임베딩>0.15 를 질문·답에 건다(이중).
- LLM 경로 임베딩 임계값은 GPU 와 같은 0.15(`LLM_GUARD_THRESH`). 규칙은 그대로 항상 먼저 적용된다.
  2026-10-31 실측: 서버 기본 0.02 로는 tools/eval_llm.py 의 답할 수 있는 피해자 질문 5개 중 3개
  (우편물 0.1258·휴대폰 명의 0.1121·머물 곳 0.0938)가 앱 입력 단계에서 거절됐다.
- answer 컨텍스트는 지식표 행 + `exposable` 조문(발췌 우선)만. 잔여 위험 행은 exposure·limits 를 뺀다.
- GPU 실패·형식 오류는 503 → 화면은 체크박스 방식으로 그대로 동작.
- 어떤 제도를 권할지는 규칙 엔진이 정한다. intake 결과는 체크박스 '미리 선택'에만 쓰인다.
"""

from __future__ import annotations

import json
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
_ARTICLE_CHARS = 600
_FIELD_CHARS = 400
_ARTICLES_PER_ITEM = 2

ITEM_ID_RE = r"^[A-Z][0-9]{1,3}$"
_ITEM_ID_PAT = re.compile(ITEM_ID_RE)

NOT_FOUND_ANSWER = (
    "제공된 자료에서는 확인되지 않습니다. "
    "1366(24시간)이나 법률구조공단 132에 문의해 보세요."
)

_FEATURE_TTL = 30.0


def llm_guard_thresh() -> float:
    try:
        return float(os.environ.get("LLM_GUARD_THRESH", "0.15"))
    except ValueError:
        return 0.15


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
        if r.status_code == 503:
            # GPU 는 생성 JSON 이 깨져도 503 을 준다(2026-10-31 실측: 앱 경로 약 40회 중 1회,
            # 같은 요청 재시도는 성공). 응답이 온 503 만 한 번 다시 묻는다(시간 초과는 재시도 안 함).
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
            if await intent(q, client, gpu_url, thresh=llm_guard_thresh()):
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


def build_items(
    item_ids: list[str],
    items_by_id: dict[str, dict[str, Any]],
    articles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """GPU `/llm/answer` 의 items: 지식표 행 + 노출 가능 조문(발췌 우선)만.

    `articles` 는 Retriever 가 이미 exposable 만 남기고 excerpt 로 치환한 목록이다.
    exposure 는 보내지 않고, 잔여 위험 행(status == "none")은 limits 도 뺀다.
    """
    art_index: dict[tuple[str, str], dict[str, Any]] = {}
    for a in articles:
        art_index[(a.get("law", ""), a.get("article", ""))] = a

    out: list[dict[str, Any]] = []
    used = 0
    for iid in item_ids:
        item = items_by_id.get(iid)
        if not item:
            continue
        fields = list(_ITEM_FIELDS)
        if item.get("status") != "none":
            fields += list(_ITEM_FIELDS_IN_FORCE_EXTRA)
        entry: dict[str, Any] = {"id": iid}
        for k in fields:
            v = _clip(item.get(k), _FIELD_CHARS)
            if v:
                entry[k] = v
        arts: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for b in item.get("basis") or []:
            key = (b.get("law", ""), _article_base(b.get("article", "")))
            if not key[1] or key in seen:
                continue
            a = art_index.get(key)
            if not a:
                continue
            seen.add(key)
            text = _clip(a.get("text"), _ARTICLE_CHARS)
            if text:
                arts.append({"law": key[0][:100], "article": key[1], "text": text})
            if len(arts) >= _ARTICLES_PER_ITEM:
                break
        entry["articles"] = arts
        size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
        if used + size > CONTEXT_BUDGET_BYTES:
            break
        out.append(entry)
        used += size
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
    items = build_items(ids, items_by_id, articles)
    if not items:
        return {"answer": NOT_FOUND_ANSWER, "citations": [], "grounded": False, "ai_generated": True}

    data = await _post(
        client,
        f"{gpu_url}/llm/answer",
        {"question": question, "items": items},
        timeout=90.0,
    )
    if data.get("refused") is True:
        return {"refused": True}
    text = _clip(data.get("answer"), 2000)
    allowed = {it["id"] for it in items}
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
    if await intent(text, client, gpu_url, thresh=llm_guard_thresh()):
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
    if data.get("refused") is True:
        return {"refused": True}
    out = data.get("suggested")
    if out is None:
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
