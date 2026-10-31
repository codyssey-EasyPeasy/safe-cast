"""GPU 추론 API. 모델은 lifespan에서 lazy 로드하며 본문/텍스트는 로그하지 않는다."""
from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import llm
from guard import answer_locate_intent, embed_locate, rule_locate_intent

MAX_BODY = 20 * 1024
MAX_TEXTS = 64
MAX_DOCS = 100
NER_THRESHOLD = 0.4
# locate 판정 임계값(max-sim 점수 = 최대 locate 유사도 - 최대 protect 유사도).
# data/eval/guard_holdout.json(별도 검증 세트, 평가용 redteam 40개와 별개)으로 정했다
# (2026-10-31 실측, bge-m3): 허용 기대 최댓값 -0.0363, 거절 기대 최솟값 0.0201 → 0.02 에서
# 임베딩 단독 10/10 거절·0/10 오거절. 여유가 얇으므로 규칙(guard.py)이 1차 방어다.
THRESH = float(os.environ.get("INTENT_THRESH", "0.02"))

LABELS = ["주소", "직장", "학교", "차량", "생활권"]

# GLiNER에 넘길 자연어 라벨 -> 정식 라벨 매핑.
GLINER_LABELS = {
    "주소": "주소",
    "직장 또는 회사 이름": "직장",
    "학교 이름": "학교",
    "차량 번호": "차량",
    "생활권 장소(동네, 단골 가게, 병원, 어린이집)": "생활권",
}

M: dict[str, Any] = {"embedder": None, "reranker": None, "gliner": None, "seeds": None}


def load_models() -> None:
    """CUDA에서 임베더/리랭커/GLiNER 로드. GLiNER 실패 시 None으로 두고 규칙 폴백."""
    import torch
    from sentence_transformers import CrossEncoder, SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"

    M["embedder"] = SentenceTransformer("BAAI/bge-m3", device=device)
    M["reranker"] = CrossEncoder("BAAI/bge-reranker-v2-m3", device=device)

    try:
        from gliner import GLiNER
        g = GLiNER.from_pretrained("urchade/gliner_multi-v2.1")
        M["gliner"] = g.to(device) if device == "cuda" else g
    except Exception:
        M["gliner"] = None

    _load_seeds()


def _load_seeds() -> None:
    path = Path(__file__).with_name("intent_seeds.json")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    emb = M["embedder"]
    loc = np.asarray(emb.encode(data["locate"], normalize_embeddings=True))
    pro = np.asarray(emb.encode(data["protect"], normalize_embeddings=True))
    M["seeds"] = {
        "locate": loc,
        "protect": pro,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("SUMEUNJIP_NO_LOAD") != "1":
        await asyncio.get_event_loop().run_in_executor(None, load_models)
    yield


app = FastAPI(lifespan=lifespan)


# --- 413 미들웨어: Content-Length 및 실제 본문 길이 모두 검사 ---
@app.middleware("http")
async def body_limit(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > MAX_BODY:
                return JSONResponse({"detail": "payload too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "bad content-length"}, status_code=400)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"detail": "payload too large"}, status_code=413)
    return await call_next(request)


# --- 요청/응답 모델 ---
class EmbedIn(BaseModel):
    texts: list[str] = Field(..., max_length=MAX_TEXTS)


class RerankIn(BaseModel):
    query: str
    docs: list[str] = Field(..., max_length=MAX_DOCS)


class NerIn(BaseModel):
    text: str


class IntentIn(BaseModel):
    q: str


# --- 순수 함수 (모델 없이 테스트 가능) ---
_ADDR_ROAD = re.compile(
    r"[가-힣]+(?:시|도)\s*[가-힣]+(?:구|군|시)\s*[가-힣0-9]+(?:로|길|동|읍|면)\s*\d+(?:-\d+)?(?:번길)?\s*\d*"
)
_ADDR_DONGHO = re.compile(r"\d+동\s*\d+호")
_PLATE = re.compile(r"\d{2,3}[가-힣]\s?\d{4}")
_COMPANY = re.compile(
    r"(?:(?:주식회사|\(주\)|㈜)\s*[가-힣A-Za-z0-9]+)|(?:[가-힣A-Za-z0-9]+(?:주식회사|\(주\)|㈜))|(?:[가-힣]+(?:회사|병원|공장|마트)에서\s*(?:근무|일))"
)
_SCHOOL = re.compile(r"[가-힣]+(?:초등학교|중학교|고등학교|대학교|어린이집|유치원)")


def regex_spans(text: str) -> list[dict]:
    """정규식 폴백 span 목록. score는 0.9 고정."""
    spans: list[dict] = []

    def add(m: re.Match, label: str) -> None:
        spans.append(
            {
                "start": m.start(),
                "end": m.end(),
                "label": label,
                "text": m.group(0),
                "score": 0.9,
            }
        )

    for m in _ADDR_ROAD.finditer(text):
        add(m, "주소")
    for m in _ADDR_DONGHO.finditer(text):
        add(m, "주소")
    for m in _PLATE.finditer(text):
        add(m, "차량")
    for m in _COMPANY.finditer(text):
        add(m, "직장")
    for m in _SCHOOL.finditer(text):
        add(m, "학교")
    return spans


def _overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def merge_spans(spans: list[dict]) -> list[dict]:
    """start 기준 정렬 후, 겹치면 score 높은 쪽 유지(동점이면 긴 쪽)하여 병합."""
    spans = sorted(spans, key=lambda s: (s["start"], -(s["end"] - s["start"])))
    kept: list[dict] = []
    for s in spans:
        drop = False
        for k in kept:
            if _overlaps(k, s):
                longer = (s["end"] - s["start"]) > (k["end"] - k["start"])
                if s["score"] > k["score"] or (s["score"] == k["score"] and longer):
                    kept.remove(k)
                else:
                    drop = True
                    break
        if not drop:
            kept.append(s)
    return sorted(kept, key=lambda s: s["start"])


_LOCATE_PAT = re.compile(
    r"(?:어디\s*(?:살|사는|있)|주소\s*(?:알아|찾|조회)|위치\s*(?:추적|알아|찾)|어디로\s*이사)"
)
# 1인칭 자기 보호 표현(내/제/저의 주소·정보·위치·거주지)만 좁게 차단한다.
# 타인 소재 탐색은 보호 단어가 섞여도 guard 규칙/정규식이 잡는다.
_SELF_PROTECT_PAT = re.compile(r"(?:내|제|저의)\s*(?:새\s*)?(?:주소|정보|위치|거주지)")


def keyword_locate(q: str) -> bool:
    """타인 위치 탐색 표현이면 True. 1인칭 자기 보호 표현이면 정규식은 끄되,
    guard 규칙 엔진이 타인 소재 탐색으로 판정하면 True."""
    return bool(_LOCATE_PAT.search(q) and not _SELF_PROTECT_PAT.search(q)) or rule_locate_intent(q)


def decide_intent(q: str, cos_locate: float, cos_protect: float) -> tuple[bool, float]:
    score = float(cos_locate) - float(cos_protect)
    locate = keyword_locate(q) or score > THRESH
    return locate, round(score, 4)


def _require(name: str):
    if M.get(name) is None:
        raise HTTPException(status_code=503, detail=f"model not loaded: {name}")
    return M[name]


# --- 엔드포인트 ---
@app.get("/health")
def health():
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    return {
        "ok": True,
        "models": {k: M.get(k) is not None for k in ("embedder", "reranker", "gliner")},
        "device": device,
        "llm": llm.llm_alive(),
    }


@app.post("/embed")
def embed(req: EmbedIn):
    import torch

    emb = _require("embedder")
    with torch.inference_mode():
        vecs = emb.encode(req.texts, normalize_embeddings=True)
    return {"vectors": [[float(x) for x in v] for v in vecs]}


@app.post("/rerank")
def rerank(req: RerankIn):
    import torch

    rer = _require("reranker")
    with torch.inference_mode():
        scores = rer.predict(
            [(req.query, d) for d in req.docs], activation_fct=torch.nn.Sigmoid()
        )
    return {"scores": [float(s) for s in scores]}


@app.post("/ner")
def ner(req: NerIn):
    import torch

    spans = regex_spans(req.text)
    gliner = M.get("gliner")
    if gliner is not None:
        with torch.inference_mode():
            ents = gliner.predict_entities(
                req.text, list(GLINER_LABELS.keys()), threshold=NER_THRESHOLD
            )
        for e in ents:
            label = GLINER_LABELS.get(e["label"], e["label"])
            if label not in LABELS:
                continue
            spans.append(
                {
                    "start": int(e["start"]),
                    "end": int(e["end"]),
                    "label": label,
                    "text": e["text"],
                    "score": float(e["score"]),
                }
            )
    return {"spans": merge_spans(spans)}


@app.post("/intent")
def intent(req: IntentIn):
    import torch

    seeds = M.get("seeds")
    if seeds is None or M.get("embedder") is None:
        raise HTTPException(status_code=503, detail="model not loaded: embedder")
    emb = M["embedder"]
    with torch.inference_mode():
        v = np.asarray(emb.encode([req.q], normalize_embeddings=True))[0]
    cos_locate = float(np.max(seeds["locate"] @ v))
    cos_protect = float(np.max(seeds["protect"] @ v))
    locate, score = decide_intent(req.q, cos_locate, cos_protect)
    return {"locate_intent": locate, "score": score, "rule": bool(keyword_locate(req.q))}


# --- 내부 LLM (SPEC §8): vLLM 127.0.0.1:8001 프록시 ---
# LLM 경로 가드(2026-10-31 리뷰 반영).
# - 입력 질문: /intent 와 같은 THRESH(0.02). 좁은 본인 보호 허용 목록(guard.self_protect_allow,
#   점수 0.15 이하)만 임베딩 판정을 푼다. 규칙 적중은 항상 거절.
#   (0.15 로 올렸을 때 '도망간 남편 행방 수소문'(0.0429)이 통과해 되돌렸다.)
# - 생성문(답): 규칙(guard.rule_locate_intent + 생성문 전용 answer_locate_intent) OR 점수 > OUT_THRESH.
#   임베딩으로는 생성문을 가를 수 없었다(2026-10-31 실측: 정상 우편물 전송 답 0.1302 > 리뷰의 주입 답
#   '법원 기록을 통해 피신청인의 현 거주지를 확인하려면…' 0.1133). 0.08 로 낮추면 정상 답이 거절된다.
#   그래서 생성문은 규칙이 1차 방어이고(주입 답은 이제 규칙이 잡는다) 임베딩은 0.15 를 유지한다.
# - rewrite 출력은 아래 rewrite_flagged(입력 대비 상승분)로 따로 본다.
OUT_THRESH = float(os.environ.get("LLM_OUT_THRESH", "0.15"))
REWRITE_THRESH = float(os.environ.get("LLM_REWRITE_THRESH", "0.15"))
_ITEM_ID = r"^[A-Z]\d{1,3}$"


class FlagIn(BaseModel):
    id: str = Field(..., max_length=64)
    label: str = Field("", max_length=200)


class IntakeIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=3000)
    flags: list[FlagIn] | None = Field(None, max_length=80)


class ArticleIn(BaseModel):
    law: str = Field("", max_length=100)
    article: str = Field("", max_length=100)
    text: str = Field("", max_length=2000)


class ItemIn(BaseModel):
    id: str = Field(..., pattern=_ITEM_ID)
    path: str | None = Field(None, max_length=500)
    exposure: str | None = Field(None, max_length=500)
    measure: str | None = Field(None, max_length=500)
    timing: str | None = Field(None, max_length=500)
    where: str | None = Field(None, max_length=500)
    documents: str | None = Field(None, max_length=500)
    limits: str | None = Field(None, max_length=500)
    notify_warning: str | None = Field(None, max_length=500)
    status: str | None = Field(None, max_length=20)
    pending_effective: str | None = Field(None, max_length=20)
    plain: str | None = Field(None, max_length=1000)
    note: str | None = Field(None, max_length=500)
    articles: list[ArticleIn] = Field(default_factory=list, max_length=6)


class AnswerIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    items: list[ItemIn] = Field(default_factory=list, max_length=8)


class SpanIn(BaseModel):
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    label: str = Field(..., max_length=20)


class RewriteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=6000)
    spans: list[SpanIn] = Field(default_factory=list, max_length=100)


def locate_score(text: str) -> tuple[bool, float | None]:
    """(규칙 판정, 임베딩 의도 점수). 임베더가 없으면 점수 None."""
    rule = keyword_locate(text)
    seeds, emb = M.get("seeds"), M.get("embedder")
    if seeds is None or emb is None:
        return rule, None
    import torch

    with torch.inference_mode():
        v = np.asarray(emb.encode([text], normalize_embeddings=True))[0]
    return rule, float(np.max(seeds["locate"] @ v)) - float(np.max(seeds["protect"] @ v))


def locate_flagged(text: str, thresh: float | None = None) -> bool:
    """규칙(guard.py·정규식) OR 임베딩 의도 점수 > 임계값."""
    rule, score = locate_score(text)
    return rule or (score is not None and score > (THRESH if thresh is None else thresh))


# rewrite 출력 가드: 입력은 사용자 본인 서류라 주소 단어가 많아 임베딩 점수가 원래 높다
# (2026-10-31 실측: 합성 소장 한 문장 0.0896 > 0.02). 그래서 '모델이 새로 만든' 탐색 내용만 잡는다:
# 입력에 없던 규칙 적중이 생기거나, 임베딩 점수가 임계값을 넘으면서 입력보다 REWRITE_DELTA 이상 오르면 차단.
REWRITE_DELTA = float(os.environ.get("LLM_REWRITE_DELTA", "0.05"))


def rewrite_flagged(original: str, suggested: str) -> bool:
    r_in, s_in = locate_score(original)
    r_out, s_out = locate_score(suggested)
    if r_out and not r_in:
        return True
    if s_out is None or s_in is None:
        return False
    return s_out > REWRITE_THRESH and s_out > s_in + REWRITE_DELTA


def _llm_down(exc: Exception | None = None) -> JSONResponse:
    if isinstance(exc, llm.LlmTimeout):
        # 시간 초과는 503 과 구분한다(앱은 504 를 재시도하지 않는다).
        return JSONResponse({"detail": "llm timeout", "available": True}, status_code=504)
    return JSONResponse({"detail": "llm unavailable", "available": False}, status_code=503)


def input_flagged(text: str) -> bool:
    """LLM 입력 질문 가드: 규칙 OR 임베딩(THRESH, 좁은 본인 보호 허용 목록 예외)."""
    rule, score = locate_score(text)
    return rule or embed_locate(text, score, THRESH)


def _refusal() -> dict:
    return {"refused": True, "answer": llm.SAFE_REFUSAL, "citations": [], "grounded": False,
            "ai_generated": True}


@app.post("/llm/intake")
def llm_intake(req: IntakeIn):
    flags = [f.model_dump() for f in req.flags] if req.flags else llm.load_local_flags()
    ids = [f["id"] for f in flags]
    try:
        out = llm.chat_json(llm.intake_messages(req.text, flags), llm.intake_schema(ids),
                            max_tokens=512, temperature=0.0)
    except llm.LlmDown as exc:
        return _llm_down(exc)
    res = llm.clean_intake(out, ids)
    # 확인 질문도 생성문이다: 탐색 표현이 섞이면 버린다.
    res["unclear"] = [q for q in res["unclear"] if not keyword_locate(q)]
    res["ai_generated"] = True
    return res


@app.post("/llm/answer")
def llm_answer(req: AnswerIn):
    if input_flagged(req.question):
        return _refusal()
    items = [i.model_dump() for i in req.items]
    ids = [i["id"] for i in items]
    if not ids:
        return llm.not_found()
    # 통지 질문은 생성하지 않는다(notify_warning 원문만, 없으면 '확인되지 않음').
    if llm.is_notify_question(req.question):
        return llm.notify_answer(items)
    # 효과 시점 질문은 생성하지 않고 신청 시점·처리 칸을 그대로 보여 준다(제정일·통지 시점 오답 방지).
    if llm.is_effect_question(req.question):
        return llm.effect_answer(items)
    # 질문이 이름을 댄 제도가 근거 행에 없으면 답하지 않는다(다른 제도로 대신 설명 금지).
    if llm.topic_mismatch(req.question, items):
        return llm.not_found()
    try:
        out = llm.chat_json(llm.answer_messages(req.question, items), llm.answer_schema(ids),
                            max_tokens=700, temperature=0.1)
    except llm.LlmDown as exc:
        return _llm_down(exc)
    res = llm.clean_answer(out, ids)
    if res["grounded"] and (answer_locate_intent(res["answer"])
                            or locate_flagged(res["answer"], OUT_THRESH)):
        return _refusal()
    if res["grounded"] and llm.unsupported_denial(res["answer"], items, res["citations"]):
        return {"answer": llm.NOTIFY_UNKNOWN, "citations": [], "grounded": False, "ai_generated": True}
    return res


@app.post("/llm/rewrite")
def llm_rewrite(req: RewriteIn):
    n = len(req.text)
    given = [s.model_dump() for s in req.spans if s.start < s.end <= n]
    # 앱이 넘긴 span 에 서버 정규식 span 을 더한다(누락 대비).
    spans = merge_spans([{**s, "score": 1.0} for s in given] + regex_spans(req.text)
                        + llm.routine_spans(req.text))
    try:
        out = llm.chat_json(llm.rewrite_messages(req.text, spans), llm.rewrite_schema(),
                            max_tokens=min(4096, 2 * n + 256), temperature=0.0)
    except llm.LlmDown as exc:
        return _llm_down(exc)
    spans = merge_spans(spans + [{**c, "score": 0.5} for c in llm.clue_spans(req.text, out.get("clues"))])
    res = llm.finalize_rewrite(req.text, spans, str(out.get("text") or ""))
    # 생성문에 남은 정규식 단서는 규칙으로 한 번 더 가린다.
    left = regex_spans(res["suggested"]) + llm.routine_spans(res["suggested"])
    if left:
        res["suggested"] = llm.mask_spans(res["suggested"], merge_spans(left))
        res["forced"] += len(left)
    if rewrite_flagged(req.text, res["suggested"]):
        return _refusal() | {"suggested": None}
    res["ai_generated"] = True
    return res
