"""FastMCP server exposing gis.plan, gis.run, gis.diagnose, and more.

Thin wrappers around GISEngine — all logic lives in engine.py.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP, Context

from .engine import GISEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("gis_assistant.server")

# -- Global Engine -----------------------------------------------------------

_engine: GISEngine | None = None


def _get_engine() -> GISEngine:
    global _engine
    if _engine is None:
        _engine = GISEngine()
    return _engine


# -- Lifespan ----------------------------------------------------------------

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    logger.info("gis_assistant server starting up")
    _get_engine().startup()
    try:
        yield {}
    finally:
        _get_engine().shutdown()
        logger.info("gis_assistant server shut down")


# -- FastMCP -----------------------------------------------------------------

mcp = FastMCP(
    "gis_assistant",
    instructions=(
        "AI-powered GIS workflow automation. Plans and executes QGIS "
        "processing workflows from natural language."
    ),
    lifespan=server_lifespan,
)


@mcp.tool(name="gis.plan")
def gis_plan(ctx: Context, prompt: str) -> str:
    """Generate a GIS workflow plan from a natural-language prompt.

    Returns a JSON plan with inputs, outputs, assumptions, and ordered steps.
    Does NOT execute anything.

    If the plan has status "needs_clarification", use gis.answer() to provide
    answers and get an updated plan.
    """
    return json.dumps(_get_engine().plan(prompt), indent=2)


@mcp.tool(name="gis.answer")
def gis_answer(ctx: Context, answers_json: str) -> str:
    """Resolve clarification questions from a previous gis.plan call.

    Takes a JSON string of {question_id: answer_value} and re-plans with
    the answers merged into the original context. Returns an updated plan.
    """
    answers = json.loads(answers_json)
    return json.dumps(_get_engine().answer(answers), indent=2)


@mcp.tool(name="gis.run")
def gis_run(ctx: Context, prompt: str) -> str:
    """Plan AND execute a GIS workflow from a natural-language prompt.

    Steps: gather QGIS context -> generate plan -> validate -> execute.

    If the plan needs clarification, returns the questions (no execution).
    If a confirm step is reached, returns a blocked report with a
    resume_token -- use gis.resume() to continue.
    """
    return json.dumps(_get_engine().run(prompt), indent=2)


@mcp.tool(name="gis.run_plan")
def gis_run_plan(ctx: Context, plan_json: str) -> str:
    """Execute a specific plan JSON (e.g. from gis.plan, possibly edited).

    Use this for the plan -> review -> edit -> execute workflow.
    """
    plan_dict = json.loads(plan_json)
    return json.dumps(_get_engine().run_plan(plan_dict), indent=2)


@mcp.tool(name="gis.resume")
def gis_resume(ctx: Context, resume_token: str, approved: bool) -> str:
    """Resume a blocked execution after a confirm step.

    Args:
        resume_token: The token from the blocked RunReport.
        approved: True to proceed past the confirmation, False to abort.
    """
    return json.dumps(_get_engine().resume(resume_token, approved), indent=2)


@mcp.tool(name="gis.diagnose")
def gis_diagnose(ctx: Context, error_text: str) -> str:
    """Diagnose a GIS/QGIS error and suggest fixes.

    Provide the error message or traceback. Returns an explanation with
    likely cause, affected step, and proposed fixes.
    """
    return json.dumps(_get_engine().diagnose(error_text), indent=2)


@mcp.tool(name="gis.explain_last_run")
def gis_explain_last_run(ctx: Context) -> str:
    """Show details of the last plan and/or execution run.

    Returns the plan (inputs, outputs, assumptions, steps) and execution
    results if gis.run was used.
    """
    return json.dumps(_get_engine().explain_last_run(), indent=2)


@mcp.tool(name="gis.list_capabilities")
def gis_list_capabilities(ctx: Context, verbose: bool = False) -> str:
    """List supported operations, algorithm categories, and workspace policy.

    Args:
        verbose: If False (default), show categories with top examples.
                 If True, list all supported algorithm IDs.
    """
    return json.dumps(_get_engine().list_capabilities(verbose), indent=2)


# -- Entry Point -------------------------------------------------------------

def main():
    """Run the MCP server on stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
