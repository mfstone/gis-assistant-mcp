"""Provider-agnostic LLM access.

Claude is the default. OpenAI stays available by setting
``GIS_PLANNER_PROVIDER=openai``.

Two call shapes cover everything this project needs:

``generate_structured``
    Plan generation. The model must return an object matching a Pydantic
    schema, so both providers use their native structured-output path rather
    than asking for JSON in a prompt and parsing it.

``complete_text``
    Error diagnosis. Free-form prose, no schema.

Each SDK is imported inside the provider that uses it, so only the provider
you actually select needs to be installed.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

logger = logging.getLogger("gis_assistant.llm")

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
DEFAULT_PROVIDER = PROVIDER_ANTHROPIC

#: Per-provider default model. ``GIS_PLANNER_MODEL`` overrides the selected one.
DEFAULT_MODELS = {
    PROVIDER_ANTHROPIC: "claude-opus-5",
    PROVIDER_OPENAI: "gpt-4o",
}

#: Generous enough that a long plan is never truncated mid-step. Both providers
#: bill on tokens produced, not on this ceiling.
MAX_TOKENS = 16000

_PROVIDER_ALIASES = {
    PROVIDER_ANTHROPIC: PROVIDER_ANTHROPIC,
    "claude": PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI: PROVIDER_OPENAI,
    "chatgpt": PROVIDER_OPENAI,
    "gpt": PROVIDER_OPENAI,
}

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMError(RuntimeError):
    """A provider call failed, or returned something unusable."""


@runtime_checkable
class LLMProvider(Protocol):
    """What the engine needs from a model, independent of vendor."""

    name: str
    model: str

    def generate_structured(
        self, *, system: str, user: str, schema: type[SchemaT]
    ) -> SchemaT:
        """Return an instance of ``schema``, validated by the provider."""
        ...

    def complete_text(self, *, system: str, user: str) -> str:
        """Return free-form text."""
        ...


class AnthropicProvider:
    """Claude, via the official ``anthropic`` SDK.

    Uses ``messages.parse`` with ``output_format``, which validates the
    response against the Pydantic schema and hands back a typed instance.
    Adaptive thinking is on: plan generation has to reason about CRS and units,
    algorithm choice, and how one step's outputs feed the next.
    """

    def __init__(self, model: str | None = None) -> None:
        import anthropic  # local: only needed when this provider is selected

        self._anthropic = anthropic
        self.name = PROVIDER_ANTHROPIC
        self.model = model or DEFAULT_MODELS[PROVIDER_ANTHROPIC]
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    def generate_structured(
        self, *, system: str, user: str, schema: type[SchemaT]
    ) -> SchemaT:
        logger.info("Calling Claude %s for structured output", self.model)
        with self._errors():
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                thinking={"type": "adaptive"},
            )
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(self._explain_empty_parse(response))
        return parsed

    def _explain_empty_parse(self, response) -> str:
        """Turn an empty parse into something the user can act on.

        A refusal is the subtle one: it arrives as HTTP 200 with
        ``stop_reason="refusal"`` rather than as an exception, so code that
        reads ``parsed_output`` without checking ``stop_reason`` just sees
        nothing and reports a generic failure. The server-side ``fallbacks``
        parameter that would re-route a refusal automatically is beta-gated and
        ``messages.parse`` accepts no ``betas``, so it is handled here instead.
        """
        reason = getattr(response, "stop_reason", None)
        where = f"Claude {self.model}"
        if reason == "refusal":
            detail = getattr(response, "stop_details", None)
            category = getattr(detail, "category", None) if detail else None
            suffix = f" (category: {category})" if category else ""
            return (
                f"{where} declined this request{suffix}. Rephrase the GIS request, "
                f"or set GIS_PLANNER_MODEL to a different model."
            )
        if reason == "max_tokens":
            return (
                f"{where} hit the {MAX_TOKENS}-token ceiling before finishing the "
                f"plan. Ask for a smaller piece of work, or raise MAX_TOKENS."
            )
        if reason == "model_context_window_exceeded":
            return (
                f"{where} ran out of context. The QGIS project likely has too many "
                f"layers to describe in one prompt; narrow the request."
            )
        return f"{where} returned no parsed output (stop_reason={reason!r})"

    def complete_text(self, *, system: str, user: str) -> str:
        logger.info("Calling Claude %s for text completion", self.model)
        with self._errors():
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
            )
        # With thinking on, content holds thinking blocks as well as text ones.
        return "".join(b.text for b in response.content if b.type == "text")

    def _errors(self):
        return _translate_errors(
            self.name,
            self.model,
            not_found=self._anthropic.NotFoundError,
            rate_limit=self._anthropic.RateLimitError,
            auth=self._anthropic.AuthenticationError,
            status=self._anthropic.APIStatusError,
            connection=self._anthropic.APIConnectionError,
        )


class OpenAIProvider:
    """OpenAI, via the official ``openai`` SDK.

    Retained as an alternative to Claude. Uses Structured Outputs
    (``beta.chat.completions.parse``) for the schema path.
    """

    def __init__(self, model: str | None = None) -> None:
        import openai  # local: only needed when this provider is selected

        self._openai = openai
        self.name = PROVIDER_OPENAI
        self.model = model or DEFAULT_MODELS[PROVIDER_OPENAI]
        self._client = openai.OpenAI()  # reads OPENAI_API_KEY

    def generate_structured(
        self, *, system: str, user: str, schema: type[SchemaT]
    ) -> SchemaT:
        logger.info("Calling OpenAI %s for structured output", self.model)
        with self._errors():
            completion = self._client.beta.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=schema,
            )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise LLMError(f"OpenAI {self.model} returned no parsed output")
        return parsed

    def complete_text(self, *, system: str, user: str) -> str:
        logger.info("Calling OpenAI %s for text completion", self.model)
        with self._errors():
            completion = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        return completion.choices[0].message.content or ""

    def _errors(self):
        return _translate_errors(
            self.name,
            self.model,
            not_found=self._openai.NotFoundError,
            rate_limit=self._openai.RateLimitError,
            auth=self._openai.AuthenticationError,
            status=self._openai.APIStatusError,
            connection=self._openai.APIConnectionError,
        )


def build_provider(
    provider: str | None = None, model: str | None = None
) -> LLMProvider:
    """Construct the configured provider.

    Resolution order, most specific first: the ``provider`` argument, then
    ``GIS_PLANNER_PROVIDER``, then Claude. Likewise ``model`` then
    ``GIS_PLANNER_MODEL`` then the provider's default.

    Raises:
        LLMError: the provider name is not recognised.
    """
    raw = provider or os.environ.get("GIS_PLANNER_PROVIDER") or DEFAULT_PROVIDER
    key = raw.strip().lower()
    resolved = _PROVIDER_ALIASES.get(key)
    if resolved is None:
        known = ", ".join(sorted(set(_PROVIDER_ALIASES)))
        raise LLMError(f"Unknown GIS_PLANNER_PROVIDER {raw!r}. Expected one of: {known}")

    chosen_model = model or os.environ.get("GIS_PLANNER_MODEL") or None
    _warn_on_model_mismatch(resolved, chosen_model)

    if resolved == PROVIDER_ANTHROPIC:
        return AnthropicProvider(chosen_model)
    return OpenAIProvider(chosen_model)


def _warn_on_model_mismatch(provider: str, model: str | None) -> None:
    """Catch a stale ``GIS_PLANNER_MODEL`` left over from the other provider.

    The default provider changed from OpenAI to Claude, so an environment that
    still exports ``GIS_PLANNER_MODEL=gpt-4o`` would otherwise send that name to
    Anthropic and get an opaque 404.
    """
    if not model:
        return
    wrong = (provider == PROVIDER_ANTHROPIC and model.startswith("gpt")) or (
        provider == PROVIDER_OPENAI and model.startswith("claude")
    )
    if wrong:
        logger.warning(
            "GIS_PLANNER_MODEL=%r looks like a model for the other provider while "
            "GIS_PLANNER_PROVIDER resolves to %r. Unset GIS_PLANNER_MODEL to use "
            "the default (%s), or set the provider to match.",
            model,
            provider,
            DEFAULT_MODELS[provider],
        )


class _translate_errors:
    """Re-raise SDK exceptions as :class:`LLMError`, most specific first.

    Both SDKs expose the same error shape under different classes, so the
    engine never has to know which vendor it is talking to.
    """

    def __init__(self, provider, model, *, not_found, rate_limit, auth, status, connection):
        self._provider = provider
        self._model = model
        self._not_found = not_found
        self._rate_limit = rate_limit
        self._auth = auth
        self._status = status
        self._connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        where = f"{self._provider} model {self._model!r}"
        if isinstance(exc, self._not_found):
            raise LLMError(
                f"{where} was not found. Check the model name "
                f"(default: {DEFAULT_MODELS[self._provider]})."
            ) from exc
        if isinstance(exc, self._auth):
            raise LLMError(f"{where}: authentication failed. Check the API key.") from exc
        if isinstance(exc, self._rate_limit):
            raise LLMError(f"{where}: rate limited. Retry with backoff.") from exc
        if isinstance(exc, self._connection):
            raise LLMError(f"{where}: could not reach the API.") from exc
        if isinstance(exc, self._status):
            raise LLMError(f"{where}: API error: {exc}") from exc
        return False
