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
from . import retrieval as _rt

STRATEGIES = ("rows", "articles")
TOP_K = 5
# 생성에 넘기는 지식표 행 상한. 5개를 넘기면 생성문이 다른 행(다른 제도)을 끌어와 섞었다
# (2026-11-01 품질 실측: 가정폭력 임시보호명령 질문에 스토킹 잠정조치 행으로 답함) → 2개.
MAX_ROWS = 2
# 행 후보는 검색 조문 5개에 이어진 행 전부(실측 최대 10개)에서 고른다.
_CANDIDATE_ROWS = 16

# 관련도 기준(round-1 개발 세트 40문, GPU /rerank 실측으로 정함 — docs/SPEC.md §11.11).
# 행 재순위 점수: (질문, 행의 path+measure+plain) 쌍을 기존 GPU /rerank 로 잰 값(0~1).
ROW_RERANK_MIN = 0.03
# 행이 근거로 드는 검색 조문의 재순위 점수(검색 결과 score). 임베딩 검색일 때만 쓴다(BM25 점수는 척도가 다르다).
ARTICLE_RERANK_MIN = 0.05
# 두 번째 행은 첫 행 재순위 점수의 이 비율 이상일 때만(관련도가 비슷할 때만 두 제도를 함께 보낸다).
SECOND_ROW_RATIO = 0.5
# 재순위를 못 쓸 때(GPU /rerank 실패)의 대체 기준: 질문 글자 bigram 중 행 글에도 있는 비율.
LEXICAL_MIN = 0.15
_ROW_TEXT_CHARS = 600   # retrieval._RERANK_CHARS 와 같은 길이

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


# ---------------------------------------------------------------------------
# 행 고르기: 제도 이름·피해 유형이 다른 행 제외 → 관련도 → 최대 2개
# ---------------------------------------------------------------------------
# 질문이 이름을 댄 제도가 행(path·measure·plain)에 없으면 그 행을 뺀다. gpu/llm.py TOPIC_TERMS 와
# 같은 목록(앱은 gpu/ 를 import 하지 않는다 — 시험이 두 목록이 같은지 확인한다). GPU 는 보낸 행 전체에
# 하나라도 있으면 통과시키므로, 섞인 행 중 엉뚱한 행을 앱에서 행 단위로 거른다.
TOPIC_TERMS: list[tuple[str, str]] = [
    ("보호명령", r"보호\s*명령"),
    ("임시조치", r"임시\s*조치"),
    ("응급조치", r"응급\s*조치"),
    ("잠정조치", r"잠정\s*조치"),
    ("접근금지", r"접근\s*금지"),
    ("신변보호", r"신변\s*(?:보호|안전)"),
    ("열람 제한", r"열람\s*(?:·\s*교부\s*)?제한"),
    ("교부 제한", r"교부\s*제한"),
    ("주민등록번호 변경", r"(?:주민\s*등록\s*)?번호\s*변경"),
    ("송달장소", r"송달\s*(?:장소|영수인)"),
    ("보호시설", r"보호\s*시설|쉼터"),
    ("임차권등기", r"임차권\s*등기"),
    ("전자장치", r"전자\s*장치|전자\s*발찌"),
    ("위치추적", r"위치\s*추적"),
    ("배상명령", r"배상\s*명령"),
    ("친권 제한", r"친권"),
    ("면접교섭", r"면접\s*교섭"),
    ("양육비", r"양육비"),
    ("위자료", r"위자료"),
    ("이혼", r"이혼"),
    ("국선변호", r"국선\s*(?:변호|보조인)"),
    ("공시제한", r"공시\s*제한"),
    ("전학", r"전학|취학"),
    ("가명", r"가명"),
]
_TOPIC_FIELDS = ("measure", "path", "plain")
_TOPIC_PATS = [(n, re.compile(p)) for n, p in TOPIC_TERMS]

# 피해 유형. 질문이 유형을 대고, 행의 제목(path·measure)이 다른 유형만 대면 그 행을 뺀다
# (plain 은 '가정폭력 피해자도 스토킹 요건을 충족하면' 처럼 다른 유형을 함께 적어 보지 않는다).
DOMAIN_TERMS: list[tuple[str, re.Pattern[str]]] = [
    ("가정폭력", re.compile(r"가정\s*폭력")),
    ("스토킹", re.compile(r"스토킹")),
    ("성폭력", re.compile(r"성\s*폭력")),
    ("아동학대", re.compile(r"아동\s*학대")),
]
_DOMAIN_FIELDS = ("path", "measure")


def _field_text(item: dict[str, Any], fields: tuple[str, ...]) -> str:
    return " ".join(str(item.get(k) or "") for k in fields)


def topic_ok(question: str, item: dict[str, Any]) -> bool:
    """질문이 이름을 댄 제도가 모두 이 행(path·measure·plain)에 있으면 True."""
    subject = _field_text(item, _TOPIC_FIELDS)
    return all(pat.search(subject) for _n, pat in _TOPIC_PATS if pat.search(question or ""))


def domain_ok(question: str, item: dict[str, Any]) -> bool:
    """질문의 피해 유형과 행 제목의 피해 유형이 겹치거나, 어느 한쪽이 유형을 대지 않으면 True."""
    qd = {n for n, pat in DOMAIN_TERMS if pat.search(question or "")}
    if not qd:
        return True
    head = _field_text(item, _DOMAIN_FIELDS)
    rd = {n for n, pat in DOMAIN_TERMS if pat.search(head)}
    return not rd or bool(qd & rd)


# 질문이 따옴표로 댄 제도 이름('…'·"…"·「…」). 그 이름(공백 무시, 끝의 '제도' 제외)이 행 글에 없으면
# 그 행을 뺀다 — 없는 제도를 이름으로 물을 때 비슷한 실제 제도 행으로 '있다'고 답하지 않게.
_QUOTED_RE = re.compile(r"[‘'\"“「『]([^‘’'\"“”「」『』]{2,40})[’'\"”」』]")


def quoted_names(question: str) -> list[str]:
    out = []
    for m in _QUOTED_RE.finditer(question or ""):
        name = re.sub(r"\s+", "", m.group(1))
        name = re.sub(r"제도$", "", name)
        if len(name) >= 2:
            out.append(name)
    return out


def quoted_ok(question: str, item: dict[str, Any]) -> bool:
    names = quoted_names(question)
    if not names:
        return True
    subject = re.sub(r"\s+", "", _field_text(item, _TOPIC_FIELDS))
    return all(n in subject for n in names)


def row_text(item: dict[str, Any]) -> str:
    return _llm._clip(_field_text(item, ("path", "measure", "plain")), _ROW_TEXT_CHARS)


_LEX_STOP = frozenset("있나 나요 수있 을수 할수 있는 하나 하면 려면 어떻 떻게 게해 해야 야하 에게 제가 받을 "
                      "는데 인데 에서 으로 하는 되나 가나 까지 니다 습니".split())


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[^가-힣A-Za-z0-9]", "", (text or "").lower())
    return {t[i:i + 2] for i in range(len(t) - 1)} - _LEX_STOP


def lexical_overlap(question: str, text: str) -> float:
    """질문 bigram(어미류 제외) 중 행 글에도 있는 비율(0~1)."""
    qb = _bigrams(question)
    return len(qb & _bigrams(text)) / len(qb) if qb else 0.0


def row_candidates(
    results: list[dict[str, Any]],
    row_index: dict[tuple[str, str], list[str]],
    limit: int = _CANDIDATE_ROWS,
) -> list[tuple[str, int, float]]:
    """검색 조문에 이어진 행 → (id, 가장 높은 검색 순위, 그 행이 드는 검색 조문의 최고 재순위 점수)."""
    best: dict[str, list[Any]] = {}
    order: list[str] = []
    for rank, a in enumerate(results):
        sc = float(a.get("score") or 0.0)
        for iid in row_index.get(article_key(a.get("law", ""), a.get("article", "")), []):
            if iid not in best:
                if len(order) >= limit:
                    continue
                best[iid] = [rank, sc]
                order.append(iid)
            else:
                best[iid][1] = max(best[iid][1], sc)
    return [(iid, best[iid][0], best[iid][1]) for iid in order]


async def rerank_rows(
    question: str, ids: list[str], items_by_id: dict[str, dict[str, Any]],
    *, client: httpx.AsyncClient, gpu_url: str,
) -> dict[str, float] | None:
    """행 글을 기존 GPU /rerank 로 잰다(gpu/ 변경 없음). 실패하면 None → 글자 겹침으로 대체."""
    if not ids:
        return {}
    try:
        data = await _rt._gpu_post(client, f"{gpu_url}/rerank",
                                   {"query": question, "docs": [row_text(items_by_id[i]) for i in ids]})
        scores = data.get("scores") or []
        if len(scores) != len(ids):
            return None
        return {i: float(s) for i, s in zip(ids, scores)}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return None


def select_rows(
    question: str,
    cands: list[tuple[str, int, float]],
    items_by_id: dict[str, dict[str, Any]],
    row_scores: dict[str, float] | None,
    *,
    fallback: bool = False,
    limit: int = MAX_ROWS,
) -> tuple[list[str], dict[str, float]]:
    """생성에 넘길 행(최대 limit 개)과 행별 관련도(재순위 점수, 없으면 글자 겹침)를 고른다.

    1) 질문이 이름을 댄 제도(TOPIC_TERMS·따옴표 이름)가 없는 행·피해 유형이 다른 행은 뺀다.
    2) 관련 있는 행만 남긴다: 재순위 ≥ ROW_RERANK_MIN, 또는 (임베딩 검색일 때) 그 행이 드는 검색 조문의
       재순위 ≥ ARTICLE_RERANK_MIN. 재순위를 못 쓰면 글자 겹침 ≥ LEXICAL_MIN.
    3) 순서: 재순위 ≥ ROW_RERANK_MIN 인 행을 점수 순으로 먼저, 나머지는 검색 순위 순.
    4) 두 번째 행은 첫 행 점수의 SECOND_ROW_RATIO 이상일 때만(첫 행이 재순위로 뽑혔을 때).
    """
    rel: dict[str, float] = {}
    kept: list[tuple[str, int, float, bool]] = []
    for iid, rank, art in cands:
        item = items_by_id.get(iid)
        if (not item or not topic_ok(question, item) or not domain_ok(question, item)
                or not quoted_ok(question, item)):
            continue
        if row_scores is not None:
            s = float(row_scores.get(iid, 0.0))
            strong = s >= ROW_RERANK_MIN
            ok = strong or (not fallback and art >= ARTICLE_RERANK_MIN)
        else:
            s = lexical_overlap(question, row_text(item))
            strong = False
            ok = s >= LEXICAL_MIN
        rel[iid] = s
        if ok:
            kept.append((iid, rank, s, strong))
    kept.sort(key=lambda k: (not k[3], -k[2] if k[3] else 0.0, k[1]))
    out: list[str] = []
    for iid, _rank, s, strong in kept:
        if out:
            first = kept[0]
            if first[3] and (not strong or s < SECOND_ROW_RATIO * first[2]):
                break
        out.append(iid)
        if len(out) >= limit:
            break
    return out, rel


def cited_rows_relevant(
    cited: list[str], cands: list[tuple[str, int, float]], rel: dict[str, float],
    *, reranked: bool, fallback: bool,
) -> bool:
    """인용된 행 중 하나라도 관련도 기준을 넘는지(발췌·생성 결과의 무관 답 거르기)."""
    art = {iid: sc for iid, _r, sc in cands}
    for iid in cited:
        s = rel.get(iid, 0.0)
        if reranked and (s >= ROW_RERANK_MIN or (not fallback and art.get(iid, 0.0) >= ARTICLE_RERANK_MIN)):
            return True
        if not reranked and s >= LEXICAL_MIN:
            return True
    return False


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
    fallback: bool = False,
) -> dict[str, Any]:
    cands = [c for c in row_candidates(results, row_index) if c[0] in items_by_id]
    if not cands:
        return _not_found(rows=[])
    scores = await rerank_rows(question, [c[0] for c in cands], items_by_id,
                               client=client, gpu_url=gpu_url)
    ids, rel = select_rows(question, cands, items_by_id, scores, fallback=fallback)
    if not ids:
        # 제도·피해 유형이 맞는 행이 없거나 모두 질문과 무관(관련도 기준 미달) → GPU 를 부르지 않는다.
        return _not_found(rows=[])
    out = await _llm.answer(question, ids, client=client, gpu_url=gpu_url,
                            items_by_id=items_by_id, articles=results)
    if out.get("refused"):
        return {"refused": True, "mode": "refused"}
    row_cites = list(out.get("citations") or [])
    if out.get("grounded") and not cited_rows_relevant(row_cites, cands, rel,
                                                       reranked=scores is not None, fallback=fallback):
        # 발췌·생성이 관련도 기준 미달 행만 인용했으면 무관한 답으로 보고 내보내지 않는다.
        return _not_found(rows=ids)
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
    fallback: bool = False,
) -> dict[str, Any]:
    if not results:
        return _not_found()
    if strategy == "articles":
        out = await answer_articles(question, results, client=client, gpu_url=gpu_url)
    else:
        out = await answer_rows(question, results, client=client, gpu_url=gpu_url,
                                items_by_id=items_by_id, row_index=row_index, fallback=fallback)
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
