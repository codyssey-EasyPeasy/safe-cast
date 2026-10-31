"""법령 조문 검색 + 질의 의도 분류.

- bm25: 순수 파이썬 간이 BM25 (GPU 불가 시 폴백, 폴백 질의).
- ensure_embedded / search: GPU `/embed` + `/rerank`. 실패 시 bm25 폴백.
- intent: GPU `/intent`. 실패 시 로컬 정규식 폴백.

사람·주소를 찾는 방법을 출력하는 텍스트는 어디에도 두지 않는다.
"""

from __future__ import annotations

import asyncio
import glob
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

# 사람·주소를 찾거나 조회하는 '방법'을 묻는 질의를 잡는 로컬 규칙.
# 출력에는 절대 이 내용을 쓰지 않는다(거절 판단에만 사용).
_LOCATE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"찾는\s*법",
        r"찾는\s*방법",
        r"위치\s*추적",
        r"주소\s*알아",
        r"주소\s*조회",
        r"열람\s*방법",
        r"어디\s*사는지",
        r"뒤\s*밟",
        r"미행\s*방법",
        r"연락처\s*알아",
        r"차량번호로\s*.*(조회|주인)",
        r"전입세대\s*열람",
        r"주민등록\s*초본\s*발급\s*방법",
        r"신상\s*털",
    )
)


def local_locate_intent(q: str) -> bool:
    """로컬 정규식 기반 의도 분류. True 이면 사람/주소 탐색 의도."""
    if not q:
        return False
    text = q.lower()
    return any(p.search(text) for p in _LOCATE_PATTERNS)


# GPU 서버는 요청 본문을 20KB 로 제한한다(gpu/server.py MAX_BODY). 여유를 두고 자른다.
_GPU_BODY_BUDGET = 16 * 1024
_EMBED_CHARS = 1200  # 임베딩용 조문 앞부분(조 제목·핵심 문장이 앞에 온다)
_RERANK_CHARS = 600


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _budget_batches(texts: list[str], max_items: int) -> list[list[int]]:
    """본문 크기 상한 안에 들어가도록 인덱스를 묶는다."""
    batches: list[list[int]] = []
    cur: list[int] = []
    size = 64
    for i, t in enumerate(texts):
        n = len(t.encode("utf-8")) + 8
        if cur and (size + n > _GPU_BODY_BUDGET or len(cur) >= max_items):
            batches.append(cur)
            cur, size = [], 64
        cur.append(i)
        size += n
    if cur:
        batches.append(cur)
    return batches


async def _gpu_post(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any], timeout: float = 15.0
) -> dict[str, Any]:
    r = await client.post(
        url,
        content=_json_bytes(payload),
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def _tokens(text: str) -> list[str]:
    """한국어 텍스트 토큰화: 공백 단어 + 문자 bigram."""
    text = (text or "").lower()
    toks: list[str] = []
    for w in text.split():
        w = re.sub(r"[^\w가-힣]+", "", w)
        if len(w) >= 2:
            toks.append(w)
    cleaned = re.sub(r"\s+", "", text)
    cleaned = re.sub(r"[^\w가-힣]+", "", cleaned)
    for i in range(len(cleaned) - 1):
        toks.append(cleaned[i : i + 2])
    return toks


def _load_articles() -> list[dict[str, Any]]:
    paths: list[Path] = []
    env = os.environ.get("ARTICLES_PATH")
    if env:
        p = Path(env)
        if p.exists():
            paths.append(p)
    else:
        default = REPO_ROOT / "data" / "laws" / "articles.jsonl"
        if default.exists():
            paths.append(default)
        else:
            for f in sorted(glob.glob(str(REPO_ROOT / "data" / "laws" / "*.jsonl"))):
                paths.append(Path(f))

    articles: list[dict[str, Any]] = []
    for p in paths:
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict) or "text" not in obj:
                        continue
                    articles.append(
                        {
                            "law": obj.get("law", ""),
                            "article": obj.get("article", ""),
                            "effective": obj.get("effective", ""),
                            "text": obj.get("text", ""),
                            "url": obj.get("url", ""),
                        }
                    )
        except OSError:
            continue
    return articles


class Retriever:
    """조문 코퍼스 검색기. 임베딩은 ensure_embedded 로 1회 캐시."""

    def __init__(self) -> None:
        self.articles: list[dict[str, Any]] = _load_articles()
        self._tokens_per_doc: list[list[str]] = [
            _tokens(a["text"]) for a in self.articles
        ]
        self._doc_lens: list[int] = [len(t) for t in self._tokens_per_doc]
        self._avgdl: float = (
            sum(self._doc_lens) / len(self._doc_lens) if self._doc_lens else 0.0
        )
        self._df: dict[str, int] = {}
        for toks in self._tokens_per_doc:
            for t in set(toks):
                self._df[t] = self._df.get(t, 0) + 1
        self._n_docs: int = len(self.articles)

        self.vectors: list[list[float]] = []
        self.embedded: bool = False
        self._embed_lock = asyncio.Lock()
        self._last_attempt: float = 0.0

    # --- BM25 -----------------------------------------------------------
    def bm25(self, query: str, top_k: int) -> list[dict[str, Any]]:
        k1 = 1.5
        b = 0.75
        q_toks = _tokens(query)
        if not q_toks or not self.articles:
            return []

        scores = [0.0] * self._n_docs
        for t in q_toks:
            df = self._df.get(t, 0)
            if df == 0:
                continue
            idf = math.log(1 + (self._n_docs - df + 0.5) / (df + 0.5))
            for i, toks in enumerate(self._tokens_per_doc):
                tf = toks.count(t)
                if tf == 0:
                    continue
                dl = self._doc_lens[i] or 1
                denom = tf + k1 * (1 - b + b * dl / (self._avgdl or 1))
                scores[i] += idf * (tf * (k1 + 1)) / denom

        ranked = sorted(range(self._n_docs), key=lambda i: scores[i], reverse=True)
        out: list[dict[str, Any]] = []
        for i in ranked[:top_k]:
            if scores[i] <= 0:
                continue
            a = self.articles[i]
            out.append({**a, "score": float(scores[i])})
        return out

    # --- 임베딩 ---------------------------------------------------------
    async def ensure_embedded(self, client: httpx.AsyncClient, gpu_url: str) -> bool:
        """코퍼스를 한 번만 임베딩한다. 실패 시 최소 60초 재시도 제한."""
        if self.embedding_ready():
            return True
        if not self.articles:
            return False

        async with self._embed_lock:
            if self.embedded:
                return True
            now = time.monotonic()
            if self._last_attempt and now - self._last_attempt < 60:
                return False
            self._last_attempt = now

            vectors: list[list[float]] = []
            try:
                all_texts = [a["text"][:_EMBED_CHARS] for a in self.articles]
                for idx in _budget_batches(all_texts, 32):
                    texts = [all_texts[i] for i in idx]
                    data = await _gpu_post(
                        client, f"{gpu_url}/embed", {"texts": texts}, timeout=30.0
                    )
                    vs = data.get("vectors") or []
                    if len(vs) != len(texts):
                        raise ValueError("embed size mismatch")
                    for v in vs:
                        vectors.append([float(x) for x in v])
                self.vectors = vectors
                self.embedded = True
                return True
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self.vectors = []
                self.embedded = False
                return False

    def embedding_ready(self) -> bool:
        return self.embedded and bool(self.vectors)

    @staticmethod
    def _dot(a: list[float], b: list[float]) -> float:
        n = min(len(a), len(b))
        s = 0.0
        for i in range(n):
            s += a[i] * b[i]
        return s

    # --- 검색 -----------------------------------------------------------
    async def search(
        self,
        q: str,
        client: httpx.AsyncClient,
        gpu_url: str,
        top_k: int = 5,
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            ok = await self.ensure_embedded(client, gpu_url)
            if not ok:
                return (self.bm25(q, top_k), True)

            data = await _gpu_post(client, f"{gpu_url}/embed", {"texts": [q]})
            vs = data.get("vectors") or []
            if not vs:
                raise ValueError("no query vector")
            qv = [float(x) for x in vs[0]]

            sims = [(self._dot(qv, v), i) for i, v in enumerate(self.vectors)]
            sims.sort(key=lambda x: x[0], reverse=True)
            top = sims[:30]
            docs = [self.articles[i]["text"][:_RERANK_CHARS] for _, i in top]

            # 재순위 점수는 (질의, 문서) 쌍마다 독립이므로 나눠 보내도 같다.
            scores: list[float] = [0.0] * len(docs)
            for idx in _budget_batches(docs, 100):
                rdata = await _gpu_post(
                    client,
                    f"{gpu_url}/rerank",
                    {"query": q, "docs": [docs[i] for i in idx]},
                )
                part = rdata.get("scores") or []
                if len(part) != len(idx):
                    raise ValueError("rerank size mismatch")
                for i, sc in zip(idx, part):
                    scores[i] = float(sc)

            ranked = sorted(
                [(float(s), i) for s, (_, i) in zip(scores, top)],
                key=lambda x: x[0],
                reverse=True,
            )
            out: list[dict[str, Any]] = []
            for s, i in ranked[:top_k]:
                a = self.articles[i]
                out.append({**a, "score": float(s)})
            return (out, False)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return (self.bm25(q, top_k), True)


async def intent(q: str, client: httpx.AsyncClient, gpu_url: str) -> bool:
    """GPU 의도 분류. 실패 시 로컬 규칙으로 폴백."""
    try:
        r = await client.post(f"{gpu_url}/intent", json={"q": q}, timeout=10.0)
        r.raise_for_status()
        data = r.json()
        return bool(data.get("locate_intent", False))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return local_locate_intent(q)
