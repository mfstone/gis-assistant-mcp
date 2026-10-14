"""Step-by-step execution engine for GIS plans."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field

from .qgis_client import AmbiguousLayerError, QGISClient
from .safety import (
    MAX_SCAN_FILES,
    check_overwrite,
    resolve_algorithm,
    validate_output_path,
    validate_input_path_exists,
    validate_plan_size,
)
from .schemas import (
    GISPlan,
    PlanStep,
    RunReport,
    StepOperation,
    StepResult,
)

logger = logging.getLogger("gis_assistant.executor")


@dataclass
class PendingExecution:
    """Saved state for a blocked execution that can be resumed."""

    plan: GISPlan
    ref_map: dict[str, str]
    completed_step_index: int
    workspace_dir: str
    prior_results: list = field(default_factory=list)
    outputs_created: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class PlanExecutor:
    """Executes validated GIS plans step-by-step."""

    def __init__(self, client: QGISClient, workspace_dir: str):
        self.client = client
        self.workspace_dir = workspace_dir
        self._ref_map: dict[str, str] = {}
        self._pending: dict[str, PendingExecution] = {}

    # -- Reference Resolution ------------------------------------------------

    def _populate_input_refs(self, plan: GISPlan) -> None:
        for inp in plan.inputs:
            self._ref_map[f"@input.{inp.name}"] = inp.path

    def _populate_output_refs(self, plan: GISPlan) -> None:
        for out_file in plan.outputs.files:
            path = os.path.join(
                plan.outputs.directory, f"{out_file.name}.{out_file.format}"
            )
            self._ref_map[f"@output.{out_file.name}"] = path

    def _resolve_ref(self, value: str) -> str:
        if isinstance(value, str) and value.startswith("@"):
            resolved = self._ref_map.get(value)
            if resolved is None:
                raise ValueError(f"Unresolved reference: {value}")
            return resolved
        return value

    def _resolve_params(self, params: dict) -> dict:
        resolved: dict = {}
        for k, v in params.items():
            if isinstance(v, str):
                resolved[k] = self._resolve_ref(v)
            elif isinstance(v, dict):
                resolved[k] = self._resolve_params(v)
            elif isinstance(v, list):
                resolved[k] = [
                    self._resolve_ref(item) if isinstance(item, str) else item
                    for item in v
                ]
            else:
                resolved[k] = v
        return resolved

    # -- Validation ----------------------------------------------------------

    def validate_plan(self, plan: GISPlan) -> list[str]:
        """Pre-execution validation. Returns list of warnings."""
        warnings: list[str] = []
        validate_plan_size(len(plan.steps))

        # Validate output directory is within workspace
        if plan.outputs.directory:
            validate_output_path(plan.outputs.directory, self.workspace_dir)

        # Validate algorithms
        for step in plan.steps:
            if step.operation == StepOperation.processing:
                resolve_algorithm(step.algorithm)

        # Populate refs and check overwrites
        self._populate_input_refs(plan)
        self._populate_output_refs(plan)
        for key, path in self._ref_map.items():
            if key.startswith("@output.") and check_overwrite(path):
                warnings.append(f"Output {path} already exists and will be overwritten")

        # Check inputs exist
        for inp in plan.inputs:
            try:
                validate_input_path_exists(inp.path)
            except FileNotFoundError:
                warnings.append(f"Input {inp.name!r} not found: {inp.path}")

        return warnings

    # -- Step Executors ------------------------------------------------------

    def _exec_scan_files(self, step: PlanStep) -> StepResult:
        directory = self._resolve_ref(step.scan_directory)
        files = QGISClient.scan_directory(
            directory,
            extensions=step.scan_extensions or None,
            limit=MAX_SCAN_FILES,
        )
        return StepResult(
            step_id=step.id,
            status="success",
            message=f"Found {len(files)} files",
            qgis_response={"files": files},
        )

    def _exec_add_layer(self, step: PlanStep) -> StepResult:
        path = self._resolve_ref(step.layer_path)
        provider = step.provider or "ogr"
        if provider == "gdal":
            resp = self.client.add_raster_layer(path, name=step.layer_name or None)
        else:
            resp = self.client.add_vector_layer(path, name=step.layer_name or None)

        if resp and resp.get("status") == "success":
            layer_id = resp.get("result", {}).get("id", "")
            self._ref_map[f"@step.{step.id}"] = layer_id
            return StepResult(
                step_id=step.id, status="success",
                message=f"Layer loaded: {step.layer_name or path}",
                output_path=path, qgis_response=resp,
            )
        msg = resp.get("message", "Unknown error") if resp else "No response"
        return StepResult(step_id=step.id, status="error", message=msg,
                          qgis_response=resp or {})

    def _exec_processing(self, step: PlanStep) -> StepResult:
        algorithm = resolve_algorithm(step.algorithm)
        params = self._resolve_params(step.parsed_parameters())

        # Validate output paths are within workspace (skip QGIS special values)
        _QGIS_SPECIAL_OUTPUTS = {"memory:", "TEMPORARY_OUTPUT", ""}
        for k, v in params.items():
            if k.upper() == "OUTPUT" and isinstance(v, str):
                if v not in _QGIS_SPECIAL_OUTPUTS:
                    validate_output_path(v, self.workspace_dir)

        resp = self.client.execute_processing(algorithm, params)
        if resp and resp.get("status") == "success":
            result_data = resp.get("result", {})
            output_path = result_data.get("result", {}).get("OUTPUT", "")
            self._ref_map[f"@step.{step.id}"] = output_path
            return StepResult(
                step_id=step.id, status="success",
                message=f"Algorithm {algorithm} completed",
                output_path=output_path, qgis_response=resp,
            )
        msg = resp.get("message", "Unknown error") if resp else "No response"
        return StepResult(step_id=step.id, status="error", message=msg,
                          qgis_response=resp or {})

    def _exec_remove_layer(self, step: PlanStep) -> StepResult:
        layer_id = self._resolve_ref(step.remove_layer_id)
        try:
            layer_id = self.client.resolve_layer(layer_id)
        except AmbiguousLayerError as e:
            choices = ", ".join(
                f"{m.get('name')} (id={m.get('id')})" for m in e.matches
            )
            return StepResult(
                step_id=step.id, status="error",
                message=f"Ambiguous layer: {e.identifier}. Matches: {choices}",
            )
        resp = self.client.remove_layer(layer_id)
        if resp and resp.get("status") == "success":
            return StepResult(step_id=step.id, status="success",
                              message=f"Removed layer {layer_id}", qgis_response=resp)
        msg = resp.get("message", "Unknown error") if resp else "No response"
        return StepResult(step_id=step.id, status="error", message=msg,
                          qgis_response=resp or {})

    def _exec_zoom(self, step: PlanStep) -> StepResult:
        layer_id = self._resolve_ref(step.zoom_layer)
        try:
            layer_id = self.client.resolve_layer(layer_id)
        except AmbiguousLayerError as e:
            choices = ", ".join(
                f"{m.get('name')} (id={m.get('id')})" for m in e.matches
            )
            return StepResult(
                step_id=step.id, status="error",
                message=f"Ambiguous layer: {e.identifier}. Matches: {choices}",
            )
        resp = self.client.zoom_to_layer(layer_id)
        if resp and resp.get("status") == "success":
            return StepResult(step_id=step.id, status="success",
                              message=f"Zoomed to {layer_id}", qgis_response=resp)
        msg = resp.get("message", "Unknown error") if resp else "No response"
        return StepResult(step_id=step.id, status="error", message=msg,
                          qgis_response=resp or {})

    def _exec_render(self, step: PlanStep) -> StepResult:
        path = self._resolve_ref(step.output_path)
        validate_output_path(path, self.workspace_dir)
        resp = self.client.render_map(path, width=step.width, height=step.height)
        if resp and resp.get("status") == "success":
            return StepResult(step_id=step.id, status="success",
                              message=f"Rendered map to {path}",
                              output_path=path, qgis_response=resp)
        msg = resp.get("message", "Unknown error") if resp else "No response"
        return StepResult(step_id=step.id, status="error", message=msg,
                          qgis_response=resp or {})

    def _exec_confirm(self, step: PlanStep) -> StepResult:
        return StepResult(
            step_id=step.id,
            status="blocked",
            message=f"CONFIRMATION REQUIRED: {step.confirm_message}",
        )

    # -- Dispatch Table ------------------------------------------------------

    _DISPATCH: dict[StepOperation, str] = {
        StepOperation.scan_files: "_exec_scan_files",
        StepOperation.add_layer: "_exec_add_layer",
        StepOperation.processing: "_exec_processing",
        StepOperation.remove_layer: "_exec_remove_layer",
        StepOperation.zoom: "_exec_zoom",
        StepOperation.render: "_exec_render",
        StepOperation.confirm: "_exec_confirm",
    }

    # -- Main Execution Loop -------------------------------------------------

    def execute(self, plan: GISPlan, start_index: int = 0) -> RunReport:
        """Execute a validated plan step-by-step.

        Stops on first error or confirm step. Returns a RunReport.
        """
        resume_token = str(uuid.uuid4())

        # Ensure output directory exists (resolved + workspace-validated)
        if plan.outputs.directory:
            resolved_dir = validate_output_path(
                plan.outputs.directory, self.workspace_dir
            )
            os.makedirs(resolved_dir, exist_ok=True)

        warnings = self.validate_plan(plan) if start_index == 0 else []
        results: list[StepResult] = []
        outputs_created: list[str] = []

        for i, step in enumerate(plan.steps):
            if i < start_index:
                continue

            logger.info(
                "Executing step %s: %s (%s)",
                step.id, step.description, step.operation.value,
            )

            handler_name = self._DISPATCH.get(step.operation)
            if not handler_name:
                results.append(StepResult(
                    step_id=step.id, status="error",
                    message=f"Unknown operation: {step.operation}",
                ))
                break

            try:
                handler = getattr(self, handler_name)
                result = handler(step)
            except Exception as e:
                logger.exception("Step %s failed", step.id)
                result = StepResult(step_id=step.id, status="error", message=str(e))

            results.append(result)
            if result.output_path:
                outputs_created.append(result.output_path)

            # Confirm = hard stop
            if result.status == "blocked":
                self._pending[resume_token] = PendingExecution(
                    plan=plan,
                    ref_map=dict(self._ref_map),
                    completed_step_index=i,
                    workspace_dir=self.workspace_dir,
                    prior_results=list(results),
                    outputs_created=list(outputs_created),
                    warnings=list(warnings),
                )
                return RunReport(
                    resume_token=resume_token,
                    plan=plan,
                    results=results,
                    outputs_created=outputs_created,
                    warnings=warnings,
                    success=False,
                    blocked_on_confirm=True,
                    blocked_confirm_id=step.id,
                    completed_step_index=i,
                )

            # Error = fail-fast
            if result.status == "error":
                logger.error("Step %s failed: %s", step.id, result.message)
                break

        success = all(r.status in ("success", "skipped") for r in results)
        return RunReport(
            resume_token=resume_token,
            plan=plan,
            results=results,
            outputs_created=outputs_created,
            warnings=warnings,
            success=success,
            completed_step_index=len(plan.steps) - 1 if success else -1,
        )

    def resume(self, resume_token: str, approved: bool) -> RunReport:
        """Resume a blocked execution.

        If approved, skips the confirm step and continues from the next step.
        If not approved, returns an aborted report.
        """
        pending = self._pending.pop(resume_token, None)
        if pending is None:
            raise ValueError(f"No pending execution for token: {resume_token}")

        if not approved:
            abort_result = StepResult(
                step_id=pending.plan.steps[pending.completed_step_index].id,
                status="skipped",
                message="User declined confirmation",
            )
            return RunReport(
                plan=pending.plan,
                results=pending.prior_results + [abort_result],
                outputs_created=pending.outputs_created,
                warnings=pending.warnings,
                success=False,
                completed_step_index=pending.completed_step_index,
            )

        # Restore state and continue from next step
        self._ref_map = pending.ref_map
        self.workspace_dir = pending.workspace_dir
        continuation = self.execute(
            pending.plan, start_index=pending.completed_step_index + 1
        )
        # Merge prior results into continuation report
        continuation.results = pending.prior_results + continuation.results
        continuation.outputs_created = (
            pending.outputs_created + continuation.outputs_created
        )
        continuation.warnings = pending.warnings + continuation.warnings
        return continuation
