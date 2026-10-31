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

MAX_BODY = 20 * 1024
MAX_TEXTS = 64
MAX_DOCS = 100
NER_THRESHOLD = 0.4
# 시드 문장으로 튜닝한 locate 판정 임계값.
THRESH = 0.15

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
        "locate": loc.mean(axis=0),
        "protect": pro.mean(axis=0),
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
_PROTECT_PAT = re.compile(r"(?:보호|차단|숨기|열람\s*제한|비공개|내\s*정보|신청|막)")


def keyword_locate(q: str) -> bool:
    """사람 위치 탐색 표현이 있으면서 보호 표현이 없을 때만 True."""
    if _PROTECT_PAT.search(q):
        return False
    return bool(_LOCATE_PAT.search(q))


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
    cos_locate = float(np.dot(v, seeds["locate"]))
    cos_protect = float(np.dot(v, seeds["protect"]))
    locate, score = decide_intent(req.q, cos_locate, cos_protect)
    return {"locate_intent": locate, "score": score}
