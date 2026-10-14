"""Core GIS engine — shared logic for MCP and HTTP API transports."""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from .executor import PlanExecutor
from .planner import GISPlanner
from .prompts import DIAGNOSE_SYSTEM_PROMPT, DIAGNOSE_USER_TEMPLATE
from .qgis_client import QGISClient
from .safety import ALLOWED_ALGORITHMS, validate_plan_size
from .schemas import GISPlan, RunReport

logger = logging.getLogger("gis_assistant.engine")

DEFAULT_WORKSPACE = os.environ.get(
    "GIS_WORKSPACE",
    os.path.expanduser("~/gis-workspace"),
)


class GISEngine:
    """Core engine that plans, validates, and executes GIS workflows.

    All public methods return dicts.  JSON serialization happens at the
    transport boundary (MCP wrapper / HTTP endpoint).

    Thread safety: a narrow lock protects shared mutable state
    (_last_plan, _last_report, _pending_prompt, _pending_context).
    Network calls (OpenAI, QGIS socket) run outside the lock.
    """

    def __init__(self, workspace_dir: str | None = None):
        self._client: QGISClient | None = None
        self._planner: GISPlanner | None = None
        self._executor: PlanExecutor | None = None
        self._last_report: RunReport | None = None
        self._last_plan: GISPlan | None = None
        self._pending_prompt: str | None = None
        self._pending_context: dict | None = None
        self.workspace_dir = workspace_dir or DEFAULT_WORKSPACE
        self._lock = threading.Lock()

    # -- Lazy init helpers ---------------------------------------------------

    def _get_client(self) -> QGISClient:
        if self._client is None:
            self._client = QGISClient()
            if not self._client.connect():
                self._client = None
                raise ConnectionError(
                    "Cannot connect to QGIS. Ensure the QGIS MCP plugin is running."
                )
        else:
            self._client.ensure_connected()
        return self._client

    def _get_planner(self) -> GISPlanner:
        if self._planner is None:
            self._planner = GISPlanner()
        return self._planner

    def _get_executor(self) -> PlanExecutor:
        if self._executor is None:
            self._executor = PlanExecutor(
                self._get_client(), workspace_dir=self.workspace_dir
            )
        return self._executor

    def _gather_context(self) -> dict:
        """Gather current QGIS state for the planner.

        If the QGIS socket is not reachable, returns partial context
        with error info rather than raising.
        """
        context: dict[str, Any] = {"workspace_dir": self.workspace_dir}
        try:
            client = self._get_client()
        except Exception as e:
            logger.warning("Cannot connect to QGIS for context: %s", e)
            context["qgis_info"] = {"error": str(e)}
            context["project"] = {"error": "QGIS not connected"}
            context["layers"] = {"error": "QGIS not connected"}
            return context

        try:
            info = client.get_qgis_info()
            context["qgis_info"] = info.get("result", info)
        except Exception as e:
            context["qgis_info"] = {"error": str(e)}
        try:
            proj = client.get_project_info()
            context["project"] = proj.get("result", proj)
        except Exception as e:
            context["project"] = {"error": str(e)}
        try:
            layers = client.get_layers()
            context["layers"] = layers.get("result", layers)
        except Exception as e:
            context["layers"] = {"error": str(e)}
        return context

    # -- Public API ----------------------------------------------------------

    def plan(self, prompt: str) -> dict:
        """Generate a GIS workflow plan from a natural-language prompt."""
        context = self._gather_context()
        planner = self._get_planner()
        plan = planner.generate_plan(prompt, context)
        validate_plan_size(len(plan.steps))

        with self._lock:
            self._last_plan = plan
            if plan.status == "needs_clarification":
                self._pending_prompt = prompt
                self._pending_context = context

        return plan.model_dump()

    def answer(self, answers: dict) -> dict:
        """Resolve clarification questions from a previous plan() call."""
        with self._lock:
            pending_prompt = self._pending_prompt
            pending_context = self._pending_context

        if pending_prompt is None or pending_context is None:
            return {"error": "No pending clarification. Call plan() first."}

        planner = self._get_planner()
        plan = planner.generate_plan(pending_prompt, pending_context, answers=answers)
        validate_plan_size(len(plan.steps))

        with self._lock:
            self._last_plan = plan
            if plan.status == "ready":
                self._pending_prompt = None
                self._pending_context = None

        return plan.model_dump()

    def run(self, prompt: str) -> dict:
        """Plan AND execute a GIS workflow from a natural-language prompt."""
        context = self._gather_context()
        planner = self._get_planner()
        plan = planner.generate_plan(prompt, context)

        with self._lock:
            self._last_plan = plan
            if plan.status == "needs_clarification":
                self._pending_prompt = prompt
                self._pending_context = context

        if plan.status == "needs_clarification":
            return plan.model_dump()

        executor = self._get_executor()
        report = executor.execute(plan)

        with self._lock:
            self._last_report = report

        return report.model_dump()

    def run_plan(self, plan_dict: dict) -> dict:
        """Execute a specific plan dict (e.g. from plan(), possibly edited)."""
        plan = GISPlan.model_validate(plan_dict)

        with self._lock:
            self._last_plan = plan

        if plan.status != "ready":
            return {"error": "Plan status must be 'ready' to execute."}

        executor = self._get_executor()
        report = executor.execute(plan)

        with self._lock:
            self._last_report = report

        return report.model_dump()

    def resume(self, resume_token: str, approved: bool) -> dict:
        """Resume a blocked execution after a confirm step."""
        executor = self._get_executor()
        report = executor.resume(resume_token, approved)

        with self._lock:
            self._last_report = report

        return report.model_dump()

    def diagnose(self, error_text: str) -> dict:
        """Diagnose a GIS/QGIS error and suggest fixes."""
        from openai import OpenAI

        context: dict = {}
        try:
            context = self._gather_context()
        except Exception:
            pass

        context_json = json.dumps(context, indent=2, default=str)
        user_prompt = DIAGNOSE_USER_TEMPLATE.format(
            error_text=error_text, context_json=context_json
        )

        client = OpenAI()
        model = os.environ.get("GIS_PLANNER_MODEL", "gpt-4o")
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": DIAGNOSE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )

        return {
            "diagnosis": completion.choices[0].message.content or "No diagnosis available."
        }

    def explain_last_run(self) -> dict:
        """Show details of the last plan and/or execution run."""
        with self._lock:
            last_plan = self._last_plan
            last_report = self._last_report

        result: dict[str, Any] = {}
        if last_plan:
            result["last_plan"] = last_plan.model_dump()
        else:
            result["last_plan"] = None

        if last_report:
            result["last_report"] = last_report.model_dump()
        else:
            result["last_report"] = None

        return result

    def list_capabilities(self, verbose: bool = False) -> dict:
        """List supported operations, algorithm categories, and workspace policy."""
        native_algs = sorted(
            a for a in ALLOWED_ALGORITHMS if a.startswith("native:")
        )
        gdal_algs = sorted(
            a for a in ALLOWED_ALGORITHMS if a.startswith("gdal:")
        )

        if verbose:
            alg_section: dict = {
                "native": native_algs,
                "gdal": gdal_algs,
            }
        else:
            alg_section = {
                "native": {
                    "geometry": [
                        "native:buffer", "native:centroids",
                        "native:convexhull", "native:simplifygeometries",
                        "native:fixgeometries",
                    ],
                    "overlay": [
                        "native:clip", "native:intersection",
                        "native:union", "native:difference",
                    ],
                    "general": [
                        "native:dissolve", "native:reprojectlayer",
                        "native:joinattributesbylocation",
                        "native:fieldcalculator",
                        "native:extractbyattribute",
                    ],
                    "analysis": [
                        "native:basicstatisticsforfields",
                        "native:listuniquevalues",
                        "native:countpointsinpolygon",
                    ],
                    "creation": [
                        "native:createpointslayerfromtable",
                        "native:creategrid",
                    ],
                    "total_count": len(native_algs),
                },
                "gdal": {
                    "raster_analysis": [
                        "gdal:hillshade", "gdal:slope", "gdal:aspect",
                        "gdal:contour", "gdal:roughness",
                    ],
                    "raster_conversion": [
                        "gdal:warpreproject", "gdal:merge",
                        "gdal:cliprasterbyextent", "gdal:translate",
                    ],
                    "vector": [
                        "gdal:buffervectors",
                        "gdal:clipvectorbyextent",
                        "gdal:dissolve",
                    ],
                    "total_count": len(gdal_algs),
                },
                "hint": "Use verbose=true for the full list of all algorithm IDs.",
            }

        return {
            "version": "0.1.0",
            "operations": [
                "scan_files - list data files in a directory",
                "add_layer - load a vector or raster layer into QGIS",
                "processing - run a QGIS Processing algorithm",
                "remove_layer - remove a layer from the project",
                "zoom - zoom the map canvas to a layer",
                "render - render the map to an image file",
                "confirm - request user confirmation before proceeding",
            ],
            "algorithms": alg_section,
            "workspace": self.workspace_dir,
            "limits": {
                "max_plan_steps": 20,
                "max_scan_files": 200,
                "max_feature_limit": 100,
            },
            "safety": [
                "execute_code is not available",
                "All outputs confined to workspace directory",
                "Confirm required before overwriting files",
                "Algorithm IDs validated against allowlist",
            ],
        }

    def qgis_status(self) -> dict:
        """Check whether the QGIS socket is reachable."""
        try:
            client = self._get_client()
            resp = client.ping()
            connected = resp.get("status") == "success"
        except Exception:
            connected = False

        return {
            "qgis_connected": connected,
            "host": self._client.host if self._client else "127.0.0.1",
            "port": self._client.port if self._client else 9876,
        }

    # -- Lifecycle -----------------------------------------------------------

    def startup(self) -> None:
        """Attempt to connect to QGIS on startup (non-fatal)."""
        try:
            self._get_client()
            logger.info("Connected to QGIS on startup")
        except Exception as e:
            logger.warning("Could not connect to QGIS on startup: %s", e)

    def shutdown(self) -> None:
        """Disconnect from QGIS."""
        if self._client:
            self._client.disconnect()
            self._client = None
        logger.info("GISEngine shut down")
