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
    assert len(engine.items) == 84  # J28 추가(조사 §3-12)
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


def _ids(rows):
    return {r["id"] for r in rows}


def test_stalking_measures_apply_to_family_and_dating():
    e = Engine(load_knowledge())
    r = e.diagnose("family", ["protection_order", "moved_recently"])
    assert {"J13", "J14", "J15"} <= _ids(r["checklist"])
    assert not ({"J13", "J14", "J15"} & _ids(r["not_applicable"]))
    r = e.diagnose("dating", ["protection_order", "criminal_case"])
    assert {"J13", "J14", "J15"} <= _ids(r["checklist"])


def test_a1_note_when_same_registration_address():
    e = Engine(load_knowledge())
    a1 = [c for c in e.diagnose("family", ["same_registration_address"])["checklist"] if c["id"] == "A1"][0]
    # 내부 id(A6)는 사용자 문구에서 제도 이름으로 바뀐다(app/humanize.py).
    assert "A6" not in a1["note"] and "분리" in a1["note"]
    a1 = [c for c in e.diagnose("family", [])["checklist"] if c["id"] == "A1"][0]
    assert "note" not in a1
    assert "conditional_notes" not in a1


def test_abuser_plaintiff_residual_and_no_exposure_in_residual():
    e = Engine(load_knowledge())
    r = e.diagnose("family", ["lawsuit", "abuser_is_plaintiff"])
    assert "J28" in _ids(r["residual"])
    for it in r["residual"]:
        assert "exposure" not in it and "limits" not in it
        assert "1366" in it["support"]


def test_a17_is_residual_not_promise():
    e = Engine(load_knowledge())
    r = e.diagnose("family", ["welfare_or_pension"])
    assert "A17" in _ids(r["residual"]) and "A17" not in _ids(r["checklist"])


def test_plain_has_no_dual_use_phrases():
    k = load_knowledge()
    bad = ("누구나 명부를 열람", "누구나 열람할 수", "인적사항을 적지 않으면", "신청할 수 있는 사람을 소유자",
           "2주 뒤 동의로 간주", "준용 규정이 확인되지 않았")
    for it in k["items"]:
        for field in ("plain", "measure", "limits", "exposure"):
            for b in bad:
                assert b not in (it.get(field) or ""), (it["id"], field, b)


def test_user_fields_have_no_internal_ids():
    import re

    pat = re.compile(r"(?<![A-Za-z0-9])[AJSLDP]\d{1,3}(?![0-9])")
    e = Engine(load_knowledge())
    for it in e.items:
        for k in ("path", "exposure", "measure", "timing", "where", "documents", "limits",
                  "notify_warning", "plain"):
            assert not pat.search(it.get(k) or ""), (it["id"], k)


def test_a6_inherits_a1_notify_warning():
    e = Engine(load_knowledge())
    by = {it["id"]: it for it in e.items}
    assert "서면으로 통지" in by["A6"]["notify_warning"]
    assert by["A6"]["notify_inherited_from"] == "A1"


def test_exposable_articles_have_no_restriction_lifting_routes():
    """검색에 노출되는 조문(발췌 우선)에 제한 해제·배제·제3자 취소 경로 문단이 없어야 한다."""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "data" / "laws" / "articles.jsonl"
    bad = ("제한조치를 하지 말 것을", "결정을 취소할 수 있다", "법 제29조제9항에 따른 신청")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if not r.get("exposable"):
            continue
        text = r.get("excerpt") or r.get("text") or ""
        for b in bad:
            assert b not in text, (r["law"], r["article"], b)
