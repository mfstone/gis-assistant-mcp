# gis-assistant-mcp

AI-powered GIS workflow automation via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), backed by QGIS.

Translates natural-language requests into validated QGIS Processing plans, executes them step-by-step, and reports results — through MCP tools (Claude Code / any MCP client) **or** the built-in chat UI inside QGIS.

## Architecture

```
                                                  ┌──────────────────┐
Claude Code  ──MCP/stdio──▶                       │                  │
                            gis-assistant engine ──┤ TCP/NDJSON ──▶ QGIS Plugin
QGIS Chat UI ──HTTP────────▶                      │                  │
                                    │              └──────────────────┘
                                    ├── Claude or OpenAI (plan generation)
                                    └── Local safety validation
```

Two transport options share the same core engine:

- **MCP/stdio** (`gis-assistant`): For Claude Code or any MCP client. Runs as a stdio server.
- **HTTP API** (`gis-assistant-api`): For the QGIS chat dock widget. Runs as a local HTTP server on port 8100 with bearer token auth.

Both transports call the same `GISEngine` class, which generates plans through a provider's structured-output API, validates them against an algorithm allowlist and path confinement rules, then executes each step by sending commands to the QGIS MCP plugin over a TCP socket.

## Prerequisites

- **QGIS 3.x** with the [qgis_mcp plugin](https://github.com/jjsantos01/qgis_mcp)
  by Juan Santos installed and running — this project is a separate MCP server that
  drives that plugin over its socket protocol; see [Acknowledgments](#acknowledgments)
- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** package manager
- **`ANTHROPIC_API_KEY`** — Claude is the default provider. (OpenAI is an
  optional alternative; see [Choosing a provider](#choosing-a-provider).)

## Installation

```bash
cd /path/to/gis-assistant-mcp
uv sync                      # Claude (default)
uv sync --extra openai       # ...or add the optional OpenAI provider
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GIS_PLANNER_PROVIDER` | `anthropic` | `anthropic` (aliases: `claude`) or `openai` (aliases: `chatgpt`, `gpt`) |
| `ANTHROPIC_API_KEY` | *(required for Claude)* | Read by the Anthropic SDK |
| `OPENAI_API_KEY` | *(required for OpenAI)* | Read by the OpenAI SDK. Needs the `openai` extra installed |
| `GIS_PLANNER_MODEL` | per provider | Overrides the provider default — `claude-opus-5` or `gpt-4o`. Must match the selected provider; a mismatch is logged as a warning |
| `GIS_WORKSPACE` | `~/gis-workspace` | Output directory (all outputs confined here) |
| `GIS_API_PORT` | `8100` | Port for the HTTP API server |
| `GIS_API_TOKEN` | *(auto-generated)* | Bearer token for HTTP API auth. If unset, a random token is generated at startup and printed to stdout. |

### Choosing a provider

Claude (`claude-opus-5`) is the default and needs no extra install. To use OpenAI
instead:

```bash
uv sync --extra openai
export GIS_PLANNER_PROVIDER=openai OPENAI_API_KEY=...
```

Both providers go through the same two-method seam in `llm.py`, so the engine
holds no vendor-specific code and switching changes no behaviour beyond the model.

**On token usage.** Adaptive thinking is enabled on both Claude call paths. Plan
generation genuinely needs it — the model has to reason about CRS and units,
choose among 103 allowed algorithms, and chain one step's outputs into the next —
but every request then spends reasoning tokens on top of the response, and that
applies to error diagnosis too, not just planning.

This server calls the **Anthropic API** with an `ANTHROPIC_API_KEY`, billed per
token. A Claude Pro or Max subscription covers Claude apps such as Claude Code and
claude.ai, but not API usage — the two are separate and metered differently.

Measured input for a typical request is ~1,900 tokens (system prompt 1,621 + a
six-layer project context). With output and thinking estimated at 1.8–5.8k tokens:

| Model | Cost per plan | Plans per $1 |
|-------|---------------|--------------|
| `claude-opus-5` (default) | $0.05 – $0.15 | 6 – 18 |
| `claude-sonnet-5` | ~$0.03 | ~32 |
| `claude-haiku-4-5` | ~$0.02 | ~63 |

To spend less: set `GIS_PLANNER_MODEL` to a cheaper model, or drop `thinking` from
`AnthropicProvider` in `src/gis_assistant/llm.py`.

### Running without an API key

Only plan *generation* needs a model. The execution engine does not, and because
the planner is constructed lazily, no key is read unless you call a planning tool:

| Needs a key | No key required |
|-------------|-----------------|
| `gis.plan`, `gis.answer`, `gis.run`, `gis.diagnose` | `gis.run_plan`, `gis.resume`, `gis.list_capabilities`, `gis.explain_last_run`, `gis.qgis_status` |

So an MCP client that is *already* a model — Claude Code, for instance — can skip
the built-in planner entirely: call `gis.list_capabilities` to learn the allowed
operations and limits, author the plan JSON itself, then hand it to `gis.run_plan`.
Safety validation, path confinement, @-reference resolution and the confirm/resume
gate all still apply, because they live in the executor rather than the planner.
The internal planner exists for the QGIS chat dock, which has no model of its own.

The test suite needs no key and makes no network calls at all:

```bash
uv sync && uv run pytest -q     # 72 passed, 6 skipped
```

### MCP Client Setup (Claude Code)

Add to your `.mcp.json` (e.g. in your workspace root):

```json
{
  "mcpServers": {
    "gis_assistant": {
      "command": "uv",
      "args": ["--directory", "/path/to/gis-assistant-mcp", "run", "gis-assistant"],
      "env": {
        "GIS_WORKSPACE": "/path/to/your/workspace"
      }
    }
  }
}
```

### QGIS Chat UI Setup

No MCP client needed. Start the HTTP API and use the chat dock widget inside QGIS:

1. Start the QGIS MCP plugin socket server (Plugins > QGIS MCP > Start Server)
2. Open the chat dock: Plugins > QGIS MCP > GIS Assistant Chat
3. Click **Start Engine** — this launches the HTTP API as a subprocess and auto-connects
4. Type your GIS request in the chat input

Alternatively, start the API manually:

```bash
cd /path/to/gis-assistant-mcp
uv run gis-assistant-api
# Prints: GIS_API_TOKEN=<token>
```

Then paste the token into the chat dock's token field and click **Connect**.

## Tools

| Tool | Description |
|------|-------------|
| `gis.plan` | Generate a workflow plan from a natural-language prompt (no execution) |
| `gis.answer` | Resolve clarification questions from a previous `gis.plan` call |
| `gis.run` | Plan and execute a workflow end-to-end |
| `gis.run_plan` | Execute a specific plan JSON (for plan → review → edit → execute workflows) |
| `gis.resume` | Resume a blocked execution after a confirmation step |
| `gis.diagnose` | Diagnose a GIS/QGIS error and suggest fixes |
| `gis.explain_last_run` | Show details of the last plan and execution results |
| `gis.list_capabilities` | List supported operations, algorithms, and workspace policy |

## HTTP API Endpoints

When running `gis-assistant-api`, the following REST endpoints are available on `http://127.0.0.1:8100`. All endpoints (except `/health`) require `Authorization: Bearer <token>`.

| Method | Path | Body | Description |
|--------|------|------|-------------|
| GET | `/health` | - | Health check (no auth) |
| GET | `/qgis_status` | - | Check if QGIS socket is reachable |
| POST | `/plan` | `{"prompt": "..."}` | Generate a workflow plan |
| POST | `/answer` | `{"answers": {...}}` | Resolve clarification questions |
| POST | `/run` | `{"prompt": "..."}` | Plan and execute end-to-end |
| POST | `/run_plan` | `{"plan": {...}}` | Execute a specific plan dict |
| POST | `/resume` | `{"token": "...", "approved": bool}` | Resume after confirmation |
| POST | `/diagnose` | `{"error_text": "..."}` | Diagnose an error |
| GET | `/capabilities` | query: `?verbose=true` | List supported algorithms |
| GET | `/last_run` | - | Show last plan/report |

Error responses use a consistent envelope: `{"status": "error", "message": "...", "details": {...}}`.

## Safety

- **No `execute_code`**: Arbitrary Python execution is intentionally excluded.
- **Algorithm allowlist**: Only approved `native:` and `gdal:` processing algorithms can run (103 algorithms).
- **Path confinement**: All output paths are resolved and validated to stay within the workspace directory.
- **Confirmation steps**: Plans include `confirm` steps before overwriting existing files, which halt execution until approved via `gis.resume`.
- **Plan size limits**: Maximum 20 steps per plan.
- **Main-thread processing**: The QGIS plugin executes processing algorithms on the main thread. Large raster operations (warps, merges, hillshades on multi-GB data) will freeze the QGIS UI. Keep operations scoped to smaller datasets, or run heavy jobs directly from the QGIS Processing Toolbox.

## Packaging

To create a clean distribution zip (excludes `__pycache__`, `.venv`, `.git`, caches):

```bash
./scripts/build_dist.sh
```

This produces `dist/gis-assistant-mcp.zip` including `uv.lock` for reproducible installs. Without `uv.lock`, `uv sync` resolves latest allowed versions which may drift.

## Development

```bash
uv sync --group dev   # install test dependencies
uv run pytest tests/ -v
```

## Acknowledgments

The QGIS socket client in this project is adapted from [qgis_mcp](https://github.com/jjsantos01/qgis_mcp) by [Juan Santos](https://github.com/jjsantos01), which is itself based on [BlenderMCP](https://github.com/ahujasid/blender-mcp) by [Siddharth Ahuja](https://x.com/sidahuj).

This is not a fork of qgis_mcp. It is a separate MCP server that drives the qgis_mcp QGIS plugin across that plugin's socket protocol — 12 of `qgis_client.py`'s 113 substantive lines coincide with upstream, all of them `send_command` names fixed by the wire protocol. See [NOTICE](NOTICE) for the retained copyright notices and [ARCHITECTURE.md](ARCHITECTURE.md#development-contributions) for a capability comparison.

## License

MIT License. See [LICENSE](LICENSE); third-party copyright notices are retained in [NOTICE](NOTICE).
