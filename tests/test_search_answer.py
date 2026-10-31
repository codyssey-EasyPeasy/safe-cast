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
        self.rerank_const: float | None = None  # 정하면 모든 재순위 점수를 이 값으로(무관 질문 흉내)
        self.rerank_bodies: list[dict] = []
        # None 이면 받은 items 의 첫 id 를 인용하는 생성 답.
        self.answer_out = None  # type: ignore[assignment]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/rerank":
            self.calls.append((path, {}))
            if self.rerank_down:
                return httpx.Response(500)
            body = json.loads(request.content)
            self.rerank_bodies.append(body)
            if self.rerank_const is not None:
                return httpx.Response(200, json={"scores": [self.rerank_const] * len(body.get("docs", []))})
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
    assert 1 <= len(ids) <= sa.MAX_ROWS == 2 and set(ids) <= {"J1", "A2", "P1", "L1"}
    assert set(ids) <= set(d["rows"])
    for it in sent["items"]:
        for a in it["articles"]:
            assert (a["law"], a["article"]) in RETRIEVED_KEYS
    blob = json.dumps(sent, ensure_ascii=False)
    assert "공직선거법" not in blob and "플래그없음" not in blob


def test_row_candidates_rank_and_article_score():
    results = [{"law": "위치정보법", "article": "제9조", "score": 0.4},
               {"law": "민사소송법", "article": "제163조", "score": 0.9},
               {"law": "없는법", "article": "제1조", "score": 0.1}]
    idx = {("민사소송법", "제163조"): ["J1", "L2"], ("위치정보법", "제9조"): ["L1", "L2"]}
    # (id, 가장 높은 검색 순위, 그 행이 드는 검색 조문의 최고 재순위 점수), 검색 순위 순
    assert sa.row_candidates(results, idx) == [("L1", 0, 0.4), ("L2", 0, 0.9), ("J1", 1, 0.9)]
    assert sa.row_candidates(results, idx, limit=2) == [("L1", 0, 0.4), ("L2", 0, 0.9)]


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


# --- 시행 전 조문(2026-11-01 검토: 시행일이 미래인 조문을 현행처럼 안내) ----------------
def test_pending_since_and_note():
    a = {"law": "스토킹처벌법", "article": "제17조의5", "effective": "2027-04-22"}
    assert sa.pending_since(a, "2026-11-01") == "2027-04-22"
    assert sa.pending_since(a, "2027-04-22") == ""  # 시행일 당일부터 현행
    assert sa.pending_since({**a, "effective": "x"}, "2026-11-01") == ""
    assert sa.pending_since({**a, "effective": ""}, "2026-11-01") == ""
    note = sa.pending_note([a, {"law": "민법", "article": "제1조", "effective": "2020-01-01"}], "2026-11-01")
    assert "스토킹처벌법 제17조의5(2027-04-22 시행)" in note and "민법" not in note
    assert sa.pending_note([{**a, "effective": "2020-01-01"}], "2026-11-01") == ""


@pytest.mark.parametrize("strategy", ["rows", "articles"])
def test_pending_cited_article_gets_note(make_client, sgpu, monkeypatch, strategy):
    # 모든 시험 조문의 시행일보다 앞인 '오늘' → 인용 조문은 전부 시행 전.
    monkeypatch.setattr(sa, "_today", lambda: "2000-01-01")
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy=strategy).json()
    assert d["grounded"] is True and d["citations"]
    assert "※ 시행 전 조문:" in d["answer"] and "지금은 이 조문으로 신청할 수 없습니다" in d["answer"]
    for cite in d["citations"]:
        assert f"{cite['law']} {cite['article']}(" in d["answer"]
    if strategy == "articles":
        (sent,) = sgpu.llm_calls("answer")
        assert all(x["measure"].startswith("[시행 전:") for x in sent["items"])


@pytest.mark.parametrize("strategy", ["rows", "articles"])
def test_in_force_articles_have_no_note(make_client, sgpu, monkeypatch, strategy):
    monkeypatch.setattr(sa, "_today", lambda: "2099-01-01")
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy=strategy).json()
    assert d["grounded"] is True and "시행 전" not in d["answer"]
    if strategy == "articles":
        (sent,) = sgpu.llm_calls("answer")
        assert not any("시행 전" in x["measure"] for x in sent["items"])


def test_pending_note_not_added_to_not_found_or_refusal(make_client, sgpu, monkeypatch):
    monkeypatch.setattr(sa, "_today", lambda: "2000-01-01")
    sgpu.answer_out = {"answer": "x", "citations": [], "grounded": False, "mode": "generated"}
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy="articles").json()
    assert d["mode"] == "not_found" and "시행 전" not in d["answer"]


def test_articles_extractive_marks_pending(make_client, sgpu, monkeypatch):
    monkeypatch.setattr(sa, "_today", lambda: "2000-01-01")
    sgpu.answer_out = {"answer": "발췌", "citations": ["Z1"], "grounded": True, "mode": "extractive"}
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, strategy="articles").json()
    assert d["mode"] == "extractive" and "시행 예정, 시행 전)" in d["answer"]


# --- 행 고르기(최대 2개·제도/유형 불일치 제외·관련도 문턱) -------------------------------
def _it(iid, path, measure="", plain=""):
    return {"id": iid, "path": path, "measure": measure, "plain": plain, "basis": []}


ROWS = {
    "G1": _it("G1", "가정폭력 피해자보호명령(피해자 직접 청구)", "퇴거, 접근금지, 임시보호명령"),
    "K1": _it("K1", "스토킹 접근·연락", "긴급응급조치, 잠정조치(서면경고, 100m)"),
    "K2": _it("K2", "수사기관이 잠정조치를 신청하지 않을 때", "스토킹 피해자보호명령·임시보호명령"),
    "N1": _it("N1", "친권 경로", "피해자보호명령 친권행사 제한"),
    "S1": _it("S1", "가정폭력 피해 자녀 입학·전학", "주소지 외 취학 지원"),
    "S3": _it("S3", "스토킹 피해자(가족 포함) 취학", "취학 지원, 비공개"),
}


def test_topic_ok_drops_row_without_named_measure():
    q = "피해자보호명령 결정이 나기 전까지 당장 보호받을 방법이 있나요?"
    assert sa.topic_ok(q, ROWS["G1"]) and sa.topic_ok(q, ROWS["K2"])
    assert not sa.topic_ok(q, ROWS["K1"])   # 스토킹 잠정조치 행은 '보호명령'을 대지 않는다
    assert sa.topic_ok("우편물을 옮겨 받으려면?", ROWS["K1"])  # 제도 이름이 없는 질문은 거르지 않는다


def test_domain_ok_uses_row_heading_only():
    assert not sa.domain_ok("스토킹 피해 학생도 전학 지원을 받을 수 있나요?", ROWS["S1"])
    assert sa.domain_ok("스토킹 피해 학생도 전학 지원을 받을 수 있나요?", ROWS["S3"])
    assert sa.domain_ok("스토킹 피해자도 보호명령을 신청할 수 있나요?", ROWS["K2"])  # 제목 measure 에 스토킹
    assert sa.domain_ok("아이를 전학시킬 수 있나요?", ROWS["S1"])  # 질문이 유형을 대지 않음
    assert sa.domain_ok("가정폭력인데 친권을 막을 수 있나요?", ROWS["N1"])  # 행 제목에 유형 없음


def test_select_rows_orders_by_rerank_and_limits_to_two():
    q = "보호를 받을 수 있나요?"
    cands = [("K1", 0, 0.9), ("G1", 1, 0.9), ("N1", 2, 0.9), ("K2", 3, 0.9)]
    ids, rel = sa.select_rows(q, cands, ROWS, {"K1": 0.40, "G1": 0.95, "N1": 0.90, "K2": 0.85})
    assert ids == ["G1", "N1"] and rel["K1"] == 0.40


def test_select_rows_second_row_needs_ratio():
    q = "보호를 받을 수 있나요?"
    cands = [("G1", 0, 0.9), ("N1", 1, 0.9)]
    assert sa.select_rows(q, cands, ROWS, {"G1": 0.9, "N1": 0.44})[0] == ["G1"]
    assert sa.select_rows(q, cands, ROWS, {"G1": 0.9, "N1": 0.46})[0] == ["G1", "N1"]
    # 두 번째 행이 재순위 문턱 미만이면(조문 점수로만 관련) 붙이지 않는다
    assert sa.select_rows(q, cands, ROWS, {"G1": 0.04, "N1": 0.02})[0] == ["G1"]


def test_select_rows_filters_topic_and_domain_before_ranking():
    q = "스토킹 피해자도 법원에 직접 피해자보호명령을 신청할 수 있나요?"
    cands = [("G1", 0, 0.9), ("K2", 1, 0.9), ("K1", 2, 0.9)]
    ids, _ = sa.select_rows(q, cands, ROWS, {"G1": 0.99, "K2": 0.8, "K1": 0.7})
    assert ids == ["K2"]   # G1 은 가정폭력, K1 은 보호명령 아님


def test_select_rows_relevance_gate():
    q = "보호를 받을 수 있나요?"
    cands = [("G1", 0, 0.01), ("N1", 1, 0.04)]
    # 행 재순위·조문 재순위 모두 문턱 미만 → 보낼 행 없음
    assert sa.select_rows(q, cands, ROWS, {"G1": 0.02, "N1": 0.029})[0] == []
    # 행은 약해도 그 행이 드는 검색 조문의 재순위가 문턱 이상이면 관련 → 검색 순위 순으로
    cands2 = [("G1", 0, 0.20), ("N1", 1, 0.06)]
    assert sa.select_rows(q, cands2, ROWS, {"G1": 0.01, "N1": 0.02})[0] == ["G1", "N1"]
    # BM25 폴백이면 조문 점수는 척도가 달라 쓰지 않는다
    assert sa.select_rows(q, cands2, ROWS, {"G1": 0.01, "N1": 0.02}, fallback=True)[0] == []
    # 재순위 문턱 이상인 행이 검색 순위보다 먼저
    assert sa.select_rows(q, cands2, ROWS, {"G1": 0.01, "N1": 0.5})[0] == ["N1"]


def test_select_rows_lexical_when_rerank_unavailable():
    q = "서면경고를 받을 수 있나요?"
    cands = [("K1", 0, 9.0), ("S1", 1, 9.0)]
    ids, rel = sa.select_rows(q, cands, ROWS, None, fallback=True)
    assert ids == ["K1"] and rel["K1"] >= sa.LEXICAL_MIN > rel["S1"]
    assert sa.lexical_overlap("", "무엇이든") == 0.0


def test_cited_rows_relevant():
    cands = [("G1", 0, 0.01), ("N1", 1, 0.30)]
    rel = {"G1": 0.02, "N1": 0.0}
    assert not sa.cited_rows_relevant(["G1"], cands, rel, reranked=True, fallback=False)
    assert sa.cited_rows_relevant(["N1"], cands, rel, reranked=True, fallback=False)
    assert not sa.cited_rows_relevant(["N1"], cands, rel, reranked=True, fallback=True)
    assert sa.cited_rows_relevant(["G1"], cands, {"G1": 0.2}, reranked=False, fallback=True)
    assert not sa.cited_rows_relevant([], cands, rel, reranked=True, fallback=False)


def test_topic_terms_match_gpu_list():
    """앱의 TOPIC_TERMS 는 gpu/llm.py 의 목록과 같아야 한다(gpu/ 는 import 하지 않고 소스만 읽는다)."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "gpu" / "llm.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        target = getattr(node, "target", None) or (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "TOPIC_TERMS":
            assert ast.literal_eval(node.value) == sa.TOPIC_TERMS
            return
    raise AssertionError("gpu/llm.py 에 TOPIC_TERMS 가 없다")


def test_rows_irrelevant_question_not_found_without_llm(make_client, sgpu):
    sgpu.rerank_const = 0.001   # 조문·행 모두 질문과 무관
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c).json()
    assert d["mode"] == "not_found" and d["grounded"] is False and d["citations"] == []
    assert d["rows"] == [] and d["results"]
    assert sgpu.llm_calls("answer") == []
    # 행 재순위는 기존 /rerank 로(조문 검색 재순위 다음 호출), 행 글(path·measure·plain)을 보낸다
    assert len(sgpu.rerank_bodies) == 2 and all(b["query"] == Q for b in sgpu.rerank_bodies)


def test_rows_rerank_down_uses_lexical_and_answers(make_client, sgpu):
    sgpu.rerank_down = True
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c, q="소송 서면 송달 주소를 비공개로 할 수 있나요?").json()
    assert d["fallback"] is True and d["grounded"] is True and d["rows"] == ["J1"]
    _assert_citations_in_retrieved(d)


def test_rows_cited_irrelevant_row_is_not_found(make_client, sgpu, monkeypatch):
    # 방어선: 생성·발췌가 관련도 문턱 미만 행만 인용하면 내보내지 않는다.
    monkeypatch.setattr(sa, "cited_rows_relevant", lambda *a, **k: False)
    sgpu.answer_out = {"answer": "관련 제도는 다음과 같습니다.", "citations": ["J1"], "grounded": True,
                       "mode": "extractive"}
    with make_client(httpx.MockTransport(sgpu)) as c:
        d = _ask(c).json()
    assert d["mode"] == "not_found" and d["citations"] == [] and d["grounded"] is False


def test_quoted_name_must_appear_in_row():
    q = "가정폭력 피해자 전용 '안심 가상주소 발급제'는 어디서 신청하나요?"
    assert sa.quoted_names(q) == ["안심가상주소발급제"]
    assert sa.quoted_names("“신분 재설정 제도”는요? 「주소 봉인」도요") == ["신분재설정", "주소봉인"]
    row = _it("A1", "주민등록 등·초본", "열람·교부 제한", "주민센터에 신청")
    assert not sa.quoted_ok(q, row)
    assert sa.quoted_ok("'열람 · 교부 제한'은 어디서 하나요?", row)   # 공백 무시
    assert sa.quoted_ok("주민등록 등본을 막을 수 있나요?", row)          # 따옴표 이름 없음
    cands = [("A1", 0, 0.9)]
    items = {"A1": row}
    assert sa.select_rows(q, cands, items, {"A1": 0.9})[0] == []
