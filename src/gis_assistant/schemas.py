"""Pydantic models for GIS plan schema and execution reports."""

from __future__ import annotations

import json
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# -- Clarification -----------------------------------------------------------

class ClarificationQuestion(BaseModel):
    """A question the planner needs answered before it can produce a plan."""

    id: str = Field(description="Unique question identifier, e.g. 'q_raster'")
    question: str = Field(description="The question to present to the user")
    choices: list[str] = Field(
        default_factory=list,
        description="Suggested answers (empty if free-form)",
    )
    default: str = Field(default="", description="Default answer if any")


# -- Plan Inputs -------------------------------------------------------------

class InputLayer(BaseModel):
    """A named input dataset referenced in the plan."""

    name: str = Field(description="Logical name, e.g. 'parcels'")
    path: str = Field(description="Absolute file path or QGIS layer source path")
    layer_type: Literal["vector", "raster"] = Field(
        description="Whether this is a vector or raster layer",
    )


# -- Plan Outputs ------------------------------------------------------------

class OutputFile(BaseModel):
    """A single output file the plan will produce."""

    name: str = Field(description="Logical name, e.g. 'buffered_parcels'")
    format: str = Field(description="File extension, e.g. 'gpkg', 'tif', 'shp'")


class PlanOutputs(BaseModel):
    """Where output files will be written."""

    directory: str = Field(description="Absolute path to the output directory")
    files: list[OutputFile] = Field(description="Output files to be created")


# -- Assumptions -------------------------------------------------------------

class Assumption(BaseModel):
    """An assumption the planner made that the user should know about."""

    key: str = Field(description="What this is about, e.g. 'crs', 'units'")
    value: str = Field(description="The assumed value")
    reason: str = Field(description="Why this assumption was made")


# -- Step Types --------------------------------------------------------------

class StepOperation(str, Enum):
    """The type of operation a step performs."""

    scan_files = "scan_files"
    add_layer = "add_layer"
    processing = "processing"
    remove_layer = "remove_layer"
    zoom = "zoom"
    render = "render"
    confirm = "confirm"


class PlanStep(BaseModel):
    """A single step in the execution plan.

    References use @-notation resolved at execution time:
      @input.<name>   -> input's path
      @output.<name>  -> <outputs.directory>/<name>.<format>
      @step.<id>      -> primary output of a prior step
    """

    id: str = Field(description="Unique step ID, e.g. 'step_1'")
    operation: StepOperation = Field(description="Type of operation")
    description: str = Field(description="Human-readable description")

    # processing fields
    algorithm: str = Field(default="", description="QGIS algorithm ID, e.g. 'native:buffer'")
    parameters: str = Field(
        default="{}",
        description=(
            "Algorithm parameters as a JSON object string, e.g. "
            '\'{"INPUT": "@input.parcels", "DISTANCE": 100, "OUTPUT": "@output.buffered"}\''
        ),
    )

    # add_layer fields
    layer_path: str = Field(default="", description="Path to load (may use @-ref)")
    layer_name: str = Field(default="", description="Display name for the layer")
    provider: str = Field(default="ogr", description="Data provider: 'ogr' or 'gdal'")

    # render fields
    output_path: str = Field(default="", description="Output file path (may use @-ref)")
    width: int = Field(default=800, description="Render width in pixels")
    height: int = Field(default=600, description="Render height in pixels")

    # confirm fields
    confirm_message: str = Field(default="", description="Message to present before proceeding")

    # scan_files fields
    scan_directory: str = Field(default="", description="Directory to scan")
    scan_extensions: list[str] = Field(default_factory=list, description="Extensions, e.g. ['.shp']")

    # zoom / remove_layer fields
    zoom_layer: str = Field(default="", description="Layer ID or @-ref to zoom to")
    remove_layer_id: str = Field(default="", description="Layer ID or @-ref to remove")

    def parsed_parameters(self) -> dict:
        """Parse the JSON parameters string into a dict.

        Falls back to ast.literal_eval if JSON parsing fails (e.g. when the
        LLM emits single-quoted strings instead of proper JSON).
        """
        if not self.parameters or self.parameters == "{}":
            return {}
        try:
            return json.loads(self.parameters)
        except json.JSONDecodeError:
            import ast
            return ast.literal_eval(self.parameters)


# -- The Complete Plan -------------------------------------------------------

class GISPlan(BaseModel):
    """Complete GIS workflow plan generated by the AI planner."""

    status: Literal["ready", "needs_clarification"] = Field(
        description="Whether the plan is ready to execute or needs user input",
    )
    questions: list[ClarificationQuestion] = Field(
        default_factory=list,
        description="Questions for the user (populated when status is needs_clarification)",
    )
    inputs: list[InputLayer] = Field(
        default_factory=list, description="Input datasets",
    )
    outputs: PlanOutputs = Field(
        default_factory=lambda: PlanOutputs(directory="", files=[]),
        description="Output configuration",
    )
    assumptions: list[Assumption] = Field(
        default_factory=list, description="Assumptions made by the planner",
    )
    steps: list[PlanStep] = Field(
        default_factory=list, description="Ordered steps (empty if needs_clarification)",
    )


# -- Execution Report --------------------------------------------------------

class StepResult(BaseModel):
    """Result of executing a single step."""

    step_id: str
    status: Literal["success", "error", "skipped", "blocked"]
    message: str = ""
    output_path: str = ""
    qgis_response: dict = Field(default_factory=dict)


class RunReport(BaseModel):
    """Report returned after gis.run completes or is blocked."""

    resume_token: str = Field(default="", description="UUID for resume if blocked")
    plan: GISPlan
    results: list[StepResult] = Field(default_factory=list)
    outputs_created: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    success: bool = False
    blocked_on_confirm: bool = False
    blocked_confirm_id: str = ""
    completed_step_index: int = -1
