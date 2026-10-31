"""운영 API (:8080). 진단·근거 질의·서류 점검.

- 사례 정보 비저장·비로깅(접근 로그는 메서드·경로·상태코드만).
- 외부 인터넷 호출 없음. GPU 서버(내부)만 호출.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.base import BaseHTTPMiddleware

from . import guard as _guard
from . import humanize as _hz
from . import llm as _llm
from . import offsets as _offsets
from . import retrieval as _retrieval
from . import search_answer as _sa
from .engine import VICTIM_LABEL, VICTIM_TYPES, Engine, load_knowledge

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GPU_URL = "http://10.1.2.33:8000"
# GPU API 공유 토큰(X-Internal-Token). GPU :8000 은 ACG 를 바꿀 수 없어 앱 수준에서 막는다.
DEFAULT_GPU_TOKEN_FILE = "~/.config/sumeunjip/gpu_token"
GPU_TOKEN_HEADER = "X-Internal-Token"


def gpu_auth_headers() -> dict[str, str]:
    """GPU_TOKEN_FILE(기본 ~/.config/sumeunjip/gpu_token)의 토큰을 헤더로. 파일이 없으면 빈 dict
    (GPU 가 401 → 각 기능은 기존 실패 경로대로 체크박스 방식으로 동작). 토큰은 로그하지 않는다."""
    path = Path(os.path.expanduser(os.environ.get("GPU_TOKEN_FILE") or DEFAULT_GPU_TOKEN_FILE))
    try:
        tok = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    return {GPU_TOKEN_HEADER: tok} if tok else {}

# 접근 로그: 메서드·경로·상태코드 한 줄만. 본문·쿼리스트링은 절대 남기지 않는다.
logger = logging.getLogger("sumeunjip.access")
logger.setLevel(logging.INFO)
if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

REFUSAL_MESSAGE = (
    "이 도구는 사람이나 주소를 찾는 용도로는 쓸 수 없습니다. "
    "피해자를 보호하는 제도에 대해서만 안내합니다. "
    "긴급한 경우 112, 1366으로 연락하세요."
)
# 2단 가드(2026-10-31 오너 결정): 거절은 분명한 타인 소재 탐색만, 거절에도 보호 대안을 붙인다.
THIRD_PARTY_MESSAGE = (
    "이 도구는 다른 사람의 주소·위치·연락처를 찾는 용도로는 쓸 수 없습니다. "
    "대신 내 정보와 안전을 지키는 데 쓸 수 있는 제도를 아래에 안내합니다. "
    "혼자 하기 어려우면 1366(24시간)·법률구조공단 132, 위험이 급하면 112에 연락하세요."
)
# 보호 안내(_guard.needs_notice 가 True 인 글)는 거절하지 않고 이 안내와 보호 제도를 함께 보여 준다.
PROTECTIVE_FRAMING_BASE = "이 서비스는 다른 사람을 찾는 데는 쓸 수 없고, 내 정보와 안전을 지키는 제도만 안내합니다."
PROTECTIVE_CONTACTS = "혼자 하기 어려우면 1366(24시간)·법률구조공단 132, 위험이 급하면 112에 연락하세요."
PROTECTIVE_FRAMING = PROTECTIVE_FRAMING_BASE + " " + PROTECTIVE_CONTACTS
REPHRASE_MESSAGE = (
    "초성이나 줄임말만으로는 뜻을 정확히 알기 어렵습니다. 하고 싶은 말을 문장으로 다시 적어 주세요. "
    "급하면 1366(24시간)이나 112에 바로 연락하세요."
)

PLACEHOLDER_HTML = (
    '<!doctype html><html lang="ko"><meta charset="utf-8">'
    "<title>보안 예보</title>"
    "<body><h1>보안 예보</h1>"
    "<p>프런트엔드가 아직 준비되지 않았습니다.</p></body></html>"
)


class DiagnoseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    victim_type: Literal["family", "stalking", "dating", "sexual"]
    flags: list[str] = Field(default_factory=list, max_length=60)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(min_length=1, max_length=500)


class DocCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=20000)


# 다턴 이력(브라우저가 메모리에만 들고 보낸다. 서버는 저장·로깅하지 않는다: 무상태).
HISTORY_MAX_TURNS = 6
HISTORY_TURN_CHARS = 500
HISTORY_TOTAL_CHARS = 3000


class HistoryTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    text: str = Field(max_length=HISTORY_TURN_CHARS)


class _WithHistory(BaseModel):
    history: list[HistoryTurn] = Field(default_factory=list, max_length=HISTORY_MAX_TURNS)

    @model_validator(mode="after")
    def _history_total(self):
        if sum(len(t.text) for t in self.history) > HISTORY_TOTAL_CHARS:
            raise ValueError("history too long")
        return self

    def user_turns(self) -> list[str]:
        return [t.text for t in self.history if t.role == "user" and t.text.strip()]

    def assistant_turns(self) -> list[str]:
        return [t.text for t in self.history if t.role == "assistant" and t.text.strip()]


class IntakeRequest(_WithHistory):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)


class AnswerRequest(_WithHistory):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    item_ids: list[Annotated[str, Field(pattern=_llm.ITEM_ID_RE)]] = Field(
        min_length=1, max_length=8
    )


class SearchAnswerRequest(_WithHistory):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(min_length=1, max_length=500)


class RewriteSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int = Field(ge=0, le=4000)
    end: int = Field(ge=1, le=4000)
    label: str = Field(default="", max_length=20)


# 문장 바꾸기 입력 상한. GPU 생성 시간 초과(LLM_TIMEOUT 60초) 안에 여유 있게 끝나는 길이.
# 2026-10-31 실측(GPU /llm/rewrite 직접, 합성 서류 이어 붙임, 1회씩): 500자 22.5초·1000자 22.1초·
# 1500자 38.9초·2000자 42.0초. 4000자는 60초 초과 → 503(재시도 포함 120초)이었다.
REWRITE_MAX_CHARS = 1500


class RewriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=REWRITE_MAX_CHARS)
    spans: list[RewriteSpan] = Field(default_factory=list, max_length=60)


LLM_UNAVAILABLE = "지금은 이 기능을 쓸 수 없습니다. 체크 항목으로 계속 진행할 수 있습니다."
LLM_TIMEOUT_MSG = "시간이 너무 오래 걸려 멈췄습니다. 글을 더 짧게 나누어 다시 해 주세요."
LLM_BUSY_MSG = "다른 요청을 처리하고 있습니다. 잠시 뒤 다시 해 주세요."
# 문장 바꾸기 동시 처리 상한: 전체 2건, 접속 주소당 1건(긴 생성이 GPU 를 오래 붙잡는다).
REWRITE_GLOBAL_LIMIT = 2


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        try:
            logger.info(f"{request.method} {request.url.path} {response.status_code}")
        except Exception:
            pass
        return response


def _require_access_code(request: Request) -> None:
    """접근 코드는 기본 비활성(SPEC §7: 피해자 본인이 직접 쓴다).

    `REQUIRE_ACCESS_CODE=1` 일 때만 `ACCESS_CODE` 와 `X-Access-Code` 를 비교한다.
    그때 ACCESS_CODE 가 비어 있으면 fail-closed 로 막는다.
    """
    if os.environ.get("REQUIRE_ACCESS_CODE", "").strip() != "1":
        return
    code = os.environ.get("ACCESS_CODE", "")
    provided = request.headers.get("X-Access-Code", "")
    if not code or not hmac.compare_digest(code, provided):
        raise HTTPException(status_code=401, detail="접근 코드가 필요합니다")


def create_app(
    knowledge_path: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    knowledge = load_knowledge(knowledge_path)
    engine = Engine(knowledge)
    retriever = _retrieval.Retriever()
    gpu_url = os.environ.get("GPU_URL", DEFAULT_GPU_URL).rstrip("/")
    items_by_id = {it.get("id"): it for it in engine.items if it.get("id")}
    known_flags = set(engine.flag_ids())
    probe = _llm.FeatureProbe()
    rewrite_busy: dict[str, int] = {}
    item_names = _hz.name_map(list(items_by_id.values()))
    # 검색 조문 → 그 조문을 근거로 드는 지식표 행(검색 조문 종합 답변 'rows' 방식).
    row_index = _sa.build_row_index(engine.items)

    def _alternatives(text: str) -> list[dict[str, str]]:
        """질문 낱말에 맞는 보호 제도 2~3개(이름·막는 제도). 지식표에 없는 id 는 건너뛴다."""
        def usable(iid: str) -> bool:
            return iid in items_by_id and items_by_id[iid].get("status") != "none"

        ids = [i for i in _guard.protective_items(text) if usable(i)]
        # 지식표에 빠진 id 가 있으면 기본 세트·다른 시행 중 항목으로 2개까지 채운다.
        for iid in [*_guard.DEFAULT_PROTECTIVE, *items_by_id]:
            if len(ids) >= 2:
                break
            if usable(iid) and iid not in ids:
                ids.append(iid)
        out = []
        for iid in ids:
            it = items_by_id[iid]
            out.append({"id": iid, "label": item_names.get(iid, iid),
                        "measure": str(it.get("measure") or "")[:200]})
        return out[:3]

    def _refusal(text: str) -> dict:
        return {"refused": True, "tier": _guard.THIRD_PARTY, "message": THIRD_PARTY_MESSAGE,
                "alternatives": _alternatives(text)}

    def _rephrase() -> dict:
        return {"refused": False, "rephrase": True, "tier": _guard.REPHRASE,
                "message": REPHRASE_MESSAGE}

    async def _assess(client: httpx.AsyncClient, url: str, text: str,
                      history_user: list[str] | None = None) -> dict:
        """입력 가드(2단). 임베딩 점수는 GPU /intent 에서 신호로만 받는다(실패하면 규칙만)."""
        score: float | None = None
        try:
            r = await client.post(f"{url}/intent", json={"q": text}, timeout=10.0)
            r.raise_for_status()
            score = float(r.json().get("score"))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            score = None
        a = _guard.assess_turns(text, history_user, score)
        # 판정 근거만 남긴다(사용자 글은 남기지 않는다).
        logger.info(f"guard tier={a['tier']} reason={a.get('reason')}")
        return a

    def _framing(text: str, body: str = "") -> dict:
        """보호 안내. 답 본문에 이미 1366·132 안내가 있으면 연락처 문장을 빼고 contacts=False 로 알린다."""
        dup = bool(re.search(r"(?<!\d)(?:1366|132)(?!\d)", body))
        return {"message": PROTECTIVE_FRAMING_BASE if dup else PROTECTIVE_FRAMING,
                "alternatives": _alternatives(text), "contacts": not dup}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # GPU 만 부르는 클라이언트라 기본 헤더로 토큰을 싣는다(main·llm·retrieval 의 모든 GPU 호출).
        client = httpx.AsyncClient(transport=transport, headers=gpu_auth_headers())
        app.state.client = client
        app.state.retriever = retriever
        app.state.engine = engine
        app.state.gpu_url = gpu_url

        async def _bg_embed() -> None:
            try:
                await retriever.ensure_embedded(client, gpu_url)
            except Exception:
                pass

        task = asyncio.create_task(_bg_embed())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            await client.aclose()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        # 요청 입력을 되비추지 않는다.
        return JSONResponse(
            status_code=422, content={"detail": "요청 형식이 올바르지 않습니다"}
        )

    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "items": len(engine.items),
            "version": engine.version,
            "embedded": bool(retriever.embedded),
        }

    @app.get("/api/flags")
    async def flags_endpoint(request: Request, _: None = Depends(_require_access_code)):
        return {
            "flags": engine.flags,
            "victim_types": [
                {"id": vt, "label": VICTIM_LABEL[vt]} for vt in VICTIM_TYPES
            ],
        }

    @app.post("/api/diagnose")
    async def diagnose(
        req: DiagnoseRequest,
        request: Request,
        _: None = Depends(_require_access_code),
    ):
        unknown = engine.unknown_flags(req.flags)
        if unknown:
            raise HTTPException(
                status_code=422, detail="알 수 없는 상황 항목이 포함되어 있습니다"
            )
        return engine.diagnose(req.victim_type, req.flags)

    @app.post("/api/search")
    async def search(
        req: SearchRequest,
        request: Request,
        _: None = Depends(_require_access_code),
    ):
        client: httpx.AsyncClient = request.app.state.client
        url: str = request.app.state.gpu_url
        a = await _assess(client, url, req.q)
        if a["tier"] == _guard.THIRD_PARTY:
            return _refusal(req.q)
        if a["tier"] == _guard.REPHRASE:
            return {**_rephrase(), "results": [], "fallback": False}
        results, fallback = await retriever.search(req.q, client, url, top_k=5)
        out = {"refused": False, "tier": a["tier"], "results": results, "fallback": fallback}
        if _guard.needs_notice(a["tier"]):
            out["protective"] = _framing(req.q)
        return out

    @app.post("/api/doccheck")
    async def doccheck(
        req: DocCheckRequest,
        request: Request,
        _: None = Depends(_require_access_code),
    ):
        client: httpx.AsyncClient = request.app.state.client
        url: str = request.app.state.gpu_url
        try:
            r = await client.post(f"{url}/ner", json={"text": req.text}, timeout=10.0)
            r.raise_for_status()
            data = r.json()
            spans = data.get("spans") or []
            if not isinstance(spans, list):
                raise TypeError("spans")
            # GPU 는 파이썬 코드 포인트 위치를 준다 → 브라우저 문자열 위치(UTF-16)로 바꿔 돌려준다(P0-04).
            return {"available": True, "offset_unit": _offsets.OFFSET_UNIT,
                    "spans": _offsets.spans_to_client(req.text, spans)}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return {"available": False, "offset_unit": _offsets.OFFSET_UNIT}

    # --- 내부 LLM 기능(SPEC §8). 실패하면 503 → 화면은 체크박스 방식으로 동작. ---
    def _llm_down(e: _llm.LLMUnavailable) -> HTTPException:
        # 요청 하나의 실패(시간 초과·형식 오류)로 모든 사용자의 LLM 기능을 끄지 않는다.
        # GPU 에 닿지 않은 경우(연결 실패)만 기능 표시를 내린다.
        if e.timeout:
            return HTTPException(status_code=504, detail=LLM_TIMEOUT_MSG)
        if e.down:
            probe.mark_down()
        return HTTPException(status_code=503, detail=LLM_UNAVAILABLE)

    async def _llm_gate(request: Request) -> tuple[httpx.AsyncClient, str]:
        client: httpx.AsyncClient = request.app.state.client
        url: str = request.app.state.gpu_url
        if _llm.llm_disabled_by_env():
            raise HTTPException(status_code=503, detail=LLM_UNAVAILABLE)
        return client, url

    @app.get("/api/features")
    async def features(request: Request, _: None = Depends(_require_access_code)):
        client: httpx.AsyncClient = request.app.state.client
        return {"llm": await probe.llm(client, request.app.state.gpu_url)}

    @app.post("/api/intake")
    async def intake_endpoint(
        req: IntakeRequest, request: Request, _: None = Depends(_require_access_code)
    ):
        client, url = await _llm_gate(request)
        users = req.user_turns()
        a = await _assess(client, url, req.text, users)
        if a["tier"] == _guard.THIRD_PARTY:
            return _refusal(req.text)
        if a["tier"] == _guard.REPHRASE:
            return _rephrase()
        # 상황 정리는 최근 사용자 턴을 함께 본다(이전 턴에 말한 상황도 체크 항목에 반영).
        joined = "\n".join([*users, req.text])[-3000:]
        try:
            out = await _llm.intake(
                joined,
                client=client,
                gpu_url=url,
                known_flags=known_flags,
                victim_types=VICTIM_TYPES,
                asked=req.assistant_turns(),
            )
        except _llm.LLMUnavailable as e:
            raise _llm_down(e)
        res = {"refused": False, "tier": a["tier"], **out}
        if _guard.needs_notice(a["tier"]):
            res["protective"] = _framing(req.text)
        return res

    @app.post("/api/answer")
    async def answer_endpoint(
        req: AnswerRequest, request: Request, _: None = Depends(_require_access_code)
    ):
        client, url = await _llm_gate(request)
        a = await _assess(client, url, req.question, req.user_turns())
        if a["tier"] == _guard.THIRD_PARTY:
            return _refusal(req.question)
        if a["tier"] == _guard.REPHRASE:
            return _rephrase()
        try:
            out = await _llm.answer(
                req.question,
                req.item_ids,
                client=client,
                gpu_url=url,
                items_by_id=items_by_id,
                articles=retriever.articles,
            )
        except _llm.LLMUnavailable as e:
            raise _llm_down(e)
        if out.get("refused"):
            # 생성문 가드에 걸린 경우도 거절만 하지 않고 보호 대안을 붙인다.
            return {**_refusal(req.question), "message": REFUSAL_MESSAGE}
        res = {"refused": False, "tier": a["tier"], **out}
        if _guard.needs_notice(a["tier"]):
            res["protective"] = _framing(req.question, str(out.get("answer") or ""))
        return res

    @app.post("/api/search_answer")
    async def search_answer_endpoint(
        req: SearchAnswerRequest,
        request: Request,
        strategy: Literal["rows", "articles"] = "rows",
        _: None = Depends(_require_access_code),
    ):
        """검색 조문 종합 답변: /api/search 와 같은 입력 가드 → 조문 5개 검색 → 그 조문만 근거로 답.

        LLM 을 쓸 수 없으면(꺼짐·GPU 실패) 503 대신 검색 결과만 주고 answer=null·mode='unavailable'.
        """
        client: httpx.AsyncClient = request.app.state.client
        url: str = request.app.state.gpu_url
        a = await _assess(client, url, req.q, req.user_turns())
        if a["tier"] == _guard.THIRD_PARTY:
            return {**_refusal(req.q), "results": [], "fallback": False}
        if a["tier"] == _guard.REPHRASE:
            return {**_rephrase(), "results": [], "fallback": False}
        results, fallback = await retriever.search(req.q, client, url, top_k=_sa.TOP_K)
        base = {"refused": False, "tier": a["tier"], "strategy": strategy,
                "results": results, "fallback": fallback}
        if _llm.llm_disabled_by_env():
            out = {"answer": None, "citations": [], "grounded": False, "ai_generated": False,
                   "mode": "unavailable", "message": LLM_UNAVAILABLE}
        else:
            try:
                out = await _sa.synthesize(strategy, req.q, results, client=client, gpu_url=url,
                                           items_by_id=items_by_id, row_index=row_index)
            except _llm.LLMUnavailable as e:
                if e.down:
                    probe.mark_down()
                out = {"answer": None, "citations": [], "grounded": False, "ai_generated": False,
                       "mode": "unavailable",
                       "message": LLM_TIMEOUT_MSG if e.timeout else LLM_UNAVAILABLE}
        if out.get("refused"):
            # 생성문 가드에 걸리면 /api/answer 와 같이 거절 + 보호 대안. 검색 결과도 내보내지 않는다.
            return {**_refusal(req.q), "message": REFUSAL_MESSAGE, "results": [], "fallback": fallback}
        res = {**base, **out}
        if _guard.needs_notice(a["tier"]):
            res["protective"] = _framing(req.q, str(out.get("answer") or ""))
        return res

    @app.post("/api/rewrite")
    async def rewrite_endpoint(
        req: RewriteRequest, request: Request, _: None = Depends(_require_access_code)
    ):
        client, url = await _llm_gate(request)
        who = request.client.host if request.client else "-"
        if rewrite_busy.get(who, 0) >= 1 or sum(rewrite_busy.values()) >= REWRITE_GLOBAL_LIMIT:
            raise HTTPException(status_code=429, detail=LLM_BUSY_MSG)
        rewrite_busy[who] = rewrite_busy.get(who, 0) + 1
        try:
            out = await _llm.rewrite(
                req.text,
                # 브라우저 span 은 UTF-16 위치 → 코드 포인트로 바꿔 GPU 로 넘긴다(P0-04).
                _offsets.spans_from_client(req.text, [s.model_dump() for s in req.spans]),
                client=client,
                gpu_url=url,
            )
        except _llm.LLMUnavailable as e:
            raise _llm_down(e)
        finally:
            rewrite_busy[who] -= 1
            if rewrite_busy[who] <= 0:
                rewrite_busy.pop(who, None)
        if out.get("refused"):
            return {"refused": True, "message": REFUSAL_MESSAGE, "alternatives": _alternatives(req.text),
                    "offset_unit": _offsets.OFFSET_UNIT}
        return {"refused": False, **out, "offset_unit": _offsets.OFFSET_UNIT}

    static_dir = REPO_ROOT / "web" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index():
        index_file = REPO_ROOT / "web" / "index.html"
        if index_file.is_file():
            return FileResponse(str(index_file))
        return HTMLResponse(PLACEHOLDER_HTML)

    return app


app = create_app()
