"""검색 조문 종합 답변(/api/search_answer) — GPU 는 전부 모의(합성 데이터, 외부 호출 없음)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app import search_answer as sa
from tests.test_api import ARTICLES, _ListHandler, make_client, articles_file  # noqa: F401  (fixtures)
from tests.test_llm import FakeGPU

RETRIEVED_KEYS = {(a["law"], a["article"]) for a in ARTICLES}
URLS = {a["url"] for a in ARTICLES}


class SearchGPU(FakeGPU):
    """재순위까지 흉내(질문 글자 겹침) → BM25 폴백이 아닌 정상 검색 경로. /llm/answer 는 items 를 기록."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.rerank_down = False
        # None 이면 받은 items 의 첫 id 를 인용하는 생성 답.
        self.answer_out = None  # type: ignore[assignment]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/rerank":
            self.calls.append((path, {}))
            if self.rerank_down:
                return httpx.Response(500)
            body = json.loads(request.content)
            q = set(body.get("query", ""))
            return httpx.Response(200, json={"scores": [float(sum(1 for ch in q if ch in d))
                                                        for d in body.get("docs", [])]})
        if path == "/llm/answer" and self.answer_out is None and not (self.fail_llm or self.llm_status
                                                                      or self.connect_fail):
            body = json.loads(request.content)
            self.calls.append((path, body))
            first = body["items"][0]["id"]
            return httpx.Response(200, json={"answer": f"이 조문은 피해자를 보호하는 내용입니다 [{first}].",
                                             "citations": [first, "ZZ9"], "grounded": True,
                                             "mode": "generated"})
        return super().__call__(request)


@pytest.fixture
def sgpu():
    return SearchGPU(intent_hit="@@없음@@")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("LLM_FEATURES", raising=False)
    monkeypatch.delenv("REQUIRE_ACCESS_CODE", raising=False)
    monkeypatch.delenv("ACCESS_CODE", raising=False)


Q = "송달 주소를 비공개로 할 수 있나요?"


def _ask(c, q=Q, strategy=None, history=None):
    body = {"q": q}
    if history is not None:
        body["history"] = history
    url = "/api/search_answer" + (f"?strategy={strategy}" if strategy else "")
    return c.post(url, json=body)


def _assert_citations_in_retrieved(d):
    got = {(x["law"], x["article"]) for x in d["citations"]}
    res = {(x["law"], x["article"]) for x in d["results"]}
    assert got <= res <= RETRIEVED_KEYS
    assert all(x["url"] in URLS and set(x) == {"law", "article", "url"} for x in d["citations"])


# --- 정상 경로·계약 ------------------------------------------------------------
def test_rows_default_contract(make_client, sgpu):
    with make_client(httpx.MockTransport(sgpu)) as c:
        r = _ask(c)
        assert r.status_code == 200
        d = r.json()
    assert d["refused"] is False and d["strategy"] == "rows" and d["fallback"] is False
    assert d["grounded"] is True and d["mode"] == "generated" and d["ai_generated"] is True
    assert len(d["results"]) == 4 and {"law", "article", "text", "url", "score"} <= set(d["results"][0])
    assert d["citations"] and "[" not in d["answer"]
    _assert_citations_in_retrieved(d)
    (sent,) = sgpu.llm_calls("answer")
    # 행 방식: 검색 조문을 근거로 드는 지식표 행만, 최대 5개, 컨텍스트 조문은 검색된 조문뿐.
    ids = [x["id"] for x in sent["items"]]
    assert 1 <= len(ids) <= 5 and set(ids) <= {"J1", "A2", "P1", "L1"}
    assert set(ids) <= set(d["rows"])
    for it in sent["items"]:
        for a in it["articles"]:
            assert (a["law"], a["article"]) in RETRIEVED_KEYS
    blob = json.dumps(sent, ensure_ascii=False)
    assert "공직선거법" not in blob and "플래그없음" not in blob


def test_rows_ordered_by_article_rank():
    results = [{"law": "위치정보법", "article": "제9조"}, {"law": "민사소송법", "article": "제163조"},
               {"law": "없는법", "article": "제1조"}]
    idx = {("민사소송법", "제163조"): ["J1"], ("위치정보법", "제9조"): ["L1", "L2"]}
    assert sa.rows_for_results(results, idx) == ["L1", "L2", "J1"]
    assert sa.rows_for_results(results, idx, limit=2) == ["L1", "L2"]


def test_row_index_uses_article_base():
    idx = sa.build_row_index([{"id": "A1", "basis": [{"law": "주민등록법", "article": "제29조 제6항"},
                                                     {"law": "주민등록법", "article": "제29조 제7항"}]},
                              {"id": "D1", "basis": []}])
    assert idx == {("주민등록법", "제29조"): ["A1"]}


def test_articles_strategy_items_and_citation_mapping(make_client, sgpu):
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy="articles").json()
    assert d["refused"] is False and d["strategy"] == "articles" and d["grounded"] is True
    (sent,) = sgpu.llm_calls("answer")
    assert set(sent) == {"question", "items"}
    items = sent["items"]
    assert [x["id"] for x in items] == [f"Z{i}" for i in range(1, len(items) + 1)]
    import re
    from app.llm import ITEM_ID_RE
    assert all(re.match(ITEM_ID_RE, x["id"]) for x in items)
    for x in items:
        assert set(x) <= {"id", "measure", "plain", "articles"}
        assert len(x["measure"]) <= 500 and len(x["plain"]) <= 1000
    assert len(json.dumps(sent, ensure_ascii=False).encode()) < 20 * 1024
    # 인용 Z1 → 검색 1위 조문, 목록 밖 ZZ9 는 버린다. 합성 id 는 본문에 남지 않는다.
    assert d["citations"] == [{k: d["results"][0][k] for k in ("law", "article", "url")}]
    assert "Z1" not in d["answer"] and "[" not in d["answer"]
    _assert_citations_in_retrieved(d)


def test_articles_extractive_rebuilt_from_article_text(make_client, sgpu):
    sgpu.answer_out = {"answer": "관련 내용은 다음과 같습니다.\n■ Z2\n- 막는 제도: x", "citations": ["Z2"],
                       "grounded": True, "mode": "extractive"}
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy="articles").json()
    assert d["mode"] == "extractive" and d["ai_generated"] is False and d["grounded"] is True
    second = d["results"][1]
    assert d["citations"] == [{k: second[k] for k in ("law", "article", "url")}]
    assert second["law"] in d["answer"] and "Z2" not in d["answer"] and "막는 제도" not in d["answer"]


@pytest.mark.parametrize("strategy", ["rows", "articles"])
def test_strategy_switch_sends_different_items(make_client, sgpu, strategy):
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy=strategy).json()
    (sent,) = sgpu.llm_calls("answer")
    synth = all(x["id"].startswith("Z") for x in sent["items"])
    assert synth is (strategy == "articles") and d["strategy"] == strategy


def test_unknown_strategy_rejected(make_client, sgpu):
    with make_client(httpx.MockTransport(sgpu)) as c:
        r = _ask(c, strategy="bogus")
    assert r.status_code == 422 and Q not in r.text


# --- not_found -----------------------------------------------------------------
@pytest.fixture
def unmapped_articles(tmp_path, monkeypatch):
    p = tmp_path / "unmapped.jsonl"
    rows = [{"law": "합성법", "article": "제1조", "effective": "x", "url": "https://www.law.go.kr/z",
             "text": "송달 주소 비공개에 관한 합성 조문이다.", "exposable": True, "excerpt": None}]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    monkeypatch.setenv("ARTICLES_PATH", str(p))
    return p


def test_rows_not_found_when_nothing_maps(make_client, sgpu, unmapped_articles):
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c).json()
    assert d["refused"] is False and d["mode"] == "not_found" and d["grounded"] is False
    assert d["citations"] == [] and d["results"] and d["rows"] == []
    assert "1366" in d["answer"] and "132" in d["answer"]
    assert sgpu.llm_calls("answer") == []


def test_not_found_when_retrieval_empty(make_client, sgpu):
    with make_client(httpx.MockTransport(sgpu)) as c:
        c.app.state.retriever.articles = []
        c.app.state.retriever.embedded = False
        c.app.state.retriever._n_docs = 0
        d = _ask(c, strategy="articles").json()
    assert d["results"] == [] and d["mode"] == "not_found" and d["citations"] == []
    assert sgpu.llm_calls("answer") == []


@pytest.mark.parametrize("strategy", ["rows", "articles"])
def test_ungrounded_gpu_answer_is_not_found(make_client, sgpu, strategy):
    sgpu.answer_out = {"answer": "아마 될 겁니다", "citations": [], "grounded": False}
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy=strategy).json()
    assert d["grounded"] is False and d["citations"] == [] and "확인되지 않습니다" in d["answer"]


@pytest.mark.parametrize("strategy", ["rows", "articles"])
def test_out_of_set_citations_dropped(make_client, sgpu, strategy):
    # 검색 집합 밖 id(행 방식: 검색 조문과 무관한 행, 조문 방식: 없는 합성 id)만 인용 → 답하지 않는다.
    sgpu.answer_out = {"answer": "근거가 있는 답입니다 [Z9].", "citations": ["Z9", "S1", "A1"],
                       "grounded": True}
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy=strategy).json()
    assert d["grounded"] is False and d["citations"] == []


def test_citations_from_rows_restricted_to_retrieved():
    items = {"A2": {"id": "A2", "basis": [{"law": "주민등록법", "article": "제29조 제1항"},
                                          {"law": "합성법", "article": "제7조"}]}}
    results = [{"law": "주민등록법", "article": "제29조", "url": "u1"},
               {"law": "민사소송법", "article": "제163조", "url": "u2"}]
    assert sa.citations_from_rows(["A2"], items, results) == [
        {"law": "주민등록법", "article": "제29조", "url": "u1"}]


# --- 입력 가드(등급) -------------------------------------------------------------
@pytest.mark.parametrize("strategy", ["rows", "articles"])
def test_third_party_refused_before_retrieval_and_llm(make_client, sgpu, strategy):
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, "상대 새 주소부터 알아야 해서요 어디 사는지", strategy).json()
    assert d["refused"] is True and d["tier"] == "third_party_locate"
    assert d["results"] == [] and d["alternatives"]
    paths = [p for p, _ in sgpu.calls]
    assert "/llm/answer" not in paths and "/rerank" not in paths


def test_rephrase_tier(make_client, sgpu):
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, "ㄴㅍ ㅈㅅ ㅇㅇㄴㄴ ㅂ").json()
    assert d["refused"] is False and d["rephrase"] is True and d["tier"] == "rephrase"
    assert d["results"] == [] and sgpu.llm_calls("answer") == []


def test_ambiguous_tier_gets_protective(make_client, sgpu):
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, "남편 회사 주소가 바뀌었대요.").json()
    assert d["refused"] is False and d["tier"] == "ambiguous"
    assert d["protective"]["alternatives"] and "1366" in d["protective"]["message"]


def test_neutral_tier_has_no_protective(make_client, sgpu):
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, "접근금지는 어디서 신청하나요?").json()
    assert d["tier"] == "neutral" and "protective" not in d


def test_split_question_refused_with_history(make_client, sgpu):
    hist = [{"role": "user", "text": "헤어진 여자친구 얘기예요."},
            {"role": "assistant", "text": "어떤 점이 궁금하신가요?"}]
    q = "지금 사는 곳 알 수 있는 방법 있나요?"
    with make_client(httpx.MockTransport(sgpu)) as c:
        assert _ask(c, q).json()["refused"] is False
        d = _ask(c, q, history=hist).json()
    assert d["refused"] is True and d["results"] == []


# --- 출력 가드 -------------------------------------------------------------------
@pytest.mark.parametrize("strategy", ["rows", "articles"])
def test_output_guard_refuses_locate_answer(make_client, sgpu, strategy):
    sgpu.answer_out = {"answer": "그의 거주지를 확인하려면 기록을 보면 됩니다 [Z1] [J1].",
                       "citations": ["Z1", "J1", "A2", "P1", "L1"], "grounded": True}
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy=strategy).json()
    blob = json.dumps(d, ensure_ascii=False)
    assert d["refused"] is True and "거주지를 확인하려면" not in blob
    assert d["results"] == [] and d["alternatives"]


@pytest.mark.parametrize("strategy", ["rows", "articles"])
def test_output_guard_embedding_threshold(make_client, sgpu, strategy):
    sgpu.answer_out = {"answer": "이 제도는 보호 조치입니다 [Z1] [J1].",
                       "citations": ["Z1", "J1", "A2", "P1", "L1"], "grounded": True}
    sgpu.intent_scores = {"이 제도는 보호": 0.5}
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy=strategy).json()
    assert d["refused"] is True


@pytest.mark.parametrize("strategy", ["rows", "articles"])
def test_unsupported_notify_denial_dropped(make_client, sgpu, strategy):
    sgpu.answer_out = {"answer": "신청해도 상대에게 알려지지 않아요 [Z1] [J1].",
                       "citations": ["Z1", "J1", "A2", "P1", "L1"], "grounded": True}
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy=strategy).json()
    assert d["grounded"] is False and "알려지지 않아요" not in d["answer"] and d["citations"] == []


def test_gpu_refusal_passes_through(make_client, sgpu):
    sgpu.answer_out = {"refused": True}
    with make_client(httpx.MockTransport(sgpu)) as c:
        for s in ("rows", "articles"):
            assert _ask(c, strategy=s).json()["refused"] is True


def test_articles_notify_and_effect_questions_not_generated(make_client, sgpu):
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, "송달 신청하면 상대에게 알려지나요?", "articles").json()
        assert d["grounded"] is False and "확인되지 않" in d["answer"] and d["ai_generated"] is False
        d = _ask(c, "송달 보호는 언제부터 효과가 있나요?", "articles").json()
        assert d["grounded"] is False and d["mode"] == "not_found"
    assert sgpu.llm_calls("answer") == []


# --- 폴백·장애 ----------------------------------------------------------------------
def test_bm25_fallback_still_answers_and_is_marked(make_client, sgpu):
    sgpu.rerank_down = True
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c).json()
    assert d["fallback"] is True and d["results"] and d["refused"] is False
    _assert_citations_in_retrieved(d)


@pytest.mark.parametrize("kind", ["fail", "connect", "timeout"])
def test_llm_failure_returns_results_without_answer(make_client, sgpu, kind):
    if kind == "fail":
        sgpu.fail_llm = True
    elif kind == "connect":
        sgpu.connect_fail = True
    else:
        sgpu.llm_status = 504
    with make_client(httpx.MockTransport(sgpu)) as c:
        r = _ask(c, strategy="articles")
        assert r.status_code == 200
        d = r.json()
        assert d["answer"] is None and d["mode"] == "unavailable" and d["results"]
        assert d["citations"] == [] and d["grounded"] is False
        if kind == "connect":
            assert c.get("/api/features").json()["llm"] is False


def test_llm_disabled_by_env_returns_results_only(make_client, sgpu, monkeypatch):
    monkeypatch.setenv("LLM_FEATURES", "0")
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c).json()
    assert d["answer"] is None and d["mode"] == "unavailable" and d["results"]
    assert sgpu.llm_calls("answer") == []


# --- 입력 상한·이력 ---------------------------------------------------------------
@pytest.mark.parametrize("body", [
    {"q": ""},
    {"q": "가" * 501},
    {"q": "뜻?", "extra": 1},
    {"q": "뜻?", "history": [{"role": "user", "text": "가"}] * 7},
    {"q": "뜻?", "history": [{"role": "user", "text": "가" * 501}]},
    {"q": "뜻?", "history": [{"role": "system", "text": "가"}]},
    {"q": "뜻?", "history": [{"role": "user", "text": "가", "x": 1}]},
])
def test_input_limits(make_client, sgpu, body):
    with make_client(httpx.MockTransport(sgpu)) as c:
        r = c.post("/api/search_answer", json=body)
    assert r.status_code == 422
    assert sgpu.llm_calls("answer") == []


def test_history_at_limits_accepted(make_client, sgpu):
    hist = [{"role": "user", "text": "가" * 500}] * 5 + [{"role": "assistant", "text": "나" * 500}]
    with make_client(httpx.MockTransport(sgpu)) as c:
        assert c.post("/api/search_answer", json={"q": "가" * 500, "history": hist}).status_code == 200


def test_access_code_required_when_enabled(make_client, sgpu, monkeypatch):
    monkeypatch.setenv("REQUIRE_ACCESS_CODE", "1")
    monkeypatch.setenv("ACCESS_CODE", "synthetic-code")
    with make_client(httpx.MockTransport(sgpu)) as c:
        assert _ask(c).status_code == 401
        r = c.post("/api/search_answer", json={"q": Q}, headers={"X-Access-Code": "synthetic-code"})
        assert r.status_code == 200


def test_bodies_not_logged(make_client, sgpu):
    log = logging.getLogger("sumeunjip.access")
    h = _ListHandler()
    log.addHandler(h)
    marker = "MARKERSA4242"
    sgpu.answer_out = {"answer": marker + " 답 [Z1] [J1]", "citations": ["Z1", "J1"], "grounded": True}
    try:
        with make_client(httpx.MockTransport(sgpu)) as c:
            for s in ("rows", "articles"):
                _ask(c, "송달 " + marker, s, history=[{"role": "user", "text": marker}])
    finally:
        log.removeHandler(h)
    joined = "\n".join(h.messages)
    assert marker not in joined
    assert "POST /api/search_answer 200" in joined
