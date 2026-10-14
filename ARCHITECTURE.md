# GIS Assistant MCP — Architecture & Development Record

AI-powered GIS workflow automation via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), backed by QGIS. Translates natural-language requests into validated QGIS Processing plans, executes them step-by-step, and reports results — through MCP tools (Claude Code / any MCP client) **or** the built-in chat UI inside QGIS.

---

## System Architecture

```
                                    ┌─────────────────────────────────┐
Claude Code ──MCP/stdio──▶         │         GIS Engine              │
                            ┌──────┤                                 │
QGIS Chat UI ──HTTP/REST──▶│      │  ┌─────────┐   ┌────────────┐  │      ┌──────────────────┐
                            │      │  │ Planner │──▶│  Executor  │──┼─────▶│  QGIS Plugin     │
                            │      │  └────┬────┘   └──────┬─────┘  │      │  Socket Server   │
                            │      │       │               │        │      │  (TCP/NDJSON     │
                            │      │  [OpenAI API]    [Safety       │      │   port 9876)     │
                            │      │  Structured      Validation]   │      └──────────────────┘
                            │      │  Outputs                       │
                            └──────┤                                 │
                                    └─────────────────────────────────┘
```

**Data flow:** User prompt → transport layer → GISEngine → OpenAI planner (generates GISPlan) → safety validation (algorithm allowlist, path confinement) → PlanExecutor (resolves @-references, runs steps) → QGISClient (TCP/NDJSON commands) → QGIS plugin socket server → QGIS Processing algorithms → results returned up the chain.

Two transport layers share the same core engine:

| Transport | Entry Point | Protocol | Auth | Use Case |
|-----------|-------------|----------|------|----------|
| MCP/stdio | `gis-assistant` | JSON-RPC over stdio | None | Claude Code, any MCP client |
| HTTP API | `gis-assistant-api` | REST over HTTP | Bearer token | QGIS chat dock widget |

---

## How gis-assistant-mcp Extends qgis_mcp

The upstream [qgis_mcp](https://github.com/jjsantos01/qgis_mcp) project by Juan Santos provides:

- A **QGIS plugin socket server** (port 9876) that accepts NDJSON commands to control QGIS
- A **FastMCP server** (`qgis_mcp_server.py`) that exposes 14 tools (ping, add_layer, execute_processing, render_map, etc.) to Claude Desktop or any MCP client
- Direct, imperative control: each MCP tool maps 1:1 to a QGIS operation

**gis-assistant-mcp** builds a higher-level automation layer on top:

| Capability | qgis_mcp (upstream) | gis-assistant-mcp (this project) |
|------------|--------------------|---------------------------------|
| Interface | Imperative tools (one command at a time) | Natural-language prompts → multi-step plans |
| Planning | None — user writes each step | AI planner via OpenAI Structured Outputs |
| Safety | Code execution toggle | Algorithm allowlist (103 algorithms), path confinement, plan size limits, no execute_code |
| Execution | Single-command | Step-by-step with @-reference resolution, confirm/resume workflow |
| Error handling | Per-command | Fail-fast with diagnostics (`gis.diagnose`), resumable after confirm |
| Transports | MCP/stdio only | MCP/stdio + HTTP API (dual transport) |
| UI | External (Claude Desktop) | Built-in QGIS chat dock widget |
| Tests | None | 44 tests across 4 test modules |

---

## Core Components

### `engine.py` — GISEngine (362 lines)

Central orchestrator shared by both transports. All public methods return dicts; JSON serialization happens at the transport boundary.

- **Lazy initialization**: `_get_client()`, `_get_planner()`, `_get_executor()` create components on first use
- **Context gathering**: `_gather_context()` snapshots QGIS state (project info, layers, CRS) for the planner. Tolerates QGIS being unreachable (returns partial context with error info)
- **Thread safety**: Narrow `threading.Lock()` protects only shared mutable state (`_last_plan`, `_last_report`, `_pending_prompt`, `_pending_context`). Network calls (OpenAI, QGIS socket) run outside the lock to avoid deadlocks
- **Public API**: `plan()`, `answer()`, `run()`, `run_plan()`, `resume()`, `diagnose()`, `explain_last_run()`, `list_capabilities()`, `qgis_status()`

### `planner.py` — GISPlanner (64 lines)

Calls OpenAI's Structured Outputs API (`client.beta.chat.completions.parse()`) with `response_format=GISPlan` to generate validated plan objects from natural language + QGIS context.

- Model: `gpt-4o` (configurable via `GIS_PLANNER_MODEL` env var)
- Returns `GISPlan` with status `"ready"` or `"needs_clarification"` (with questions for the user)
- Clarification answers are fed back in via `answers` parameter for re-planning

### `executor.py` — PlanExecutor (375 lines)

Steps through a validated GISPlan sequentially, executing each operation via the QGIS socket client.

- **@-reference resolution**: `@input.<name>` → input path, `@output.<name>` → output directory + name.format, `@step.<id>` → prior step's output. Supports nested resolution in dicts and lists
- **Dispatch table**: Maps `StepOperation` enum to handler methods (`_exec_scan_files`, `_exec_add_layer`, `_exec_processing`, `_exec_remove_layer`, `_exec_zoom`, `_exec_render`, `_exec_confirm`)
- **Confirm/resume**: `_exec_confirm` returns a `blocked` status with a UUID resume token. `PendingExecution` dataclass preserves full state (plan, ref_map, results, step index) for resumption
- **Fail-fast**: Stops on first error. Returns `RunReport` with step results, outputs created, and warnings

### `qgis_client.py` — QGISClient (216 lines)

TCP socket wrapper using NDJSON framing (newline-delimited JSON, protocol version 1, 10 MB max line length).

- **Commands**: ping, get_qgis_info, get_project_info, get_layers, get_layer_features, add_vector_layer, add_raster_layer, remove_layer, zoom_to_layer, execute_processing, render_map, save_project, load_project
- **Layer resolution**: Tries exact ID → source path (normalized, handles GPKG pipe suffixes) → name match. Raises `AmbiguousLayerError` if a name matches multiple layers
- **Local helper**: `scan_directory()` (static method, no socket) lists files by extension with configurable limit

### `safety.py` — Safety Validation (198 lines)

- **Algorithm allowlist**: 90 approved `native:` and `gdal:` processing algorithms
- **Synonym resolution**: Maps near-misses (e.g. `native:reproject` → `native:reprojectlayer`)
- **Path confinement**: All output paths resolved and validated to stay within the workspace directory. Detects symlink escapes
- **Plan limits**: Max 20 steps, max 200 scan files, max 100 features
- **Overwrite detection**: `check_overwrite()` flags existing files for confirm steps

### `schemas.py` — Pydantic Models (179 lines)

- **GISPlan**: Complete workflow plan (status, questions, inputs, outputs, assumptions, steps)
- **PlanStep**: Single step with operation type, algorithm, parameters (JSON string for OpenAI strict mode compatibility), layer paths, render settings
- **RunReport**: Execution results (resume token, step results, outputs created, warnings, success/blocked status)
- **Supporting models**: ClarificationQuestion, InputLayer, OutputFile, PlanOutputs, Assumption, StepResult

### `server.py` — MCP/stdio Transport (157 lines)

Thin FastMCP wrappers around a global `GISEngine` instance. Each of the 8 tools calls an engine method and `json.dumps()` the result. Lifespan manages `startup()`/`shutdown()`.

### `api.py` — HTTP API Transport (238 lines)

Starlette HTTP API with 10 REST endpoints. Bearer token auth (auto-generated if `GIS_API_TOKEN` not set, printed to stdout for plugin capture). Uses `anyio.to_thread.run_sync()` to offload blocking engine calls without freezing the async event loop. Consistent error envelopes: `{"status": "error", "message": "...", "details": {...}}`. Host fixed to `127.0.0.1` (no external access).

### `prompts.py` — LLM System Prompts (158 lines)

10-rule system prompt governing plan generation: status handling, input/output naming, @-reference syntax, algorithm IDs, parameter encoding (JSON string with integer enums), safety constraints, CRS/unit awareness for specific algorithms (contour intervals, hillshade Z_FACTOR, buffer distance units). Separate diagnostic prompt for `gis.diagnose`.

---

## QGIS Chat Widget

Two new files added to the qgis_mcp plugin provide a complete chat interface inside QGIS.

### `chat_widget.py` — QgisChatDockWidget (631 lines)

Dock widget with full chat UI and subprocess engine management.

**UI layout** (top to bottom):
1. Dual status indicators (API: green/red, QGIS Runtime: green/red)
2. Connection settings (URL, token, engine directory, OpenAI key)
3. Engine controls (Start/Stop Engine, Connect)
4. Mode selector (Plan Only / Plan + Execute)
5. Chat history (QTextBrowser with styled HTML)
6. Interactive frame (clarification radio buttons/text inputs, confirm approve/decline buttons)
7. Message input area + Send button

**Engine subprocess management**:
- Launches `gis-assistant-api` via `QProcess` using `uv --directory <path> run gis-assistant-api`
- Injects `OPENAI_API_KEY` via `QProcessEnvironment`
- Captures API token from stdout (`GIS_API_TOKEN=<token>` line)
- Graceful shutdown: `terminate()` → 3s wait → `kill()` if needed

**Response rendering**:
- Plans: status, assumptions, steps, outputs with collapsible raw JSON
- Reports: success/failure per step with status icons, file outputs, warnings
- Clarification: dynamic radio button groups or text inputs per question
- Confirm: styled Approve/Decline buttons with resume token handling

### `chat_client.py` — GISChatClient (155 lines)

Non-blocking HTTP client using Qt's `QNetworkAccessManager`.

**Four signals**:
- `response_received(endpoint, data)` — successful API responses
- `request_failed(endpoint, http_status, error_msg)` — HTTP errors with parsed error body
- `connection_changed(connected)` — API reachability changes
- `qgis_status_changed(connected)` — QGIS runtime reachability via engine socket

**Heartbeat**: 10-second timer pings `/health` and `/qgis_status`. Health/status responses update status lights silently (no chat messages). Connection failures emit `connection_changed(False)` only once.

---

## Key Design Decisions

**Dicts at engine boundary.** GISEngine methods return plain dicts. JSON serialization (`json.dumps()`) happens only in the transport wrappers (MCP tools, HTTP endpoints). This avoids double parse/serialize and makes the engine directly testable.

**Narrow lock scope.** The engine's `threading.Lock()` protects only shared mutable state mutations (4 fields). Network calls to OpenAI and the QGIS socket run outside the lock, preventing deadlocks when long-running requests overlap with heartbeat checks.

**Parameters as JSON string.** OpenAI's Structured Outputs strict mode requires `additionalProperties: false` on all object schemas. A free-form `dict[str, object]` for algorithm parameters can't satisfy this constraint. The solution: `parameters` is a `str` field containing a JSON-encoded object. The planner outputs the JSON string; the executor calls `parsed_parameters()` to get the dict. The system prompt explicitly documents this encoding and warns about integer enum requirements.

**QNetworkAccessManager for non-blocking UI.** All HTTP communication from the QGIS plugin uses Qt's native async networking. No threads, no `asyncio`, no event loop conflicts. Requests fire immediately; results arrive via Qt signals on the main thread.

**Token exchange via stdout.** The HTTP API prints `GIS_API_TOKEN=<token>` as its first line. The chat widget's `QProcess.readyReadStandardOutput` signal captures this, auto-configures the client, and starts the heartbeat. No files or IPC needed.

**Resilient context gathering.** If the QGIS socket is unreachable when the planner needs context, `_gather_context()` returns partial context with error annotations instead of raising. Planning can still proceed (with degraded quality) rather than failing outright.

---

## Safety Model

| Layer | Mechanism | Scope |
|-------|-----------|-------|
| Algorithm allowlist | 90 approved `native:` and `gdal:` algorithms | Blocks arbitrary processing |
| Path confinement | All outputs resolved and validated within workspace dir | Prevents writes outside workspace |
| No execute_code | Intentionally excluded from gis-assistant operations | Blocks arbitrary Python |
| Plan size limits | Max 20 steps, max 200 scan files, max 100 features | Bounds resource usage |
| Confirm steps | `confirm` operation blocks execution with resume token | User approves before overwrites |
| Bearer token auth | HTTP API requires `Authorization: Bearer <token>` | Prevents unauthorized access |
| Localhost binding | HTTP API binds to `127.0.0.1` only (no external access) | Network isolation |
| No CORS | Starlette default — CORS disabled | Blocks cross-origin browser requests |

---

## Development Contributions

### Upstream: qgis_mcp

**Original project** by [Juan Santos](https://github.com/jjsantos01) (March 2025), based on [BlenderMCP](https://github.com/ahujasid/blender-mcp) by [Siddharth Ahuja](https://x.com/sidahuj).

- QGIS plugin with socket server (NDJSON protocol, 20+ command handlers)
- FastMCP server with 14 tools for Claude Desktop integration
- Project/layer manipulation, processing algorithm execution, code execution, map rendering

### This project: gis-assistant-mcp

Original work, written from scratch. It is a separate package rather than a fork:
it holds no PyQGIS imports and reaches QGIS only across the plugin's socket
protocol. The one borrowed piece is the socket client, whose `send_command` names
necessarily match the plugin's wire protocol — 12 of its 113 substantive lines
coincide with upstream for that reason, and the file says so in its docstring.

| Module | Lines | Description |
|--------|-------|-------------|
| `engine.py` | 362 | Core GISEngine orchestrator with thread-safe state management |
| `executor.py` | 377 | Step-by-step plan execution with @-reference resolution and confirm/resume |
| `api.py` | 238 | Starlette HTTP API with bearer token auth and 10 REST endpoints |
| `qgis_client.py` | 216 | TCP/NDJSON socket client with layer resolution and reconnection |
| `safety.py` | 198 | Algorithm allowlist (103 algorithms), path confinement, plan limits |
| `schemas.py` | 187 | Pydantic models for GISPlan, RunReport, and supporting types |
| `prompts.py` | 168 | LLM system prompts with CRS/unit awareness and parameter type guidance |
| `server.py` | 157 | MCP/stdio transport — thin wrappers around GISEngine |
| `planner.py` | 64 | Structured-output LLM integration for plan generation |
| **Source total** | **1,967** | |
| `test_executor.py` | 301 | 13 tests: confirm/resume, reference resolution, error handling |
| `test_safety.py` | 114 | 15 tests: algorithm validation, path escape prevention |
| `test_qgis_client.py` | 129 | 11 tests: layer resolution, directory scanning |
| `test_schemas.py` | 106 | 6 tests: model validation, serialization round-trips |
| **Test total** | **650** | **45 tests** |

Counts above were verified with `pytest -q` and `wc -l src/gis_assistant/*.py`.
Re-run those rather than trusting this table.

**QGIS plugin additions** (in qgis_mcp fork):

| File | Lines | Description |
|------|-------|-------------|
| `chat_widget.py` | 631 | Chat dock with QProcess engine management, interactive UI |
| `chat_client.py` | 155 | QNetworkAccessManager HTTP client with dual-status signals |
| `qgis_mcp_plugin.py` | +91 | Plugin integration: chat_action, toggle_chat_dock, cleanup |
| **Plugin additions** | **~877** | |

**Protocol & safety improvements** to qgis_mcp:

- NDJSON framing with `"v": 1` protocol version field
- 10 MB buffer limit (prevents DoS)
- Code execution disabled by default with explicit toggle
- Comprehensive error handling with traceback capture

**Documentation**: Updated both project READMEs with architecture diagrams, HTTP API endpoint tables, chat UI setup instructions, and safety documentation.

---

## Project Statistics

### Line Counts

| Category | Lines |
|----------|-------|
| gis-assistant-mcp source | 1,947 |
| gis-assistant-mcp tests | 636 |
| QGIS plugin additions | ~877 |
| **Total new code** | **~3,460** |

### Test Coverage

| Test Module | Tests | Covers |
|-------------|-------|--------|
| test_executor.py | 18 | Confirm/resume flow, @-reference resolution, error handling, ambiguous layers |
| test_safety.py | 14 | Algorithm allowlist, path escape prevention (including symlinks), plan size limits |
| test_qgis_client.py | 12 | Layer resolution strategies, GPKG pipe handling, directory scanning |
| test_schemas.py | 5 | GISPlan validation, PlanStep processing, RunReport round-trip serialization |
| **Total** | **44** | |

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| mcp[cli] | >=1.3.0 | Model Context Protocol SDK |
| openai | >=1.30.0 | OpenAI Structured Outputs API |
| pydantic | >=2.0.0 | Data validation and schema generation |
| starlette | >=0.37.0 | HTTP API framework |
| anyio | >=4.0 | Async-to-sync bridge for blocking engine calls |
| uvicorn | >=0.30.0 | ASGI server for Starlette |
| pytest | >=8.0 | Test framework (dev only) |

**Python**: 3.10+
**License**: MIT
