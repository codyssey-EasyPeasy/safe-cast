from __future__ import annotations

import hashlib
import json
import logging
import math
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

FIXTURE = Path(__file__).parent / "fixtures" / "knowledge_small.json"

ARTICLES = [
    {
        "law": "민사소송법",
        "article": "제163조",
        "effective": "2025-07-12",
        "text": "소송 서면의 송달은 당사자에게 표시된 주소로 이루어진다.",
        "url": "https://www.law.go.kr/a",
    },
    {
        "law": "주민등록법",
        "article": "제29조",
        "effective": "2022-01-01",
        "text": "주민등록표 등본의 교부는 본인 또는 세대원에게 한다.",
        "url": "https://www.law.go.kr/b",
    },
    {
        "law": "가정폭력범죄처벌법",
        "article": "제8조의2",
        "effective": "2021-01-01",
        "text": "긴급 임시조치로 피해자의 주거에서 퇴거하게 할 수 있다.",
        "url": "https://www.law.go.kr/c",
    },
    {
        "law": "위치정보법",
        "article": "제9조",
        "effective": "2020-01-01",
        "text": "개인위치정보의 수집과 이용에는 동의가 필요하다.",
        "url": "https://www.law.go.kr/d",
    },
]


def _fake_vector(text: str, dim: int = 8) -> list[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    v = [float(b) / 255.0 for b in h[:dim]]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def make_handler(dim: int = 8):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/embed":
            body = json.loads(request.content)
            texts = body.get("texts", [])
            return httpx.Response(
                200, json={"vectors": [_fake_vector(t, dim) for t in texts]}
            )
        if path == "/rerank":
            body = json.loads(request.content)
            q = body.get("query", "")
            docs = body.get("docs", [])
            qchars = set(q)
            scores = [float(sum(1 for ch in qchars if ch in d)) for d in docs]
            return httpx.Response(200, json={"scores": scores})
        if path == "/intent":
            body = json.loads(request.content)
            q = body.get("q", "")
            return httpx.Response(200, json={"locate_intent": "찾" in q, "score": 1.0})
        if path == "/ner":
            body = json.loads(request.content)
            text = body.get("text", "")
            return httpx.Response(
                200,
                json={
                    "spans": [
                        {
                            "start": 0,
                            "end": min(3, len(text)),
                            "label": "주소",
                            "text": text[:3],
                            "score": 0.9,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    return handler


def make_down_handler():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gpu down", request=request)

    return handler


@pytest.fixture
def articles_file(tmp_path, monkeypatch):
    p = tmp_path / "articles.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for a in ARTICLES:
            f.write(json.dumps({**a, "exposable": True, "excerpt": None}, ensure_ascii=False) + "\n")
        # 노출 불가 조문은 검색 말뭉치에 들어가면 안 된다.
        f.write(json.dumps({"law": "공직선거법", "article": "제40조", "effective": "x", "text": "송달 송달 명부 열람", "url": "u", "exposable": False, "excerpt": None}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"law": "플래그없음", "article": "제1조", "effective": "x", "text": "송달 송달", "url": "u"}, ensure_ascii=False) + "\n")
    monkeypatch.setenv("ARTICLES_PATH", str(p))
    return p


@pytest.fixture
def make_client(articles_file):
    @contextmanager
    def _make(transport):
        from app.main import create_app

        app = create_app(knowledge_path=str(FIXTURE), transport=transport)
        with TestClient(app) as client:
            yield client

    return _make


def _diagnose(client, victim_type, flags):
    r = client.post("/api/diagnose", json={"victim_type": victim_type, "flags": flags})
    assert r.status_code == 200, r.text
    return r.json()


def test_diagnose_order(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        data = _diagnose(client, "family", ["lawsuit"])
        assert [c["id"] for c in data["checklist"]] == ["J1", "J2", "L1"]


def test_diagnose_order_urgency_area(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        data = _diagnose(client, "family", ["moved_recently", "criminal_case"])
        assert [c["id"] for c in data["checklist"]] == ["A1", "P1", "A2", "L1"]


def test_family_only_gap(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        fam = _diagnose(client, "family", ["lawsuit"])
        assert "J1" in [c["id"] for c in fam["checklist"]]

        stalk = _diagnose(client, "stalking", ["lawsuit"])
        assert "J1" not in [c["id"] for c in stalk["checklist"]]
        na = [x for x in stalk["not_applicable"] if x["id"] == "J1"]
        assert len(na) == 1
        assert na[0]["reason"] == "이 피해 유형(스토킹)에는 해당 제도가 없습니다"


def test_pending_last(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        data = _diagnose(client, "family", ["children_school"])
        ids = [c["id"] for c in data["checklist"]]
        assert ids[-1] == "S1"
        s1 = data["checklist"][-1]
        assert s1["note"] == "2026-12-01 시행 예정, 지금은 쓸 수 없음"
        assert data["summary"]["pending"] == 1


def test_residual_vehicle(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        with_veh = _diagnose(client, "family", ["vehicle"])
        assert "D1" in [r["id"] for r in with_veh["residual"]]

        without = _diagnose(client, "family", [])
        assert "D1" not in [r["id"] for r in without["residual"]]


def test_always_item(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        for vt in ("family", "stalking", "dating", "sexual"):
            data = _diagnose(client, vt, [])
            assert "L1" in [c["id"] for c in data["checklist"]]


def test_422_extra_field(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        r = client.post(
            "/api/diagnose",
            json={"victim_type": "family", "flags": [], "extra": "x"},
        )
        assert r.status_code == 422


def test_422_unknown_flag(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        r = client.post(
            "/api/diagnose",
            json={"victim_type": "family", "flags": ["bogus_flag"]},
        )
        assert r.status_code == 422
        assert "bogus_flag" not in r.text


def test_422_no_echo(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        r = client.post(
            "/api/diagnose",
            json={"victim_type": "family", "flags": [], "SECRETMARKER": "abc"},
        )
        assert r.status_code == 422
        assert "SECRETMARKER" not in r.text


def test_search_refusal(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        r = client.post("/api/search", json={"q": "누구를 찾는 방법"})
        assert r.status_code == 200
        data = r.json()
        assert data["refused"] is True
        assert "찾는 용도로는 쓸 수 없습니다" in data["message"]


def test_search_normal(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        r = client.post("/api/search", json={"q": "송달"})
        assert r.status_code == 200
        data = r.json()
        assert data["refused"] is False
        assert data["fallback"] is False
        assert 0 < len(data["results"]) <= 5
        for item in data["results"]:
            for key in ("law", "article", "effective", "text", "url", "score"):
                assert key in item


def test_search_fallback(make_client):
    with make_client(httpx.MockTransport(make_down_handler())) as client:
        r = client.post("/api/search", json={"q": "송달"})
        assert r.status_code == 200
        data = r.json()
        assert data["refused"] is False
        assert data["fallback"] is True
        assert len(data["results"]) >= 1
        assert data["results"][0]["law"] == "민사소송법"
        assert all(r["law"] not in ("공직선거법", "플래그없음") for r in data["results"])


def test_doccheck_ok(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        r = client.post("/api/doccheck", json={"text": "서울시 어딘가 123"})
        assert r.status_code == 200
        data = r.json()
        assert data["available"] is True
        assert len(data["spans"]) == 1
        assert data["spans"][0]["label"] == "주소"


def test_doccheck_down(make_client):
    with make_client(httpx.MockTransport(make_down_handler())) as client:
        r = client.post("/api/doccheck", json={"text": "서울시 어딘가 123"})
        assert r.status_code == 200
        assert r.json()["available"] is False


def test_access_code(make_client, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", "secret")
    with make_client(httpx.MockTransport(make_handler())) as client:
        r = client.get("/api/flags")
        assert r.status_code == 401
        assert r.json()["detail"] == "기관 코드가 필요합니다"

        r2 = client.get("/api/flags", headers={"X-Access-Code": "secret"})
        assert r2.status_code == 200

        r3 = client.get("/api/health")
        assert r3.status_code == 200


def test_security_headers(make_client):
    with make_client(httpx.MockTransport(make_handler())) as client:
        r = client.get("/api/health")
        h = r.headers
        assert "default-src 'self'" in h["content-security-policy"]
        assert h["cache-control"] == "no-store"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer"
        assert h["x-content-type-options"] == "nosniff"


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_no_body_logging(make_client):
    log = logging.getLogger("sumeunjip.access")
    handler = _ListHandler()
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        with make_client(httpx.MockTransport(make_handler())) as client:
            marker = "MARKERXYZ987"
            r = client.post("/api/search", json={"q": marker})
            assert r.status_code == 200
    finally:
        log.removeHandler(handler)

    all_text = "\n".join(handler.messages)
    assert marker not in all_text
    assert any("POST /api/search 200" in m for m in handler.messages)


def test_search_refuses_when_gpu_allows_but_rules_catch(make_client):
    # GPU 가 살아 있고 locate=False 를 돌려줘도 로컬 규칙과 OR 로 결합해 거절한다.
    with make_client(httpx.MockTransport(make_handler())) as client:
        for q in (
            "보호명령 신청하려는데 그 전에 상대 새 주소부터 알아야 해서요",
            "전입세대 열람 하는 법",
        ):
            data = client.post("/api/search", json={"q": q}).json()
            assert data["refused"] is True, q


def test_corpus_excludes_procedural_articles():
    from app.retrieval import Retriever

    r = Retriever()
    keys = {(a["law"], a["article"]) for a in r.articles}
    for k in (("공직선거법", "제40조"), ("자동차등록규칙", "제12조"), ("자동차관리법", "제7조"),
              ("민사소송법", "제162조"), ("출입국관리법", "제88조의3"), ("형사소송법", "제266조의3")):
        assert k not in keys, k
    by = {(a["law"], a["article"]): a for a in r.articles}
    j29 = by[("주민등록법", "제29조")]["text"]
    assert "제한하도록 신청할 수 있다" in j29          # 보호 조항(제6항)은 남긴다
    assert "세대주의 배우자" not in j29                # 교부 신청 자격(제2항)은 뺀다
    assert by[("주민등록법", "제29조")]["url"].startswith("https://www.law.go.kr/")
