# AGENTS

## Repository contract

This repository is the pytest-side client for RevitDevTool host testing. It
owns local pytest collection, host discovery/launch, suite leasing, direct MCP
connection, and conversion of returned case data into pytest reports. It does
not own host execution or relay through `DevTools.Daemon` or McpGateway.

```text
pytest collection -> direct named-pipe MCP session
  -> DevTools_{Host}_{Version}_{PID} -> pytest_run -> host Python runtime
```

The canonical pipe is strict: four segments, literal `DevTools`, nonblank host
and version without `_`, and a positive PID. `pipe_name.py` validates that
identity before connection; `mcp_client.py` validates initialized server
metadata against it. One host pipe supports multiple independent MCP sessions.

## Module map

| Module | Responsibility |
|---|---|
| `plugin.py` | pytest hooks, options, lifecycle, and local collection/report flow |
| `connection.py` | discovery, connection, leasing, and launch orchestration |
| `pipe_name.py` | canonical pipe identity and prefix-filtered Windows enumeration |
| `named_pipe_transport.py` | byte-stream adapter and case-event decoding |
| `mcp_client.py` | session initialize, `pytest_run`, metadata validation, cancellation |
| `models.py` | pytest tool request/result domain models |
| `reporting.py` | map host `CaseResult` values to pytest reports |
| `discovery.py` | host registry lookup and process launch |

`pytest_run` receives local workspace/test roots, node IDs, and pytest
arguments. Domain test outcomes return as `RunResponse`; stable MCP tool errors
represent infrastructure failures. Standard MCP progress is request-token
scoped. Case events are optional and require the negotiated
`experimental.devtools.pytest.caseEvents.version = "1"` capability. Ctrl+C or
a deadline cancels only this MCP session after its grace period; never kill a
host process.

## Verification

```powershell
# Pure unit/contract tests (no live host)
uv run pytest tests/unit -q

# Full plugin suite when a configured host is available
uv run pytest

# Explicit live host selection
uv run pytest --host revit --host-version 2025
```

Report unavailable Revit/AutoCAD/Python environment as a live-test blocker;
do not replace it with a fake protocol assertion.

## Change rules

- Keep `pipe_name.py`, `named_pipe_transport.py`, and `mcp_client.py` aligned
  with the host standard-MCP contract.
- Preserve local collection and direct `pytest_run` ownership; do not add a
  legacy framed protocol or daemon/gateway hop.
- Adding an option requires both `pytest_addoption` and `parser.addini`.
- Keep host registry changes aligned with the root host configuration.
- Run the focused unit suite after connection, transport, model, or reporting
  changes; run a live host test when the environment is available.
