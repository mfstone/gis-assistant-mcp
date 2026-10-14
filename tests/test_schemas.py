"""Tests for Pydantic schema validation."""

import json

from gis_assistant.schemas import (
    ClarificationQuestion,
    GISPlan,
    PlanOutputs,
    PlanStep,
    RunReport,
    StepOperation,
    StepResult,
)


def test_gisplan_ready_valid():
    plan = GISPlan(
        status="ready",
        inputs=[],
        outputs=PlanOutputs(directory="/tmp/out", files=[]),
        steps=[
            PlanStep(
                id="step_1",
                operation=StepOperation.processing,
                description="Buffer",
                algorithm="native:buffer",
                parameters=json.dumps({"INPUT": "@input.x", "DISTANCE": 100}),
            )
        ],
    )
    assert plan.status == "ready"
    assert len(plan.steps) == 1


def test_gisplan_needs_clarification():
    plan = GISPlan(
        status="needs_clarification",
        questions=[
            ClarificationQuestion(
                id="q_crs",
                question="What CRS should the output use?",
                choices=["EPSG:4326", "EPSG:32618"],
            )
        ],
    )
    assert plan.status == "needs_clarification"
    assert len(plan.questions) == 1
    assert plan.questions[0].id == "q_crs"


def test_planstep_processing():
    step = PlanStep(
        id="s1",
        operation=StepOperation.processing,
        description="Run buffer",
        algorithm="native:buffer",
        parameters=json.dumps({"INPUT": "@input.parcels", "DISTANCE": 50, "OUTPUT": "@output.buf"}),
    )
    assert step.operation == StepOperation.processing
    assert step.algorithm == "native:buffer"
    assert step.parsed_parameters()["DISTANCE"] == 50


def test_runreport_round_trip():
    plan = GISPlan(
        status="ready",
        outputs=PlanOutputs(directory="/tmp", files=[]),
    )
    report = RunReport(
        resume_token="abc-123",
        plan=plan,
        results=[
            StepResult(step_id="s1", status="success", message="OK"),
        ],
        outputs_created=["/tmp/out.gpkg"],
        success=True,
        completed_step_index=0,
    )
    data = report.model_dump_json()
    restored = RunReport.model_validate_json(data)
    assert restored.resume_token == "abc-123"
    assert restored.results[0].status == "success"
    assert restored.success is True


def test_parsed_parameters_single_quote_fallback():
    """LLM sometimes emits single-quoted strings; ast.literal_eval handles it."""
    step = PlanStep(
        id="s1",
        operation=StepOperation.processing,
        description="Extract by expression",
        algorithm="native:extractbyexpression",
        parameters="{'INPUT': '@input.parcels', 'EXPRESSION': '\"Shape_Area\" < 1000', 'OUTPUT': 'TEMPORARY_OUTPUT'}",
    )
    params = step.parsed_parameters()
    assert params["INPUT"] == "@input.parcels"
    assert '"Shape_Area"' in params["EXPRESSION"]


def test_step_operation_enum_values():
    ops = {e.value for e in StepOperation}
    expected = {
        "scan_files", "add_layer", "processing",
        "remove_layer", "zoom", "render", "confirm",
    }
    assert ops == expected
