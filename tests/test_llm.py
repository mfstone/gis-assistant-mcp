"""Provider selection and error translation.

No network and no real credentials: providers are constructed against dummy
API keys, and the SDK clients are replaced with stubs where a call is needed.
"""

from __future__ import annotations

import importlib.util
import logging

import pytest
from pydantic import BaseModel

from gis_assistant.llm import (
    DEFAULT_MODELS,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    AnthropicProvider,
    LLMError,
    LLMProvider,
    OpenAIProvider,
    build_provider,
)

PLANNER_ENV = ("GIS_PLANNER_PROVIDER", "GIS_PLANNER_MODEL")

#: OpenAI is an optional extra, so a Claude-only install must still pass the
#: suite. These skips are what prove the extra really is optional.
requires_openai = pytest.mark.skipif(
    importlib.util.find_spec("openai") is None,
    reason="openai extra not installed (uv sync --extra openai)",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from a known environment with dummy credentials."""
    for var in PLANNER_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dummy")


class Tiny(BaseModel):
    value: str


# --------------------------------------------------------------------------
# provider selection
# --------------------------------------------------------------------------


def test_defaults_to_claude():
    provider = build_provider()
    assert provider.name == PROVIDER_ANTHROPIC
    assert provider.model == "claude-opus-5"


@requires_openai
def test_env_selects_openai(monkeypatch):
    monkeypatch.setenv("GIS_PLANNER_PROVIDER", "openai")
    provider = build_provider()
    assert provider.name == PROVIDER_OPENAI
    assert provider.model == DEFAULT_MODELS[PROVIDER_OPENAI]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("anthropic", PROVIDER_ANTHROPIC),
        ("claude", PROVIDER_ANTHROPIC),
        ("  Claude  ", PROVIDER_ANTHROPIC),
        ("ANTHROPIC", PROVIDER_ANTHROPIC),
        pytest.param("openai", PROVIDER_OPENAI, marks=requires_openai),
        pytest.param("chatgpt", PROVIDER_OPENAI, marks=requires_openai),
        pytest.param("gpt", PROVIDER_OPENAI, marks=requires_openai),
    ],
)
def test_provider_aliases(monkeypatch, value, expected):
    monkeypatch.setenv("GIS_PLANNER_PROVIDER", value)
    assert build_provider().name == expected


def test_unknown_provider_names_the_valid_options(monkeypatch):
    monkeypatch.setenv("GIS_PLANNER_PROVIDER", "gemini")
    with pytest.raises(LLMError) as exc:
        build_provider()
    assert "gemini" in str(exc.value)
    assert "anthropic" in str(exc.value)


def test_argument_beats_environment(monkeypatch):
    monkeypatch.setenv("GIS_PLANNER_PROVIDER", "openai")
    assert build_provider(provider="claude").name == PROVIDER_ANTHROPIC


# --------------------------------------------------------------------------
# model resolution
# --------------------------------------------------------------------------


def test_env_overrides_model(monkeypatch):
    monkeypatch.setenv("GIS_PLANNER_MODEL", "claude-sonnet-5")
    assert build_provider().model == "claude-sonnet-5"


def test_model_argument_beats_env(monkeypatch):
    monkeypatch.setenv("GIS_PLANNER_MODEL", "claude-sonnet-5")
    assert build_provider(model="claude-haiku-4-5").model == "claude-haiku-4-5"


def test_stale_openai_model_warns_instead_of_failing_opaquely(monkeypatch, caplog):
    """The default provider changed, so a left-over GIS_PLANNER_MODEL=gpt-4o
    would otherwise reach Anthropic and come back as a bare 404."""
    monkeypatch.setenv("GIS_PLANNER_MODEL", "gpt-4o")
    with caplog.at_level(logging.WARNING, logger="gis_assistant.llm"):
        provider = build_provider()
    assert provider.name == PROVIDER_ANTHROPIC
    assert "looks like a model for the other provider" in caplog.text


@requires_openai
def test_claude_model_against_openai_also_warns(monkeypatch, caplog):
    monkeypatch.setenv("GIS_PLANNER_PROVIDER", "openai")
    monkeypatch.setenv("GIS_PLANNER_MODEL", "claude-opus-5")
    with caplog.at_level(logging.WARNING, logger="gis_assistant.llm"):
        build_provider()
    assert "looks like a model for the other provider" in caplog.text


def test_matching_model_does_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("GIS_PLANNER_MODEL", "claude-opus-4-8")
    with caplog.at_level(logging.WARNING, logger="gis_assistant.llm"):
        build_provider()
    assert "other provider" not in caplog.text


# --------------------------------------------------------------------------
# both providers satisfy the Protocol
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [AnthropicProvider, pytest.param(OpenAIProvider, marks=requires_openai)],
)
def test_satisfies_protocol(factory):
    assert isinstance(factory(), LLMProvider)


# --------------------------------------------------------------------------
# structured output handling
# --------------------------------------------------------------------------


class _FakeParsed:
    def __init__(self, parsed_output, stop_reason="end_turn"):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def test_anthropic_returns_parsed_output():
    provider = AnthropicProvider()
    provider._client.messages = _FakeMessages(_FakeParsed(Tiny(value="ok")))
    result = provider.generate_structured(system="s", user="u", schema=Tiny)
    assert result.value == "ok"


def test_anthropic_passes_schema_and_adaptive_thinking():
    provider = AnthropicProvider()
    fake = _FakeMessages(_FakeParsed(Tiny(value="ok")))
    provider._client.messages = fake
    provider.generate_structured(system="sys", user="usr", schema=Tiny)

    sent = fake.calls[0]
    assert sent["output_format"] is Tiny
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["model"] == "claude-opus-5"
    assert sent["system"] == "sys"
    # system prompt goes in `system`, never as a messages entry
    assert [m["role"] for m in sent["messages"]] == ["user"]


def test_refusal_is_reported_as_a_refusal():
    """A declined request arrives as HTTP 200 with stop_reason="refusal" and an
    empty parse -- not as an exception. Without this branch it would surface as
    a generic "no parsed output" and look like a bug in the schema."""
    provider = AnthropicProvider()
    provider._client.messages = _FakeMessages(_FakeParsed(None, stop_reason="refusal"))
    with pytest.raises(LLMError) as exc:
        provider.generate_structured(system="s", user="u", schema=Tiny)
    message = str(exc.value)
    assert "declined" in message
    assert "GIS_PLANNER_MODEL" in message  # tells the user what they can change


def test_refusal_includes_category_when_present():
    provider = AnthropicProvider()
    response = _FakeParsed(None, stop_reason="refusal")
    response.stop_details = type("D", (), {"category": "cyber"})()
    provider._client.messages = _FakeMessages(response)
    with pytest.raises(LLMError) as exc:
        provider.generate_structured(system="s", user="u", schema=Tiny)
    assert "cyber" in str(exc.value)


@pytest.mark.parametrize(
    "stop_reason,expected_phrase",
    [
        ("max_tokens", "token ceiling"),
        ("model_context_window_exceeded", "ran out of context"),
        ("end_turn", "no parsed output"),
        (None, "no parsed output"),
    ],
)
def test_empty_parse_explains_each_stop_reason(stop_reason, expected_phrase):
    provider = AnthropicProvider()
    provider._client.messages = _FakeMessages(_FakeParsed(None, stop_reason=stop_reason))
    with pytest.raises(LLMError) as exc:
        provider.generate_structured(system="s", user="u", schema=Tiny)
    assert expected_phrase in str(exc.value)


# --------------------------------------------------------------------------
# text completion skips thinking blocks
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _FakeCreate:
    def __init__(self, content):
        self._content = content

    def create(self, **kwargs):
        return type("R", (), {"content": self._content})()


def test_complete_text_ignores_thinking_blocks():
    """With adaptive thinking on, content carries thinking blocks too;
    concatenating everything would leak reasoning into the diagnosis."""
    provider = AnthropicProvider()
    provider._client.messages = _FakeCreate(
        [
            _Block("thinking", "internal reasoning"),
            _Block("text", "The CRS is geographic; "),
            _Block("text", "reproject before measuring area."),
        ]
    )
    out = provider.complete_text(system="s", user="u")
    assert out == "The CRS is geographic; reproject before measuring area."
    assert "internal reasoning" not in out


# --------------------------------------------------------------------------
# error translation
# --------------------------------------------------------------------------


class _RaisingMessages:
    def __init__(self, exc):
        self._exc = exc

    def parse(self, **kwargs):
        raise self._exc


def _api_error(cls, message="nope", status=404):
    """Build a real SDK exception; its constructor dereferences response.request."""
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return cls(message, response=response, body=None)


def test_not_found_names_the_default_model():
    import anthropic

    provider = AnthropicProvider(model="claude-does-not-exist")
    provider._client.messages = _RaisingMessages(
        _api_error(anthropic.NotFoundError)
    )
    with pytest.raises(LLMError) as exc:
        provider.generate_structured(system="s", user="u", schema=Tiny)
    message = str(exc.value)
    assert "claude-does-not-exist" in message
    assert DEFAULT_MODELS[PROVIDER_ANTHROPIC] in message


@pytest.mark.parametrize(
    "exc_name,status,expected_phrase",
    [
        ("AuthenticationError", 401, "authentication failed"),
        ("RateLimitError", 429, "rate limited"),
        ("InternalServerError", 500, "API error"),
    ],
)
def test_status_errors_translate_to_actionable_messages(exc_name, status, expected_phrase):
    import anthropic

    provider = AnthropicProvider()
    provider._client.messages = _RaisingMessages(
        _api_error(getattr(anthropic, exc_name), status=status)
    )
    with pytest.raises(LLMError) as exc:
        provider.generate_structured(system="s", user="u", schema=Tiny)
    assert expected_phrase in str(exc.value)


def test_connection_error_translates():
    import anthropic
    import httpx2

    provider = AnthropicProvider()
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    provider._client.messages = _RaisingMessages(
        anthropic.APIConnectionError(request=request)
    )
    with pytest.raises(LLMError) as exc:
        provider.generate_structured(system="s", user="u", schema=Tiny)
    assert "could not reach the API" in str(exc.value)


def test_unexpected_error_is_not_swallowed():
    """Only known SDK errors are translated; a bug must surface as itself."""
    provider = AnthropicProvider()
    provider._client.messages = _RaisingMessages(ZeroDivisionError("boom"))
    with pytest.raises(ZeroDivisionError):
        provider.generate_structured(system="s", user="u", schema=Tiny)
