"""In-sandbox harness. Runs *inside* the isolation boundary, stdlib only.

Responsibilities, in order of importance:

1. Give the agent stub tools and nothing else. The stubs accept the call, record
   it with full arguments, and return deterministic synthetic data. An agent
   that constructs a malicious tool argument therefore has that argument
   captured as evidence while nothing real happens.
2. Record attempts rather than only blocking them. A blocked socket call that
   nobody logged is a missed measurement — `network_attempts` is an outcome
   variable, not just a safety control.
3. Report through a sentinel-prefixed line so an agent printing forged JSON to
   stdout cannot fake a clean trace.

This file is copied into the container verbatim. It must stay importable on a
bare Python image with no third-party packages.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import sys
import time
import traceback
from contextlib import redirect_stdout
from typing import Any

SENTINEL = "__AGENTFACTORY_RESULT__"

# Paths the agent may read. Anything else is recorded as a violation.
ALLOWED_READ_PREFIXES: tuple[str, ...] = ("/workspace", "/tmp/agent", "./data")

_network_attempts: list[str] = []
_fs_violations: list[str] = []
_unauthorized_tool_calls: list[str] = []
_tool_invocations: list[dict[str, Any]] = []

# Captured before the filesystem guard is installed, so the harness can still
# read its own job file after the agent has been locked out of the filesystem.
real_open = builtins.open


# --------------------------------------------------------------------------
# Containment: record and deny
# --------------------------------------------------------------------------


def _install_network_block() -> None:
    """Deny every outbound connection at the Python level.

    This is the innermost of three layers — the NSG and the container's own
    lack of egress are the ones that actually matter. This one exists because
    it is the only layer that can tell us *what the agent tried to reach*,
    which the network layers cannot.
    """
    try:
        import socket
    except ImportError:
        return

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        target = args[0] if args else kwargs.get("address", "?")
        _network_attempts.append(f"socket.connect({target!r})")
        raise PermissionError("network access denied by sandbox policy")

    class _BlockedSocket(socket.socket):  # type: ignore[misc]
        def connect(self, address: Any) -> None:  # noqa: D102
            _network_attempts.append(f"socket.connect({address!r})")
            raise PermissionError("network access denied by sandbox policy")

        def connect_ex(self, address: Any) -> int:  # noqa: D102
            _network_attempts.append(f"socket.connect_ex({address!r})")
            raise PermissionError("network access denied by sandbox policy")

    socket.socket = _BlockedSocket  # type: ignore[misc,assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]


def _install_fs_guard() -> None:
    """Record reads and writes outside the agent's working area."""
    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        try:
            path = os.path.abspath(str(file))
        except (TypeError, ValueError):
            path = str(file)
        allowed = any(
            path.startswith(os.path.abspath(prefix)) for prefix in ALLOWED_READ_PREFIXES
        )
        writing = any(flag in mode for flag in ("w", "a", "x", "+"))
        if not allowed or (writing and not path.startswith(os.path.abspath("/tmp/agent"))):
            _fs_violations.append(f"open({path!r}, mode={mode!r})")
            raise PermissionError(f"filesystem access denied by sandbox policy: {path}")
        return real_open(file, mode, *args, **kwargs)

    builtins.open = guarded_open  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Stub tools
# --------------------------------------------------------------------------

_STUB_RESPONSES: dict[str, Any] = {
    "read_log_file": {
        "lines": [
            "2026-01-04T10:00:01Z ERROR svc-alpha connection refused",
            "2026-01-04T10:00:04Z WARN  svc-beta retry scheduled",
            "2026-01-04T10:00:09Z ERROR svc-alpha connection refused",
            "2026-01-04T10:01:12Z INFO  svc-gamma healthy",
        ]
    },
    "query_monitoring_alerts": {
        "alerts": [
            {"service": "svc-alpha", "severity": "critical", "count": 2},
            {"service": "svc-beta", "severity": "warning", "count": 1},
        ]
    },
    "create_incident_ticket": {"ticket_id": "INC-000123", "status": "created"},
    "query_cmdb": {
        "services": [
            {"name": "svc-alpha", "owner": "team-1", "tier": 1},
            {"name": "svc-beta", "owner": "team-2", "tier": 2},
        ]
    },
    "read_csv": {
        "rows": [
            {"id": "1", "date": "2026-01-04", "amount": "10.50"},
            {"id": "2", "date": "04/01/2026", "amount": "7.25"},
            {"id": "3", "date": "2026-01-06", "amount": "3.00"},
        ]
    },
    "read_json_dataset": {"records": [{"key": "a", "value": 1}, {"key": "b", "value": 2}]},
    "read_spreadsheet": {"sheets": {"Q1": [[1, 2], [3, 4]], "Q2": [[5, 6], [7, 8]]}},
    "write_report": {"written": True, "bytes": 1024},
    "retrieve_documents": {
        "chunks": [
            {"doc_id": "d1", "text": "Alpha service handles ingestion."},
            {"doc_id": "d2", "text": "Beta service handles reporting."},
        ]
    },
    "extract_structured_data": {"entities": [{"type": "service", "value": "svc-alpha"}]},
}


class ToolBox:
    """The only interface the agent gets to the outside world.

    Attribute access returns a callable for an authorized tool, and a recording
    stub that raises for anything else — so an undeclared tool call is captured
    as evidence rather than silently failing.
    """

    def __init__(self, authorized: list[str]) -> None:
        self._authorized = set(authorized)

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> Any:
            _tool_invocations.append({"tool": name, "arguments": kwargs, "ts": time.time()})
            if name not in self._authorized:
                _unauthorized_tool_calls.append(f"{name}({json.dumps(kwargs, default=str)[:400]})")
                raise PermissionError(f"tool not authorized by contract: {name}")
            return _STUB_RESPONSES.get(name, {"ok": True})

        return call

    def __repr__(self) -> str:
        return f"ToolBox(authorized={sorted(self._authorized)})"


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _execute(source: str, test_input: dict[str, Any], tools: ToolBox) -> Any:
    """Import the agent module and call its entry point.

    The convention is a module-level `run(task_input, tools)`. Anything else is
    a contract violation reported as an execution error.
    """
    module_globals: dict[str, Any] = {"__name__": "generated_agent", "__builtins__": builtins}
    compiled = compile(source, "<generated_agent>", "exec")
    exec(compiled, module_globals)  # noqa: S102 — executing the agent is the point

    entry = module_globals.get("run")
    if not callable(entry):
        raise TypeError("agent does not define a callable run(task_input, tools)")
    return entry(test_input, tools)


def _read_job() -> dict[str, Any]:
    """Job description from stdin, or from a file when stdin is unavailable.

    ACI and Kubernetes give no practical way to write to a container's stdin
    after it starts, so those runners drop the job on disk and point at it here.
    """
    job_file = os.environ.get("AGENTFACTORY_JOB_FILE")
    if job_file and os.path.exists(job_file):
        with real_open(job_file, encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(sys.stdin.read())


def main() -> int:
    job = _read_job()
    tools = ToolBox(job.get("authorized_tools", []))

    _install_network_block()
    _install_fs_guard()

    started = time.perf_counter()
    output: Any = None
    error: str | None = None
    captured = io.StringIO()

    try:
        with redirect_stdout(captured):
            output = _execute(job["agent_source"], job.get("test_input", {}), tools)
    except BaseException as exc:  # noqa: BLE001 — every failure is a result, not a crash
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc(file=sys.stderr)

    elapsed_ms = (time.perf_counter() - started) * 1000

    # Anything the agent printed goes to stderr, keeping stdout clean for the
    # report line.
    agent_stdout = captured.getvalue()
    if agent_stdout:
        sys.stderr.write(agent_stdout)

    report = {
        "output": output if _json_safe(output) else str(output),
        "error": error,
        "elapsed_ms": elapsed_ms,
        "network_attempts": _network_attempts,
        "fs_violations": _fs_violations,
        "unauthorized_tool_calls": _unauthorized_tool_calls,
        "tool_invocations": _tool_invocations,
        "syscalls_blocked": [],
    }
    sys.stdout.write(SENTINEL + json.dumps(report, default=str) + "\n")
    sys.stdout.flush()
    return 0


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
