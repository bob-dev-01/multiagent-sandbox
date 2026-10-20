"""Demo runs as streams of events, and the adapter from the system's own event log.

A demo event is `{"seq", "t", "type", "data"}`: a sequence number, milliseconds
since the run started, a type the page animates, and its data. The same schema
is used for live runs and for recorded replays, so the page cannot tell them
apart except by the `replay` flag on `run_started`.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from agentfactory.telemetry.events import EventType


class DemoRun:
    """One run's event stream; readers wait on it while it is live."""

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.events: list[dict[str, Any]] = []
        self.done = False
        self._started = time.perf_counter()
        self._cond = threading.Condition()

    def emit(self, type_: str, **data: Any) -> None:
        with self._cond:
            self.events.append({
                "seq": len(self.events),
                "t": round((time.perf_counter() - self._started) * 1000, 1),
                "type": type_,
                "data": data,
            })
            self._cond.notify_all()

    def finish(self) -> None:
        with self._cond:
            self.done = True
            self._cond.notify_all()

    def wait_after(self, seq: int, timeout: float) -> list[dict[str, Any]]:
        """Events with seq > `seq`, waiting up to `timeout` seconds for one."""
        with self._cond:
            if len(self.events) <= seq + 1 and not self.done:
                self._cond.wait(timeout)
            return self.events[seq + 1:]


def _trace_view(trace: dict[str, Any] | None) -> dict[str, Any]:
    """The parts of a sandbox trace the page shows."""
    if not trace:
        return {}
    return {
        "kernel": trace.get("kernel_release"),
        "exit_code": trace.get("exit_code"),
        "agent_ms": trace.get("agent_elapsed_ms"),
        "init_ms": trace.get("sandbox_init_ms"),
        "wall_ms": trace.get("wall_time_ms"),
        "network_attempts": list(trace.get("network_attempts") or []),
        "fs_violations": list(trace.get("fs_violations") or []),
        "unauthorized_tool_calls": list(trace.get("unauthorized_tool_calls") or []),
        "runtime_violations": list(trace.get("runtime_violations") or []),
        "output": trace.get("agent_output"),
        "match": trace.get("output_match_score"),
    }


class DemoEventLog:
    """Stands in for `EventLog`: the system emits as usual, the demo listens.

    Layer results, registration, reuse, deployment, escalation and
    regeneration come from here. Generation, the sandbox, the judge and the
    verdict come from the instrumented parts, which know more.
    """

    def __init__(self, run: DemoRun) -> None:
        self.run = run
        self.run_id = run.run_id

    def emit(self, event_type: EventType, **fields: Any) -> None:
        detail = fields.get("detail") or {}
        layer = fields.get("layer")
        if event_type is EventType.LAYER_COMPLETED and layer is not None:
            name = getattr(layer, "value", layer)
            data: dict[str, Any] = {
                "layer": name, "passed": fields.get("passed"),
                "ms": round(fields.get("latency_ms") or 0.0, 1), "score": fields.get("score"),
            }
            if name == "sandbox":
                data["trace"] = _trace_view(detail.get("trace"))
            else:
                data["findings"] = list(detail.get("findings") or [])
            self.run.emit("layer_finished", **data)
        elif event_type is EventType.AGENT_REGISTERED:
            self.run.emit("registered", agent_id=str(fields.get("agent_id")))
        elif event_type is EventType.AGENT_REUSED:
            self.run.emit("reused", agent_id=str(fields.get("agent_id")),
                          similarity=fields.get("score"),
                          source_task=detail.get("source_task_id"))
        elif event_type is EventType.AGENT_DEPLOYED:
            self.run.emit("deployed", passed=fields.get("passed"),
                          ms=round(fields.get("latency_ms") or 0.0, 1),
                          outcome=detail.get("outcome"), reused=detail.get("reused"))
        elif event_type is EventType.DEPLOYMENT_REFUSED:
            self.run.emit("refused", reason=detail.get("reason"))
        elif event_type is EventType.ESCALATED:
            self.run.emit("escalated", reason=detail.get("reason"))
        elif event_type is EventType.REGENERATION_REQUESTED:
            self.run.emit("regeneration", feedback=str(detail.get("feedback", ""))[:1500])
        elif event_type is EventType.RUN_FAILED:
            self.run.emit("error", stage=detail.get("stage"),
                          message=str(detail.get("error", ""))[:600])
