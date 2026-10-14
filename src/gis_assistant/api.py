"""Starlette HTTP API for the GIS assistant engine.

Provides REST endpoints for the QGIS chat widget (and any other HTTP client).
Bearer token auth is enforced when GIS_API_TOKEN is set.
"""

from __future__ import annotations

import json
import logging
import os
import secrets

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .engine import GISEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("gis_assistant.api")

_engine = GISEngine()
_api_token: str | None = None


# -- Auth --------------------------------------------------------------------

def _check_auth(request: Request) -> JSONResponse | None:
    """Return an error response if auth fails, None if OK."""
    if _api_token is None:
        return None
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {_api_token}":
        return JSONResponse(
            {"status": "error", "message": "unauthorized"},
            status_code=401,
        )
    return None


def _error(message: str, details: dict | None = None, status_code: int = 400) -> JSONResponse:
    """Build a consistent error envelope."""
    body: dict = {"status": "error", "message": message}
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status_code)


# -- Endpoints ---------------------------------------------------------------

async def health_endpoint(request: Request) -> JSONResponse:
    """Health check — no auth required."""
    return JSONResponse({"status": "ok"})


async def qgis_status_endpoint(request: Request) -> JSONResponse:
    """Check QGIS socket reachability."""
    if err := _check_auth(request):
        return err
    result = await anyio.to_thread.run_sync(_engine.qgis_status)
    return JSONResponse(result)


async def plan_endpoint(request: Request) -> JSONResponse:
    if err := _check_auth(request):
        return err
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error("invalid_json")
    if "prompt" not in body:
        return _error("missing_field", {"field": "prompt"})
    try:
        result = await anyio.to_thread.run_sync(lambda: _engine.plan(body["prompt"]))
    except Exception as e:
        logger.exception("plan failed")
        return _error(str(e), status_code=500)
    return JSONResponse(result)


async def answer_endpoint(request: Request) -> JSONResponse:
    if err := _check_auth(request):
        return err
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error("invalid_json")
    if "answers" not in body:
        return _error("missing_field", {"field": "answers"})
    try:
        result = await anyio.to_thread.run_sync(lambda: _engine.answer(body["answers"]))
    except Exception as e:
        logger.exception("answer failed")
        return _error(str(e), status_code=500)
    return JSONResponse(result)


async def run_endpoint(request: Request) -> JSONResponse:
    if err := _check_auth(request):
        return err
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error("invalid_json")
    if "prompt" not in body:
        return _error("missing_field", {"field": "prompt"})
    try:
        result = await anyio.to_thread.run_sync(lambda: _engine.run(body["prompt"]))
    except Exception as e:
        logger.exception("run failed")
        return _error(str(e), status_code=500)
    return JSONResponse(result)


async def run_plan_endpoint(request: Request) -> JSONResponse:
    if err := _check_auth(request):
        return err
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error("invalid_json")
    if "plan" not in body:
        return _error("missing_field", {"field": "plan"})
    try:
        result = await anyio.to_thread.run_sync(lambda: _engine.run_plan(body["plan"]))
    except Exception as e:
        logger.exception("run_plan failed")
        return _error(str(e), status_code=500)
    return JSONResponse(result)


async def resume_endpoint(request: Request) -> JSONResponse:
    if err := _check_auth(request):
        return err
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error("invalid_json")
    if "token" not in body:
        return _error("missing_field", {"field": "token"})
    if "approved" not in body:
        return _error("missing_field", {"field": "approved"})
    try:
        result = await anyio.to_thread.run_sync(
            lambda: _engine.resume(body["token"], body["approved"])
        )
    except ValueError as e:
        return _error(str(e), status_code=404)
    except Exception as e:
        logger.exception("resume failed")
        return _error(str(e), status_code=500)
    return JSONResponse(result)


async def diagnose_endpoint(request: Request) -> JSONResponse:
    if err := _check_auth(request):
        return err
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error("invalid_json")
    if "error_text" not in body:
        return _error("missing_field", {"field": "error_text"})
    try:
        result = await anyio.to_thread.run_sync(
            lambda: _engine.diagnose(body["error_text"])
        )
    except Exception as e:
        logger.exception("diagnose failed")
        return _error(str(e), status_code=500)
    return JSONResponse(result)


async def capabilities_endpoint(request: Request) -> JSONResponse:
    if err := _check_auth(request):
        return err
    verbose = request.query_params.get("verbose", "").lower() in ("true", "1")
    result = _engine.list_capabilities(verbose)
    return JSONResponse(result)


async def last_run_endpoint(request: Request) -> JSONResponse:
    if err := _check_auth(request):
        return err
    result = _engine.explain_last_run()
    return JSONResponse(result)


# -- App setup ---------------------------------------------------------------

def _build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/health", health_endpoint, methods=["GET"]),
            Route("/qgis_status", qgis_status_endpoint, methods=["GET"]),
            Route("/plan", plan_endpoint, methods=["POST"]),
            Route("/answer", answer_endpoint, methods=["POST"]),
            Route("/run", run_endpoint, methods=["POST"]),
            Route("/run_plan", run_plan_endpoint, methods=["POST"]),
            Route("/resume", resume_endpoint, methods=["POST"]),
            Route("/diagnose", diagnose_endpoint, methods=["POST"]),
            Route("/capabilities", capabilities_endpoint, methods=["GET"]),
            Route("/last_run", last_run_endpoint, methods=["GET"]),
        ],
    )


def main():
    """Run the HTTP API server."""
    global _api_token

    port = int(os.environ.get("GIS_API_PORT", "8100"))
    _api_token = os.environ.get("GIS_API_TOKEN")
    if not _api_token:
        _api_token = secrets.token_urlsafe(32)

    # Print token on first line so plugin (QProcess) can capture it
    print(f"GIS_API_TOKEN={_api_token}", flush=True)
    logger.info("Starting HTTP API on 127.0.0.1:%d", port)

    _engine.startup()

    app = _build_app()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    finally:
        _engine.shutdown()


if __name__ == "__main__":
    main()
