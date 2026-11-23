"""Local web server for the chat UI.

Locked to this machine by default. Because a browser will happily send requests
to localhost from any website, every API call also checks:
- the Host header (blocks DNS-rebinding attacks),
- the Origin / Sec-Fetch-Site headers (blocks other sites posting to it),
- an access token, required whenever it is bound to a non-loopback address.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..db import DatabaseError
from ..engine import MAX_QUESTION_CHARS, Assistant
from ..llm import LLMError
from ..safety import UnsafeQueryError
from ..util import is_loopback_host

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


def create_app(assistant: Assistant, *, host: str = "127.0.0.1", port: int = 8765,
               token: str | None = None) -> FastAPI:
    app = FastAPI(title="local-db-chat", docs_url=None, redoc_url=None, openapi_url=None)
    exposed = not is_loopback_host(host)
    if exposed and not token:
        token = secrets.token_urlsafe(24)
    app.state.token = token
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}",
                     "localhost", "127.0.0.1", "[::1]"}
    if exposed:
        allowed_hosts |= {host, f"{host}:{port}"}
    limiter = asyncio.Semaphore(MAX_CONCURRENT_QUESTIONS)

    @app.exception_handler(DatabaseError)
    async def _db_error(_request, exc: DatabaseError):
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.exception_handler(LLMError)
    async def _llm_error(_request, exc: LLMError):
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host_header = (request.headers.get("host") or "").lower()
        if host_header not in allowed_hosts and not exposed:
            return JSONResponse({"error": "Host not allowed."}, status_code=403)
        if request.url.path.startswith("/api/"):
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "Cross-site requests are not allowed."}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1].lower() not in allowed_hosts:
                return JSONResponse({"error": "Origin not allowed."}, status_code=403)
            if token:
                given = request.headers.get("x-ldbc-token", "")
                if not hmac.compare_digest(given, token):
                    return JSONResponse({"error": "Missing or wrong access token."}, status_code=401)
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

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/app.js")
    def script():
        return FileResponse(STATIC / "app.js", media_type="text/javascript")

    @app.get("/app.css")
    def style():
        return FileResponse(STATIC / "app.css", media_type="text/css")

    @app.post("/api/ask")
    async def ask(body: AskRequest):
        if limiter.locked():
            return JSONResponse({"error": "Busy answering other questions. Try again in a moment."},
                                status_code=429)
        async with limiter:
            # The model and the database calls block; run them off the event loop.
            answer = await asyncio.to_thread(
                assistant.ask, body.question, [t.model_dump() for t in body.history])
        return answer.to_dict()

    @app.post("/api/feedback")
    async def feedback(body: FeedbackRequest):
        try:
            if body.correct:
                await asyncio.to_thread(assistant.confirm, body.question, body.sql)
            else:
                await asyncio.to_thread(assistant.reject, body.question)
        except UnsafeQueryError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True}

    @app.get("/api/schema")
    async def schema():
        info = await asyncio.to_thread(lambda: assistant.schema)
        return {
            "dialect": info.dialect,
            "tables": [{"name": t.full_name, "kind": t.kind, "comment": t.comment,
                        "columns": [c.name for c in t.visible_columns]}
                       for t in info.tables.values()],
            "warnings": info.warnings,
        }

    @app.post("/api/refresh")
    async def refresh():
        info = await asyncio.to_thread(assistant.refresh_schema)
        return {"tables": len(info), "warnings": info.warnings}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    return app
