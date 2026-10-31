"""내부 LLM 프록시(/api/features·intake·answer·rewrite) — GPU 는 전부 모의(합성 데이터)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from tests.test_api import _ListHandler, make_client, articles_file  # noqa: F401  (fixtures)


class FakeGPU:
    """GPU 서버 모의. 호출 경로·본문을 기록한다(테스트 메모리 안에서만)."""

    def __init__(self, *, llm: bool = True, fail_llm: bool = False, intent_hit: str = "찾") -> None:
        self.llm = llm
        self.fail_llm = fail_llm
        self.intent_hit = intent_hit
        self.calls: list[tuple[str, dict]] = []
        self.intake_out: dict = {
            "victim_type": "family",
            "flags": ["moved_recently", "lawsuit", "bogus_flag"],
            "confidence": 0.8,
            "unclear": ["자녀가 학교에 다니나요?"],
        }
        self.answer_out: dict = {"answer": "송달 주소를 따로 정할 수 있습니다.", "citations": ["J1", "ZZ9"], "grounded": True}
        self.rewrite_out: dict = {"rewritten": "[주소 생략]에서 일합니다."}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        if path == "/health":
            return httpx.Response(200, json={"ok": True, "llm": self.llm})
        if path == "/intent":
            return httpx.Response(200, json={"locate_intent": self.intent_hit in body.get("q", ""), "score": 1.0})
        if path == "/embed":
            return httpx.Response(200, json={"vectors": [[1.0, 0.0] for _ in body.get("texts", [])]})
        if path.startswith("/llm/"):
            if self.fail_llm:
                return httpx.Response(500, json={"detail": "x"})
            if path == "/llm/intake":
                return httpx.Response(200, json=self.intake_out)
            if path == "/llm/answer":
                return httpx.Response(200, json=self.answer_out)
            if path == "/llm/rewrite":
                return httpx.Response(200, json=self.rewrite_out)
        return httpx.Response(404)

    def llm_calls(self, name: str) -> list[dict]:
        return [b for p, b in self.calls if p == f"/llm/{name}"]


@pytest.fixture
def gpu():
    return FakeGPU()


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch):
    monkeypatch.delenv("LLM_FEATURES", raising=False)
    monkeypatch.delenv("REQUIRE_ACCESS_CODE", raising=False)
    monkeypatch.delenv("ACCESS_CODE", raising=False)


# --- 접근 코드 ---------------------------------------------------------------
def test_access_code_disabled_by_default(make_client, monkeypatch, gpu):
    monkeypatch.setenv("ACCESS_CODE", "secret")
    with make_client(httpx.MockTransport(gpu)) as c:
        assert c.get("/api/flags").status_code == 200


def test_require_access_code_without_code_fails_closed(make_client, monkeypatch, gpu):
    monkeypatch.setenv("REQUIRE_ACCESS_CODE", "1")
    with make_client(httpx.MockTransport(gpu)) as c:
        assert c.get("/api/flags").status_code == 401
        assert c.get("/api/features").status_code == 401


# --- features ---------------------------------------------------------------
def test_features_on(make_client, gpu):
    with make_client(httpx.MockTransport(gpu)) as c:
        assert c.get("/api/features").json() == {"llm": True}


def test_features_off_when_gpu_says_no(make_client):
    with make_client(httpx.MockTransport(FakeGPU(llm=False))) as c:
        assert c.get("/api/features").json() == {"llm": False}


def test_features_off_when_gpu_down(make_client):
    def down(request):
        raise httpx.ConnectError("down", request=request)

    with make_client(httpx.MockTransport(down)) as c:
        assert c.get("/api/features").json() == {"llm": False}


def test_features_off_by_env(make_client, monkeypatch, gpu):
    monkeypatch.setenv("LLM_FEATURES", "0")
    with make_client(httpx.MockTransport(gpu)) as c:
        assert c.get("/api/features").json() == {"llm": False}
        r = c.post("/api/intake", json={"text": "이사했고 소송 중입니다"})
        assert r.status_code == 503
    assert not gpu.llm_calls("intake")


# --- intake -----------------------------------------------------------------
def test_intake_filters_to_known_flags(make_client, gpu):
    with make_client(httpx.MockTransport(gpu)) as c:
        r = c.post("/api/intake", json={"text": "최근 이사했고 소송이 걸려 있습니다"})
        assert r.status_code == 200
        d = r.json()
        assert d["refused"] is False
        assert d["victim_type"] == "family"
        assert d["flags"] == ["moved_recently", "lawsuit"]
        assert d["ai_generated"] is True
        assert d["unclear"] == ["자녀가 학교에 다니나요?"]
    assert gpu.llm_calls("intake") == [{"text": "최근 이사했고 소송이 걸려 있습니다"}]


def test_intake_drops_bad_victim_type_and_guarded_questions(make_client, gpu):
    gpu.intake_out = {"victim_type": "other", "flags": "x", "confidence": "high",
                      "unclear": ["상대방 새 주소 찾는 방법 아세요?", "언제 이사했나요?"]}
    with make_client(httpx.MockTransport(gpu)) as c:
        d = c.post("/api/intake", json={"text": "이사했습니다"}).json()
        assert d["victim_type"] is None
        assert d["flags"] == []
        assert d["confidence"] == 0.0
        assert d["unclear"] == ["언제 이사했나요?"]


def test_intake_input_guard_blocks_before_llm(make_client, gpu):
    with make_client(httpx.MockTransport(gpu)) as c:
        d = c.post("/api/intake", json={"text": "전입세대 열람 하는 법 알려줘"}).json()
        assert d["refused"] is True
        assert "찾는 용도로는 쓸 수 없습니다" in d["message"]
    assert not gpu.llm_calls("intake")


def test_intake_503_when_llm_fails(make_client):
    with make_client(httpx.MockTransport(FakeGPU(fail_llm=True))) as c:
        r = c.post("/api/intake", json={"text": "이사했습니다"})
        assert r.status_code == 503
        assert "이사" not in r.text
        # 실패 직후 features 는 꺼진 것으로 보인다.
        assert c.get("/api/features").json() == {"llm": False}


@pytest.mark.parametrize("payload", [
    {"text": ""},
    {"text": "가" * 2001},
    {"text": "이사", "extra": 1},
])
def test_intake_strict_validation(make_client, gpu, payload):
    with make_client(httpx.MockTransport(gpu)) as c:
        r = c.post("/api/intake", json=payload)
        assert r.status_code == 422
        assert "extra" not in r.text
    assert not gpu.llm_calls("intake")


# --- answer -----------------------------------------------------------------
def test_answer_context_is_items_and_exposable_articles_only(make_client, gpu):
    with make_client(httpx.MockTransport(gpu)) as c:
        r = c.post("/api/answer", json={"question": "이게 무슨 뜻인가요?", "item_ids": ["J1", "D1", "A2", "Q99"]})
        assert r.status_code == 200
        d = r.json()
        assert d["refused"] is False
        assert d["grounded"] is True
        assert d["citations"] == ["J1"]          # 컨텍스트 밖 인용(ZZ9)은 버린다
        assert d["ai_generated"] is True
    (sent,) = gpu.llm_calls("answer")
    assert sent["item_ids"] == ["J1", "D1", "A2"]   # 없는 id 는 빠진다
    ids = [x["id"] for x in sent["context"]]
    assert "J1" in ids and "D1" in ids and "A2" in ids
    # 노출 가능 조문(제163조, 제29조)만 붙고, 플래그 없는/노출 불가 조문은 없다.
    assert "J1:민사소송법 제163조" in ids
    assert "A2:주민등록법 제29조" in ids
    blob = json.dumps(sent, ensure_ascii=False)
    assert "공직선거법" not in blob and "플래그없음" not in blob
    # 잔여 위험(D1) 행에는 exposure·limits 를 싣지 않는다.
    d1 = next(x for x in sent["context"] if x["id"] == "D1")["text"]
    assert "limits:" not in d1 and "exposure" not in d1
    assert all("exposure" not in x["text"] for x in sent["context"])
    assert len(json.dumps(sent, ensure_ascii=False).encode()) < 20 * 1024


def test_answer_not_grounded_falls_back(make_client, gpu):
    gpu.answer_out = {"answer": "아마 될 겁니다", "citations": [], "grounded": False}
    with make_client(httpx.MockTransport(gpu)) as c:
        d = c.post("/api/answer", json={"question": "가능한가요?", "item_ids": ["J1"]}).json()
        assert d["grounded"] is False
        assert "확인되지 않습니다" in d["answer"] and "1366" in d["answer"] and "132" in d["answer"]
        assert d["citations"] == []


def test_answer_unknown_items_skip_llm(make_client, gpu):
    with make_client(httpx.MockTransport(gpu)) as c:
        d = c.post("/api/answer", json={"question": "뜻이 뭔가요?", "item_ids": ["Q99"]}).json()
        assert d["grounded"] is False
    assert not gpu.llm_calls("answer")


def test_answer_input_guard(make_client, gpu):
    with make_client(httpx.MockTransport(gpu)) as c:
        d = c.post("/api/answer", json={"question": "상대 새 주소부터 알아야 해서요 어디 사는지", "item_ids": ["J1"]}).json()
        assert d["refused"] is True
    assert not gpu.llm_calls("answer")


def test_answer_output_guard(make_client, gpu):
    gpu.answer_out = {"answer": "그 사람을 찾는 방법은 다음과 같습니다", "citations": ["J1"], "grounded": True}
    with make_client(httpx.MockTransport(gpu)) as c:
        d = c.post("/api/answer", json={"question": "무슨 뜻인가요?", "item_ids": ["J1"]}).json()
        assert d["refused"] is True
        assert "다음과 같습니다" not in json.dumps(d, ensure_ascii=False)


@pytest.mark.parametrize("payload", [
    {"question": "뜻?", "item_ids": []},
    {"question": "뜻?", "item_ids": ["j1"]},
    {"question": "뜻?", "item_ids": ["J1; drop"]},
    {"question": "뜻?", "item_ids": ["J1"] * 9},
    {"question": "가" * 501, "item_ids": ["J1"]},
])
def test_answer_strict_validation(make_client, gpu, payload):
    with make_client(httpx.MockTransport(gpu)) as c:
        assert c.post("/api/answer", json=payload).status_code == 422


def test_answer_503(make_client):
    with make_client(httpx.MockTransport(FakeGPU(fail_llm=True))) as c:
        assert c.post("/api/answer", json={"question": "뜻?", "item_ids": ["J1"]}).status_code == 503


# --- rewrite ----------------------------------------------------------------
def test_rewrite_ok_and_remaining(make_client, gpu):
    text = "저는 가나다로 12에서 일합니다. 라마빌딩 3층."
    spans = [{"start": 3, "end": 9, "label": "주소"}, {"start": 19, "end": 23, "label": "건물"}]
    gpu.rewrite_out = {"rewritten": "저는 [주소]에서 일합니다. 라마빌딩 3층."}
    with make_client(httpx.MockTransport(gpu)) as c:
        d = c.post("/api/rewrite", json={"text": text, "spans": spans}).json()
        assert d["rewritten"].startswith("저는 [주소]")
        assert d["remaining"] == [text[19:23]]
        assert d["ai_generated"] is True
    (sent,) = gpu.llm_calls("rewrite")
    assert sent["spans"][0] == {"start": 3, "end": 9, "label": "주소"}


def test_rewrite_drops_out_of_range_spans(make_client, gpu):
    with make_client(httpx.MockTransport(gpu)) as c:
        r = c.post("/api/rewrite", json={"text": "짧은 글", "spans": [{"start": 2, "end": 900}]})
        assert r.status_code == 200
    assert gpu.llm_calls("rewrite")[0]["spans"] == []


def test_rewrite_503_on_empty(make_client, gpu):
    gpu.rewrite_out = {"rewritten": "  "}
    with make_client(httpx.MockTransport(gpu)) as c:
        assert c.post("/api/rewrite", json={"text": "글", "spans": []}).status_code == 503


def test_rewrite_strict_validation(make_client, gpu):
    with make_client(httpx.MockTransport(gpu)) as c:
        assert c.post("/api/rewrite", json={"text": "가" * 4001, "spans": []}).status_code == 422
        assert c.post("/api/rewrite", json={"text": "글", "spans": [{"start": 0, "end": 1, "x": 1}]}).status_code == 422


# --- 로그 -------------------------------------------------------------------
def test_llm_endpoints_do_not_log_bodies(make_client, gpu):
    log = logging.getLogger("sumeunjip.access")
    h = _ListHandler()
    log.addHandler(h)
    marker = "MARKERLLM4242"
    gpu.answer_out = {"answer": marker + " 답", "citations": ["J1"], "grounded": True}
    gpu.rewrite_out = {"rewritten": marker}
    try:
        with make_client(httpx.MockTransport(gpu)) as c:
            c.post("/api/intake", json={"text": marker})
            c.post("/api/answer", json={"question": marker, "item_ids": ["J1"]})
            c.post("/api/rewrite", json={"text": marker, "spans": []})
    finally:
        log.removeHandler(h)
    joined = "\n".join(h.messages)
    assert marker not in joined
    for p in ("intake", "answer", "rewrite"):
        assert f"POST /api/{p} 200" in joined
