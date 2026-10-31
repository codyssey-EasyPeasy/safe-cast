"""레인 통합 시험: 실제 지식표·평가 시나리오, 정적 서빙, GPU 본문 상한(20KB) 준수."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app import retrieval
from app.engine import Engine, load_knowledge
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent
GPU_MAX_BODY = 20 * 1024  # gpu/server.py MAX_BODY


def test_real_knowledge_scenarios():
    engine = Engine(load_knowledge(ROOT / "data" / "knowledge.json"))
    assert len(engine.items) == 83
    known = set(engine.flag_ids())
    for s in json.loads((ROOT / "data" / "eval" / "scenarios.json").read_text("utf-8")):
        assert set(s["flags"]) <= known, s["id"]
        r = engine.diagnose(s["victim_type"], s["flags"])
        ids = {c["id"] for c in r["checklist"]}
        assert set(s["must_include"]) <= ids, s["id"]
        assert not (set(s["must_not_include"]) & ids), s["id"]


def test_static_assets_served():
    client = TestClient(create_app(str(ROOT / "data" / "knowledge.json")))
    for path in ("/", "/static/app.css", "/static/app.js", "/static/detect.js"):
        assert client.get(path).status_code == 200, path


def test_budget_batches_fit_gpu_limit():
    texts = ["가" * 1200] * 50
    batches = retrieval._budget_batches(texts, 32)
    assert sorted(i for b in batches for i in b) == list(range(50))
    for b in batches:
        assert len(retrieval._json_bytes({"texts": [texts[i] for i in b]})) < GPU_MAX_BODY


def test_real_corpus_embeds_under_gpu_limit():
    """실제 조문 174건 임베딩·재순위가 GPU 본문 상한(413)에 걸리지 않는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        if len(request.content) > GPU_MAX_BODY:
            return httpx.Response(413, json={"detail": "payload too large"})
        body = json.loads(request.content)
        if request.url.path == "/embed":
            return httpx.Response(200, json={"vectors": [[1.0, 0.0] for _ in body["texts"]]})
        if request.url.path == "/rerank":
            return httpx.Response(200, json={"scores": [float(len(d)) for d in body["docs"]]})
        return httpx.Response(404)

    async def run():
        r = retrieval.Retriever()
        assert len(r.articles) > 100
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            assert await r.ensure_embedded(c, "http://gpu")
            res, fallback = await r.search("소송 서류 주소 비공개", c, "http://gpu")
        return res, fallback

    res, fallback = asyncio.run(run())
    assert fallback is False
    assert len(res) == 5
