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
- 입력 가드: intake·answer 는 /api/search 와 같은 `retrieval.intent(...)`(규칙 OR 임베딩>0.02,
  좁은 본인 보호 허용 목록만 예외)로 먼저 검사한다. 엔드포인트마다 판정이 달라지지 않게 한다.
  (2026-10-31 리뷰: 0.15 로 올렸더니 '도망간 남편 행방 수소문'(0.0429)이 /api/search 에선 거절,
  /api/answer·/api/intake 에선 통과했다. 그래서 입력 임계값을 0.02 로 되돌렸다.)
- 출력 가드: intake 의 질문 문구·answer 의 답은 규칙(생성문 전용 `answer_locate_intent` 포함) OR
  임베딩 > `LLM_OUT_THRESH`(기본 0.15)로 한 번 더 검사해 걸리면 거절한다. GPU `/llm/answer` 도 같은
  출력 검사를 한다(이중). 임베딩만으로는 정상 답과 주입 답을 가르지 못했다(아래 out_thresh 참고).
- 효과 시점 질문('언제부터 효과가 있나요?')은 생성하지 않고 신청 시점·처리 칸을 그대로 보여 준다.
- 통지 질문('신청하면 상대에게 알려지나요?')은 생성하지 않고 지식표 통지 경고만으로 답한다.
  통지 경고가 없으면 '확인되지 않음 · 1366/132 문의'. 근거 없는 '알려지지 않아요' 류 답은 버린다.
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

from . import humanize as _hz
from .guard import answer_locate_intent
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


def out_thresh() -> float:
    """생성문(답·확인 질문) 출력 가드 임베딩 임계값.

    0.15: 2026-10-31 실측에서 정상 답(우편물 전송 0.1302)이 리뷰의 주입 답(0.1133)보다 높아
    임베딩으로는 가를 수 없었다. 생성문은 규칙(guard.answer_locate_intent)이 1차 방어다.
    """
    try:
        return float(os.environ.get("LLM_OUT_THRESH", "0.15"))
    except ValueError:
        return 0.15


class LLMUnavailable(Exception):
    """GPU LLM 이 응답하지 않거나 형식이 맞지 않는다.

    `down=True` 는 GPU 에 닿지 않은 경우(연결 실패)만. 이때만 기능 표시를 전역으로 끈다.
    """

    def __init__(self, *, down: bool = False, timeout: bool = False) -> None:
        super().__init__()
        self.down = down
        self.timeout = timeout


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
            # 같은 요청 재시도는 성공). 응답이 온 503 만 한 번 다시 묻는다.
            # 생성 시간 초과는 GPU 가 504 로 따로 알려 주며 재시도하지 않는다.
            r = await client.post(url, json=payload, timeout=timeout)
        if r.status_code == 504:
            raise LLMUnavailable(timeout=True)
        r.raise_for_status()
        data = r.json()
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise LLMUnavailable(down=True) from e
    except httpx.TimeoutException as e:
        raise LLMUnavailable(timeout=True) from e
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
            if await intent(q, client, gpu_url, out_thresh=out_thresh()):
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
# 조문 개정 부기('<신설 2009. 4. 1.>', '[전문개정 2016. 12. 30.]')는 효과 시점이 아니다.
# 2026-10-31 리뷰: '언제부터 효과가 있나요?' 에 '2009년 4월 1일부터' 로 답한 원인. 컨텍스트에서 뺀다.
_AMEND_RE = re.compile(r"<(?:신설|개정|삭제|전문개정|본조신설|제목개정)[^>]*>|\[(?:전문개정|본조신설|제목개정|시행일)[^\]]*\]")


def strip_amend_notes(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", _AMEND_RE.sub("", text or "")).strip()


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
            text = _clip(strip_amend_notes(a.get("text") or ""), _ARTICLE_CHARS)
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


# 통지 질문: 신청하면 상대(가해자)에게 알려지는지 묻는 질문.
NOTIFY_Q_RE = re.compile(
    r"(?:상대|가해자|그\s*사람|배우자|남편|아내|제한대상자|피신청인|행위자)[^.?!]{0,15}"
    r"(?:알려지|알려져|알게\s*(?:되|돼)|통지|통보|고지|연락이?\s*가|알림)"
    r"|(?:통지|통보|고지)\s*(?:가|를|는|되|돼|하|받|도)"
    r"|(?:신청|제한)[^.?!]{0,12}(?:알려지|알려져|알게\s*(?:되|돼))"
)
# '알려지지 않게 하려면'처럼 보호 방법을 묻는 질문은 통지 질문이 아니다.
_NOTIFY_PROTECT_RE = re.compile(r"(?:알려지|알게\s*되)\s*지\s*(?:않게|않도록|못하게)|모르게")
# 통지를 부정하는 생성문: 근거 행에 명시가 없으면 내보내지 않는다.
DENY_NOTIFY_RE = re.compile(
    r"(?:알려지|알려져|통지되|통지가\s*되|통보되|고지되|알게\s*되|알게\s*돼)\s*지\s*(?:않|않아|않습)"
    r"|(?:통지|통보|고지)(?:하지|는|가)?\s*(?:않|없)"
    r"|모르게\s*(?:신청|할\s*수|돼|됩)"
    r"|알\s*수\s*없(?:어요|습니다)"
)
NOTIFY_UNKNOWN_ANSWER = (
    "제공된 자료에서는 신청 사실이 상대에게 알려지는지 확인되지 않습니다. "
    "신청 전에 1366(24시간)이나 법률구조공단 132에 꼭 문의해 보세요."
)


def is_notify_question(q: str) -> bool:
    q = q or ""
    return bool(NOTIFY_Q_RE.search(q)) and not _NOTIFY_PROTECT_RE.search(q)


def _row_supports_denial(item: dict[str, Any]) -> bool:
    for k in ("notify_warning", "limits", "where", "plain", "measure"):
        if DENY_NOTIFY_RE.search(item.get(k) or ""):
            return True
    return False


def notify_answer(ids: list[str], items_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """통지 질문: 생성하지 않고 지식표 통지 경고만 그대로 보여 준다."""
    texts: list[str] = []
    cites: list[str] = []
    for iid in ids:
        w = (items_by_id.get(iid) or {}).get("notify_warning")
        if w and w not in texts:
            texts.append(w)
            cites.append(iid)
    if not texts:
        return {"answer": NOTIFY_UNKNOWN_ANSWER, "citations": [], "grounded": False,
                "ai_generated": False, "source": "notify"}
    return {"answer": " ".join(texts), "citations": cites, "grounded": True,
            "ai_generated": False, "source": "notify"}


EFFECT_Q_RE = re.compile(
    r"언제부터|효과가\s*언제|효력이?\s*(?:언제|생기|발생)|언제\s*(?:효과|효력)|"
    r"바로\s*(?:효과|적용)|얼마나\s*걸리|며칠\s*(?:걸리|만에)|처리\s*(?:기간|시간)"
)


def is_effect_question(q: str) -> bool:
    return bool(EFFECT_Q_RE.search(q or ""))


def effect_answer(ids: list[str], items_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """효과 시점 질문: 생성하지 않고 신청 시점·처리 칸 원문만(제정일·통지 시점 오답 방지)."""
    parts: list[str] = []
    cites: list[str] = []
    for iid in ids:
        it = items_by_id.get(iid) or {}
        bits = []
        if it.get("timing"):
            bits.append(f"신청 시점: {it['timing']}")
        if it.get("where"):
            bits.append(f"신청처·처리: {it['where']}")
        if bits:
            parts.append(" / ".join(bits))
            cites.append(iid)
    if not parts:
        return {"answer": NOT_FOUND_ANSWER, "citations": [], "grounded": False, "ai_generated": True}
    return {"answer": "안내문에 적힌 시점입니다. " + " · ".join(parts)
            + ". 처리 기간이 적혀 있지 않으면 신청처에 꼭 확인하세요.",
            "citations": cites, "grounded": True, "ai_generated": False, "source": "effect"}


def _with_sources(out: dict[str, Any], items_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    names = _hz.name_map(list(items_by_id.values()))
    out["sources"] = [
        {"id": c, "label": _hz.source_label(items_by_id[c], names)}
        for c in out.get("citations") or [] if c in items_by_id
    ]
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
    if ids and is_notify_question(question):
        return _with_sources(notify_answer(ids, items_by_id), items_by_id)
    if ids and is_effect_question(question):
        return _with_sources(effect_answer(ids, items_by_id), items_by_id)
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
    if answer_locate_intent(text) or await intent(text, client, gpu_url, out_thresh=out_thresh()):
        return {"refused": True}

    # 근거 없는 '상대에게 알려지지 않아요' 는 위험한 오답이다: 인용 행에 명시가 없으면 버린다.
    if DENY_NOTIFY_RE.search(text) and not any(
        _row_supports_denial(items_by_id[c]) for c in citations
    ):
        return {"answer": NOTIFY_UNKNOWN_ANSWER, "citations": [], "grounded": False,
                "ai_generated": True}

    # 내부 id 를 사용자에게 보이지 않게: '[A1]' 인용 표시는 지우고 남은 id 는 제도 이름으로.
    names = _hz.name_map(list(items_by_id.values()))
    text = _hz.humanize_text(_hz.strip_bracket_ids(text), names)
    return _with_sources(
        {"answer": text, "citations": citations, "grounded": True, "ai_generated": True},
        items_by_id,
    )


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
