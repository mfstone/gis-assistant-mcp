"""OpenAI integration -- generates a GISPlan from a natural-language prompt."""

from __future__ import annotations

import logging
import os

from openai import OpenAI

from .prompts import SYSTEM_PROMPT, build_user_prompt
from .schemas import GISPlan

logger = logging.getLogger("gis_assistant.planner")

DEFAULT_MODEL = "gpt-4o"


class GISPlanner:
    """Generates GIS workflow plans using OpenAI Structured Outputs."""

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("GIS_PLANNER_MODEL", DEFAULT_MODEL)
        self._client = OpenAI()  # uses OPENAI_API_KEY env var

    def generate_plan(
        self,
        user_request: str,
        qgis_context: dict,
        answers: dict[str, str] | None = None,
    ) -> GISPlan:
        """Call OpenAI to produce a structured GISPlan.

        Args:
            user_request: The natural-language GIS request.
            qgis_context: Current QGIS state (layers, project info, etc.).
            answers: Optional dict of {question_id: answer} from a previous
                     needs_clarification response.

        Returns:
            A validated GISPlan (may have status="needs_clarification").
        """
        user_prompt = build_user_prompt(user_request, qgis_context, answers)

        logger.info("Calling OpenAI %s for plan generation", self.model)
        completion = self._client.beta.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=GISPlan,
        )

        plan = completion.choices[0].message.parsed
        if plan is None:
            raise ValueError("OpenAI returned no parsed plan")

        logger.info(
            "Plan generated: status=%s, %d steps, %d inputs",
            plan.status,
            len(plan.steps),
            len(plan.inputs),
        )
        return plan
