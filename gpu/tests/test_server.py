import numpy as np
import pytest
from fastapi.testclient import TestClient

import server


class FakeEmbedder:
    def encode(self, texts, normalize_embeddings=True):
        rng = np.random.default_rng(0)
        v = rng.random((len(texts), 8))
        if normalize_embeddings:
            v = v / np.linalg.norm(v, axis=1, keepdims=True)
        return v


class FakeReranker:
    def predict(self, pairs, activation_fct=None):
        # 입력 순서를 보존하는 결정적 점수.
        return [i / 10.0 for i in range(len(pairs))]


class FakeGliner:
    def predict_entities(self, text, labels, threshold=0.4):
        return []


@pytest.fixture()
def client():
    server.M["embedder"] = FakeEmbedder()
    server.M["reranker"] = FakeReranker()
    server.M["gliner"] = FakeGliner()
    rng = np.random.default_rng(1)
    loc = rng.random(8)
    pro = rng.random(8)
    server.M["seeds"] = {
        "locate": loc / np.linalg.norm(loc),
        "protect": pro / np.linalg.norm(pro),
    }
    with TestClient(server.app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body["models"]) == {"embedder", "reranker", "gliner"}
    assert body["models"]["embedder"] is True


def test_embed_shape(client):
    r = client.post("/embed", json={"texts": ["a", "b"]})
    assert r.status_code == 200
    vecs = r.json()["vectors"]
    assert len(vecs) == 2
    assert all(len(v) == 8 for v in vecs)
    assert all(abs(np.linalg.norm(v) - 1.0) < 1e-6 for v in vecs)


def test_rerank_order(client):
    docs = ["d0", "d1", "d2"]
    r = client.post("/rerank", json={"query": "q", "docs": docs})
    assert r.status_code == 200
    scores = r.json()["scores"]
    assert scores == [0.0, 0.1, 0.2]


def test_ner_regex_address(client):
    text = "서울특별시 강남구 테헤란로 123 4동 567호"
    spans = server.regex_spans(text)
    labels = {s["label"] for s in spans}
    assert "주소" in labels


def test_ner_regex_plate(client):
    spans = server.regex_spans("차 번호는 12가3456 입니다")
    assert any(s["label"] == "차량" for s in spans)


def test_ner_regex_school(client):
    spans = server.regex_spans("아이는 한솔초등학교에 다녀요")
    assert any(s["label"] == "학교" for s in spans)


def test_ner_regex_company(client):
    spans = server.regex_spans("(주)대한물산에서 일합니다")
    assert any(s["label"] == "직장" for s in spans)


def test_merge_overlap():
    spans = [
        {"start": 0, "end": 5, "label": "주소", "text": "aaaaa", "score": 0.5},
        {"start": 2, "end": 10, "label": "주소", "text": "bbbbbbbb", "score": 0.9},
        {"start": 20, "end": 22, "label": "차량", "text": "cc", "score": 0.9},
    ]
    merged = server.merge_spans(spans)
    assert len(merged) == 2
    assert merged[0]["score"] == 0.9
    assert merged[0]["start"] == 2


def test_keyword_locate_positive():
    assert server.keyword_locate("그 사람이 지금 어디 사는지 알아내고 싶다") is True


def test_keyword_locate_protective_not_flagged():
    assert server.keyword_locate("가해자가 내 주소를 알지 못하게 차단하고 싶어요") is False


def test_decide_intent_keyword():
    locate, score = server.decide_intent("헤어진 사람이 어디 사는지 알아내고 싶다", 0.0, 0.0)
    assert locate is True
    assert isinstance(score, float)


def test_decide_intent_threshold():
    locate, score = server.decide_intent("임의 문장", 0.5, 0.1)
    assert locate is True
    assert score == 0.4


def test_413_large_body(client):
    big = "x" * (21 * 1024)
    r = client.post("/embed", json={"texts": [big]})
    assert r.status_code == 413


def test_422_bad_input(client):
    r = client.post("/embed", json={"texts": "not-a-list"})
    assert r.status_code == 422


def test_keyword_locate_protect_word_does_not_disable():
    # 보호 단어(신청)가 섞여도 타인 + 소재 + 탐색이면 locate.
    assert server.keyword_locate("보호명령 신청하려는데 그 전에 상대 새 주소부터 알아야 해서요") is True


def test_decide_intent_rule_overrides_low_score():
    locate, _ = server.decide_intent("전처 차량번호로 등록원부 떼서 주소 확인 가능한가요", -0.5, 0.5)
    assert locate is True
