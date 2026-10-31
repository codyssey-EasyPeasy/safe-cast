"""운영 API (:8080). 진단·근거 질의·서류 점검.

- 사례 정보 비저장·비로깅(접근 로그는 메서드·경로·상태코드만).
- 외부 인터넷 호출 없음. GPU 서버(내부)만 호출.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import llm as _llm
from . import retrieval as _retrieval
from .engine import VICTIM_LABEL, VICTIM_TYPES, Engine, load_knowledge

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GPU_URL = "http://10.1.2.33:8000"

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

PLACEHOLDER_HTML = (
    '<!doctype html><html lang="ko"><meta charset="utf-8">'
    "<title>숨은집 지킴이</title>"
    "<body><h1>숨은집 지킴이</h1>"
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


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    item_ids: list[Annotated[str, Field(pattern=_llm.ITEM_ID_RE)]] = Field(
        min_length=1, max_length=8
    )


class RewriteSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int = Field(ge=0, le=4000)
    end: int = Field(ge=1, le=4000)
    label: str = Field(default="", max_length=20)


class RewriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    spans: list[RewriteSpan] = Field(default_factory=list, max_length=60)


LLM_UNAVAILABLE = "지금은 이 기능을 쓸 수 없습니다. 체크 항목으로 계속 진행할 수 있습니다."


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(transport=transport)
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
        locator = await _retrieval.intent(req.q, client, url)
        if locator:
            return {"refused": True, "message": REFUSAL_MESSAGE}
        results, fallback = await retriever.search(req.q, client, url, top_k=5)
        return {"refused": False, "results": results, "fallback": fallback}

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
            return {"available": True, "spans": data.get("spans") or []}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {"available": False}

    # --- 내부 LLM 기능(SPEC §8). 실패하면 503 → 화면은 체크박스 방식으로 동작. ---
    def _llm_down() -> HTTPException:
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
        if await _retrieval.intent(req.text, client, url, thresh=_llm.llm_guard_thresh()):
            return {"refused": True, "message": REFUSAL_MESSAGE}
        try:
            out = await _llm.intake(
                req.text,
                client=client,
                gpu_url=url,
                known_flags=known_flags,
                victim_types=VICTIM_TYPES,
            )
        except _llm.LLMUnavailable:
            raise _llm_down()
        return {"refused": False, **out}

    @app.post("/api/answer")
    async def answer_endpoint(
        req: AnswerRequest, request: Request, _: None = Depends(_require_access_code)
    ):
        client, url = await _llm_gate(request)
        if await _retrieval.intent(req.question, client, url, thresh=_llm.llm_guard_thresh()):
            return {"refused": True, "message": REFUSAL_MESSAGE}
        try:
            out = await _llm.answer(
                req.question,
                req.item_ids,
                client=client,
                gpu_url=url,
                items_by_id=items_by_id,
                articles=retriever.articles,
            )
        except _llm.LLMUnavailable:
            raise _llm_down()
        if out.get("refused"):
            return {"refused": True, "message": REFUSAL_MESSAGE}
        return {"refused": False, **out}

    @app.post("/api/rewrite")
    async def rewrite_endpoint(
        req: RewriteRequest, request: Request, _: None = Depends(_require_access_code)
    ):
        client, url = await _llm_gate(request)
        try:
            out = await _llm.rewrite(
                req.text,
                [s.model_dump() for s in req.spans],
                client=client,
                gpu_url=url,
            )
        except _llm.LLMUnavailable:
            raise _llm_down()
        if out.get("refused"):
            return {"refused": True, "message": REFUSAL_MESSAGE}
        return {"refused": False, **out}

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
