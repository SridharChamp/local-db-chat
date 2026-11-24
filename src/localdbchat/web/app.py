"""Local web server for the chat UI and the first-run setup screen.

Locked to this machine by default. Because a browser will happily send requests
to localhost from any website, every API call also checks:
- the Host header (blocks DNS-rebinding attacks),
- the Origin / Sec-Fetch-Site headers (blocks other sites posting to it),
- an access token, required whenever it is bound to a non-loopback address.
Changing the database connection is only allowed from this machine itself.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..config import Config
from ..db import DatabaseError
from ..engine import MAX_QUESTION_CHARS, Assistant
from ..llm import LLMError, OllamaClient
from ..safety import UnsafeQueryError
from ..setup import KINDS, ConnectionFields, SetupError, build_url, check_driver, save_database_url, try_connection
from ..util import is_loopback_host, redact_url

STATIC = Path(__file__).parent / "static"
MAX_BODY_BYTES = 256 * 1024
MAX_CONCURRENT_QUESTIONS = 4


class Turn(BaseModel):
    question: str = Field(max_length=MAX_QUESTION_CHARS)
    sql: str = Field(max_length=5000)


class AskRequest(BaseModel):
    question: str = Field(max_length=MAX_QUESTION_CHARS)
    history: list[Turn] = Field(default_factory=list, max_length=20)


class FeedbackRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    sql: str = Field(default="", max_length=20_000)
    correct: bool


class RunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    sql: str = Field(min_length=1, max_length=20_000)


class ExplainRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    sql: str = Field(min_length=1, max_length=20_000)
    columns: list[str] = Field(max_length=200)
    rows: list[list] = Field(max_length=1000)
    row_count: int = Field(ge=0)
    truncated: bool = False


class ConnectionRequest(BaseModel):
    kind: Literal["postgresql", "mysql", "mssql", "sqlite"]
    host: str = Field(default="", max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str = Field(default="", max_length=255)
    user: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=1024)
    path: str = Field(default="", max_length=4096)
    ssl: bool = False
    trust_certificate: bool = False


def create_app(assistant: Assistant | None, *, cfg: Config | None = None, config_path: str = "config.yaml",
               host: str = "127.0.0.1", port: int = 8765, token: str | None = None,
               assistant_factory: Callable[[Config], Assistant] = Assistant,
               llm_factory: Callable[[Config], OllamaClient] | None = None) -> FastAPI:
    cfg = cfg or (assistant.cfg if assistant else Config())
    app = FastAPI(title="local-db-chat", docs_url=None, redoc_url=None, openapi_url=None)
    exposed = not is_loopback_host(host)
    if exposed and not token:
        token = secrets.token_urlsafe(24)
    app.state.token = token
    app.state.assistant = assistant
    app.state.cfg = cfg
    app.state.pull = {"running": False, "status": "", "completed": 0, "total": 0, "error": "", "done": False}
    pull_lock = threading.Lock()
    swap_lock = threading.Lock()
    app.state.preload = {"state": "idle", "ms": 0, "tokens": 0, "error": ""}

    def start_preload(target: Assistant | None) -> None:
        """Have the model read the schema in the background, so questions start fast."""
        if target is None:
            return

        def run():
            import time as _time
            app.state.preload = {"state": "running", "ms": 0, "tokens": 0, "error": ""}
            t0 = _time.perf_counter()
            try:
                if not target.schema_preloadable:
                    app.state.preload = {"state": "too_big", "ms": 0, "tokens": 0, "error": ""}
                    return
                stats = target.preload()
                if app.state.assistant is target:  # ignore a run overtaken by a newer save
                    app.state.preload = {"state": "done", "ms": int((_time.perf_counter() - t0) * 1000),
                                         "tokens": int(stats.get("prompt_tokens", 0)), "error": ""}
            except (LLMError, DatabaseError) as exc:
                app.state.preload = {"state": "error", "ms": 0, "tokens": 0, "error": str(exc)}

        threading.Thread(target=run, name="schema-preload", daemon=True).start()

    app.state.start_preload = start_preload
    if assistant is not None and getattr(assistant, "preloaded", False):
        app.state.preload = {"state": "done", "ms": 0, "tokens": 0, "error": ""}
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}",
                     "localhost", "127.0.0.1", "[::1]"}
    limiter = asyncio.Semaphore(MAX_CONCURRENT_QUESTIONS)

    def make_llm() -> OllamaClient:
        if llm_factory:
            return llm_factory(app.state.cfg)
        c = app.state.cfg
        return OllamaClient(c.ollama_url, c.model, embed_model=c.embed_model, timeout=10,
                            allow_remote=c.allow_remote_llm)

    def current() -> Assistant:
        a = app.state.assistant
        if a is None:
            raise SetupError("No database is connected yet. Open the setup screen to connect one.")
        return a

    # ------------------------------------------------------------- middleware
    @app.exception_handler(DatabaseError)
    async def _db_error(_request, exc: DatabaseError):
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.exception_handler(LLMError)
    async def _llm_error(_request, exc: LLMError):
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.exception_handler(SetupError)
    async def _setup_error(_request, exc: SetupError):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host_header = (request.headers.get("host") or "").lower()
        if host_header not in allowed_hosts and not exposed:
            return JSONResponse({"error": "Host not allowed."}, status_code=403)
        if request.url.path.startswith("/api/"):
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "Cross-site requests are not allowed."}, status_code=403)
            origin = request.headers.get("origin")
            same_site = allowed_hosts | ({host_header} if exposed else set())
            if origin and origin.split("://", 1)[-1].lower() not in same_site:
                return JSONResponse({"error": "Origin not allowed."}, status_code=403)
            if token:
                given = request.headers.get("x-ldbc-token", "")
                if not hmac.compare_digest(given, token):
                    return JSONResponse({"error": "Missing or wrong access token."}, status_code=401)
            if request.url.path.startswith(("/api/setup/", "/api/model/")):
                client = request.client.host if request.client else ""
                if not is_loopback_host(client or "") and client != "testclient":
                    return JSONResponse({"error": "Setup can only be changed on the machine running "
                                                  "local-db-chat."}, status_code=403)
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
                return JSONResponse({"error": "Request too large."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        response.headers["Cache-Control"] = "no-store"
        return response

    # ---------------------------------------------------------------- static
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/app.js")
    def script():
        return FileResponse(STATIC / "app.js", media_type="text/javascript")

    @app.get("/app.css")
    def style():
        return FileResponse(STATIC / "app.css", media_type="text/css")

    # ---------------------------------------------------------------- status
    @app.get("/api/status")
    async def status():
        a = app.state.assistant
        c = app.state.cfg
        out = {"configured": a is not None, "database": None, "dialect": None, "tables": None,
               "model": c.model, "ollama_running": False, "model_installed": False, "ollama_error": "",
               "pull": dict(app.state.pull),
               "kinds": [{"kind": k, "label": v["label"], "port": v["port"]} for k, v in KINDS.items()]}
        if a is not None:
            out["database"] = redact_url(a.db.url)
            out["dialect"] = a.db.backend
        out["preload"] = dict(app.state.preload)

        def check_model():
            llm = make_llm()
            try:
                return llm.has_model()
            finally:
                llm.close()
        try:
            out["model_installed"] = await asyncio.to_thread(check_model)
            out["ollama_running"] = True
        except LLMError as exc:
            out["ollama_error"] = str(exc)
        return out

    # ----------------------------------------------------------------- setup
    def fields(body: ConnectionRequest) -> ConnectionFields:
        return ConnectionFields(**body.model_dump())

    @app.post("/api/setup/test")
    async def setup_test(body: ConnectionRequest):
        check_driver(body.kind)
        url = build_url(fields(body))
        result = await asyncio.to_thread(try_connection, url, app.state.cfg)
        return {"ok": result.ok, "message": result.message, "tables": result.tables}

    @app.post("/api/setup/save")
    async def setup_save(body: ConnectionRequest):
        check_driver(body.kind)
        url = build_url(fields(body))
        result = await asyncio.to_thread(try_connection, url, app.state.cfg)
        if not result.ok:
            return JSONResponse({"error": result.message}, status_code=400)

        def swap():
            with swap_lock:
                save_database_url(config_path, url)
                new_cfg = Config(**{**app.state.cfg.__dict__, "database_url": url})
                new_assistant = assistant_factory(new_cfg)
                new_assistant.refresh_schema()
                old = app.state.assistant
                app.state.cfg, app.state.assistant = new_cfg, new_assistant
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
                return len(new_assistant.schema)
        tables = await asyncio.to_thread(swap)
        start_preload(app.state.assistant)
        return {"ok": True, "message": f"Saved. {tables} tables ready.", "tables": tables}

    @app.post("/api/model/pull")
    async def model_pull():
        with pull_lock:
            if app.state.pull["running"]:
                return {"started": False, "pull": dict(app.state.pull)}
            app.state.pull = {"running": True, "status": "starting", "completed": 0, "total": 0,
                              "error": "", "done": False}

        def run():
            def progress(status_text, completed, total):
                app.state.pull.update(status=status_text, completed=completed, total=total)
            llm = None
            try:
                llm = make_llm()
                llm.pull(app.state.cfg.model, progress)
                app.state.pull.update(done=True, status="ready")
                start_preload(app.state.assistant)  # the model is here now: let it read the schema
            except LLMError as exc:
                app.state.pull.update(error=str(exc))
            finally:
                app.state.pull["running"] = False
                if llm is not None:
                    llm.close()

        threading.Thread(target=run, name="model-pull", daemon=True).start()
        return {"started": True, "pull": dict(app.state.pull)}

    # ------------------------------------------------------------------ chat
    @app.post("/api/ask")
    async def ask(body: AskRequest):
        a = current()
        if limiter.locked():
            return JSONResponse({"error": "Busy answering other questions. Try again in a moment."},
                                status_code=429)
        async with limiter:
            # The model and the database calls block; run them off the event loop. The written
            # sentence is fetched separately (/api/explain) so the table shows without waiting.
            answer = await asyncio.to_thread(a.ask, body.question, [t.model_dump() for t in body.history],
                                             explain=False)
        out = answer.to_dict()
        out["explain_pending"] = bool(a.cfg.explain_answers and answer.ok and answer.sql
                                      and answer.source != "schema")
        return out

    @app.post("/api/run")
    async def run(body: RunRequest):
        """Run SQL a person corrected ("Edit SQL"), through the same safety checks."""
        a = current()
        async with limiter:
            answer = await asyncio.to_thread(a.run_sql, body.question, body.sql)
        out = answer.to_dict()
        out["explain_pending"] = bool(a.cfg.explain_answers and answer.ok and answer.sql)
        return out

    @app.post("/api/explain")
    async def explain(body: ExplainRequest):
        a = current()
        async with limiter:
            text = await asyncio.to_thread(a.articulate, body.question, body.sql, body.columns, body.rows,
                                           body.row_count, body.truncated)
        return {"answer": text}

    @app.post("/api/feedback")
    async def feedback(body: FeedbackRequest):
        a = current()
        try:
            if body.correct:
                await asyncio.to_thread(a.confirm, body.question, body.sql)
            else:
                await asyncio.to_thread(a.reject, body.question)
        except UnsafeQueryError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True}

    @app.get("/api/schema")
    async def schema():
        a = current()
        info = await asyncio.to_thread(lambda: a.schema)
        return {
            "dialect": info.dialect,
            "tables": [{"name": t.full_name, "kind": t.kind, "comment": t.comment,
                        "columns": [c.name for c in t.visible_columns]}
                       for t in info.tables.values()],
            "warnings": info.warnings,
        }

    @app.post("/api/refresh")
    async def refresh():
        a = current()
        info = await asyncio.to_thread(a.refresh_schema)
        start_preload(a)  # the schema block changed: the model has to read the new one
        return {"tables": len(info), "warnings": info.warnings}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    return app
