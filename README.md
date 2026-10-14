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
                                    ├── OpenAI (plan generation)
                                    └── Local safety validation
```

Two transport options share the same core engine:

- **MCP/stdio** (`gis-assistant`): For Claude Code or any MCP client. Runs as a stdio server.
- **HTTP API** (`gis-assistant-api`): For the QGIS chat dock widget. Runs as a local HTTP server on port 8100 with bearer token auth.

Both transports call the same `GISEngine` class, which generates plans via OpenAI Structured Outputs, validates them against an algorithm allowlist and path confinement rules, then executes each step by sending commands to the QGIS MCP plugin over a TCP socket.

## Prerequisites

- **QGIS 3.x** with the [qgis_mcp plugin](https://github.com/jjsantos01/qgis_mcp)
  by Juan Santos installed and running — this project is a separate MCP server that
  drives that plugin over its socket protocol; see [Acknowledgments](#acknowledgments)
- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** package manager
- **OpenAI API key** (`OPENAI_API_KEY` environment variable)

## Installation

```bash
cd /path/to/gis-assistant-mcp
uv sync
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | *(required)* | OpenAI API key for plan generation |
| `GIS_PLANNER_MODEL` | `gpt-4o` | OpenAI model for planning and diagnostics |
| `GIS_WORKSPACE` | `~/gis-workspace` | Output directory (all outputs confined here) |
| `GIS_API_PORT` | `8100` | Port for the HTTP API server |
| `GIS_API_TOKEN` | *(auto-generated)* | Bearer token for HTTP API auth. If unset, a random token is generated at startup and printed to stdout. |

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
- **Algorithm allowlist**: Only approved `native:` and `gdal:` processing algorithms can run (~90 algorithms).
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

## License

MIT License. See [LICENSE](LICENSE) for details.
