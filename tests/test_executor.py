"""Tests for PlanExecutor: reference resolution, plan validation, confirm/resume."""

import os
from unittest.mock import MagicMock

import pytest

from gis_assistant.executor import PlanExecutor
from gis_assistant.qgis_client import AmbiguousLayerError, QGISClient
from gis_assistant.schemas import (
    GISPlan,
    InputLayer,
    OutputFile,
    PlanOutputs,
    PlanStep,
    StepOperation,
)


# -- Helpers -----------------------------------------------------------------

def _make_executor(tmp_path):
    """Create a PlanExecutor with a mocked QGISClient."""
    client = MagicMock(spec=QGISClient)
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)
    return PlanExecutor(client, workspace_dir=workspace), client


def _simple_plan(workspace, steps=None, inputs=None):
    """Build a minimal valid plan."""
    return GISPlan(
        status="ready",
        inputs=inputs or [],
        outputs=PlanOutputs(
            directory=workspace,
            files=[OutputFile(name="result", format="gpkg")],
        ),
        steps=steps or [],
    )


# -- Reference resolution ---------------------------------------------------

def test_populate_input_refs(tmp_path):
    executor, _ = _make_executor(tmp_path)
    plan = _simple_plan(
        str(tmp_path / "workspace"),
        inputs=[InputLayer(name="parcels", path="/data/parcels.gpkg", layer_type="vector")],
    )
    executor._populate_input_refs(plan)
    assert executor._ref_map["@input.parcels"] == "/data/parcels.gpkg"


def test_populate_output_refs(tmp_path):
    executor, _ = _make_executor(tmp_path)
    workspace = str(tmp_path / "workspace")
    plan = _simple_plan(workspace)
    executor._populate_output_refs(plan)
    assert executor._ref_map["@output.result"] == os.path.join(workspace, "result.gpkg")


def test_resolve_ref_success(tmp_path):
    executor, _ = _make_executor(tmp_path)
    executor._ref_map["@input.x"] = "/data/x.gpkg"
    assert executor._resolve_ref("@input.x") == "/data/x.gpkg"


def test_resolve_ref_unknown_raises(tmp_path):
    executor, _ = _make_executor(tmp_path)
    with pytest.raises(ValueError, match="Unresolved reference"):
        executor._resolve_ref("@input.nonexistent")


def test_resolve_ref_non_ref_passthrough(tmp_path):
    executor, _ = _make_executor(tmp_path)
    assert executor._resolve_ref("/literal/path") == "/literal/path"


def test_resolve_params_nested(tmp_path):
    executor, _ = _make_executor(tmp_path)
    executor._ref_map["@input.x"] = "/data/x.gpkg"
    executor._ref_map["@output.y"] = "/out/y.gpkg"

    params = {
        "INPUT": "@input.x",
        "OUTPUT": "@output.y",
        "DISTANCE": 100,
        "OPTIONS": {"CRS": "@input.x"},
        "LAYERS": ["@input.x", "@output.y"],
    }
    resolved = executor._resolve_params(params)
    assert resolved["INPUT"] == "/data/x.gpkg"
    assert resolved["OUTPUT"] == "/out/y.gpkg"
    assert resolved["DISTANCE"] == 100
    assert resolved["OPTIONS"]["CRS"] == "/data/x.gpkg"
    assert resolved["LAYERS"] == ["/data/x.gpkg", "/out/y.gpkg"]


# -- Plan validation ---------------------------------------------------------

def test_validate_plan_bad_algorithm(tmp_path):
    executor, _ = _make_executor(tmp_path)
    workspace = str(tmp_path / "workspace")
    plan = _simple_plan(
        workspace,
        steps=[
            PlanStep(
                id="s1",
                operation=StepOperation.processing,
                description="Bad algo",
                algorithm="native:doesnotexist",
            ),
        ],
    )
    with pytest.raises(ValueError, match="not in the allowlist"):
        executor.validate_plan(plan)


# -- Confirm-stop and resume -------------------------------------------------

def test_execute_confirm_blocks(tmp_path):
    executor, _ = _make_executor(tmp_path)
    workspace = str(tmp_path / "workspace")
    plan = _simple_plan(
        workspace,
        steps=[
            PlanStep(
                id="s1",
                operation=StepOperation.confirm,
                description="Confirm overwrite",
                confirm_message="Overwrite existing file?",
            ),
        ],
    )
    report = executor.execute(plan)
    assert report.blocked_on_confirm is True
    assert report.blocked_confirm_id == "s1"
    assert report.resume_token != ""
    assert report.success is False
    assert any(r.status == "blocked" for r in report.results)


def test_resume_approved_continues(tmp_path):
    executor, client = _make_executor(tmp_path)
    workspace = str(tmp_path / "workspace")

    # Create a directory for scan
    scan_dir = tmp_path / "scandir"
    scan_dir.mkdir()
    (scan_dir / "a.gpkg").write_text("data")

    plan = _simple_plan(
        workspace,
        steps=[
            PlanStep(
                id="s1",
                operation=StepOperation.confirm,
                description="Confirm",
                confirm_message="Proceed?",
            ),
            PlanStep(
                id="s2",
                operation=StepOperation.scan_files,
                description="Scan",
                scan_directory=str(scan_dir),
            ),
        ],
    )
    # Execute — should block on confirm
    report = executor.execute(plan)
    assert report.blocked_on_confirm is True
    token = report.resume_token

    # Resume approved — should execute the scan step
    resumed = executor.resume(token, approved=True)
    assert resumed.success is True
    # Should have both the blocked confirm result AND the scan result
    assert len(resumed.results) >= 2


def test_resume_declined_returns_full_history(tmp_path):
    executor, client = _make_executor(tmp_path)
    workspace = str(tmp_path / "workspace")

    scan_dir = tmp_path / "scandir"
    scan_dir.mkdir()
    (scan_dir / "a.gpkg").write_text("data")

    plan = _simple_plan(
        workspace,
        steps=[
            PlanStep(
                id="s1",
                operation=StepOperation.scan_files,
                description="Scan first",
                scan_directory=str(scan_dir),
            ),
            PlanStep(
                id="s2",
                operation=StepOperation.confirm,
                description="Confirm",
                confirm_message="Proceed?",
            ),
            PlanStep(
                id="s3",
                operation=StepOperation.scan_files,
                description="Scan again",
                scan_directory=str(scan_dir),
            ),
        ],
    )
    report = executor.execute(plan)
    assert report.blocked_on_confirm is True
    token = report.resume_token

    # Decline — should return all prior results plus the abort
    declined = executor.resume(token, approved=False)
    assert declined.success is False
    # s1 (success) + s2 (blocked) + s2 (skipped abort)
    assert len(declined.results) >= 3
    assert any(r.status == "skipped" for r in declined.results)
    # The scan result from s1 should be present
    assert any(r.step_id == "s1" and r.status == "success" for r in declined.results)


def test_resume_invalid_token(tmp_path):
    executor, _ = _make_executor(tmp_path)
    with pytest.raises(ValueError, match="No pending execution"):
        executor.resume("invalid-token", approved=True)


# -- Error handling ----------------------------------------------------------

def test_execute_error_stops(tmp_path):
    """An error step should cause fail-fast (no subsequent steps run)."""
    executor, client = _make_executor(tmp_path)
    workspace = str(tmp_path / "workspace")

    # add_layer will fail because the mock returns an error response
    client.add_vector_layer.return_value = {
        "status": "error",
        "message": "Layer not valid",
    }

    plan = _simple_plan(
        workspace,
        inputs=[InputLayer(name="x", path="/data/x.gpkg", layer_type="vector")],
        steps=[
            PlanStep(
                id="s1",
                operation=StepOperation.add_layer,
                description="Load layer",
                layer_path="@input.x",
            ),
            PlanStep(
                id="s2",
                operation=StepOperation.scan_files,
                description="Should not run",
                scan_directory=workspace,
            ),
        ],
    )
    report = executor.execute(plan)
    assert report.success is False
    # Only one result — s2 should not have run
    assert len(report.results) == 1
    assert report.results[0].step_id == "s1"
    assert report.results[0].status == "error"


def test_ambiguous_layer_error_details(tmp_path):
    """AmbiguousLayerError should include match details in the error message."""
    executor, client = _make_executor(tmp_path)
    workspace = str(tmp_path / "workspace")

    matches = [
        {"name": "Roads", "id": "roads_1"},
        {"name": "Roads", "id": "roads_2"},
    ]
    client.resolve_layer.side_effect = AmbiguousLayerError("Roads", matches)

    plan = _simple_plan(
        workspace,
        steps=[
            PlanStep(
                id="s1",
                operation=StepOperation.zoom,
                description="Zoom to roads",
                zoom_layer="Roads",
            ),
        ],
    )
    # Populate ref_map so zoom_layer resolves
    executor._ref_map["Roads"] = "Roads"

    report = executor.execute(plan)
    assert report.success is False
    assert report.results[0].status == "error"
    assert "roads_1" in report.results[0].message
    assert "roads_2" in report.results[0].message
