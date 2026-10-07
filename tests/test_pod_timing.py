"""The pod runner's clock must resolve the L2/L3 difference RQ2 asks about.

The first pod runner polled pod status once a second, and the measured L2→L3
gap was ~0.16 s — below what that instrument could see. These tests pin the
replacement down: milestones come from watch events stamped on arrival, a
milestone whose time is only an upper bound says so, and a fault in the
instrument is never recorded as something the agent did.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import time

import pytest

from agentfactory.contracts import AgentContract
from agentfactory.validation.sandbox.base import (
    HARNESS_SENTINEL,
    SandboxExecutionError,
    SandboxRequest,
)
from agentfactory.validation.sandbox.pod_runner import (
    JsonStreamDecoder,
    KubectlPodWatch,
    gvisor_pod_runner,
)

from .conftest import SAFE_AGENT, make_spec


@pytest.fixture
def request_(safe_contract: AgentContract) -> SandboxRequest:
    return SandboxRequest(spec=make_spec(SAFE_AGENT, safe_contract), test_input={"case": 1})


def _pod(
    phase: str = "Pending",
    *,
    scheduled: bool = False,
    state: str | None = None,
    reason: str | None = None,
) -> dict:
    status: dict = {"phase": phase, "conditions": []}
    if scheduled:
        status["conditions"].append({"type": "PodScheduled", "status": "True"})
    if state == "waiting":
        status["containerStatuses"] = [{"state": {"waiting": {"reason": reason}}}]
    elif state == "running":
        status["containerStatuses"] = [{"state": {"running": {}}}]
    elif state == "terminated":
        status["containerStatuses"] = [
            {"state": {"terminated": {"exitCode": 0, "reason": reason or "Completed"}}}
        ]
    return {"status": status}


SCHEDULED = _pod(scheduled=True)
RUNNING = _pod("Running", scheduled=True, state="running")
DONE = _pod("Succeeded", scheduled=True, state="terminated")


class FakeWatch:
    """Replays (delay, pod) pairs; a None pod ends the stream."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.closed = False

    def next(self, timeout: float):
        if not self.script:
            time.sleep(min(timeout, 0.01))
            raise queue.Empty
        delay, pod = self.script.pop(0)
        time.sleep(delay)
        return None if pod is None else (time.perf_counter(), pod)

    def error(self) -> str:
        return "connection reset"

    def close(self) -> None:
        self.closed = True


class FakeKubectl:
    def __init__(self, logs: str = "", final_phase: str | None = None) -> None:
        self.logs = logs
        self.final_phase = final_phase

    def __call__(self, *args, **_kw) -> subprocess.CompletedProcess:
        out = ""
        if args[0] == "logs":
            out = self.logs
        elif args[:2] == ("get", "pod"):
            out = json.dumps({"status": {"phase": self.final_phase}})
        return subprocess.CompletedProcess(list(args), 0, out, "")


def harness_stdout(release: str = "4.19.0-gvisor") -> str:
    report = {
        "output": {"total": 2},
        "kernel_release": release,
        "elapsed_ms": 0.2,
        "sandbox_init_ms": 41.5,
    }
    return HARNESS_SENTINEL + json.dumps(report) + "\n"


def runner_with(monkeypatch, watches: list, kubectl: FakeKubectl):
    runner = gvisor_pod_runner(kube_context="t")
    monkeypatch.setattr(runner, "_kubectl", kubectl)
    opened = iter(watches)
    monkeypatch.setattr(runner, "_open_watch", lambda name: next(opened))
    return runner


def expire_deadline_soon(monkeypatch) -> None:
    """Jump the runner's monotonic clock far ahead after ~50 ms."""
    import agentfactory.validation.sandbox.pod_runner as pod_runner

    real = time.monotonic
    start = real()
    monkeypatch.setattr(
        pod_runner.time, "monotonic", lambda: real() + (1e6 if real() - start > 0.05 else 0)
    )


# ------------------------------------------------------------- milestones


def test_milestones_come_from_live_watch_events(monkeypatch, request_) -> None:
    watch = FakeWatch([(0.0, _pod()), (0.02, SCHEDULED), (0.05, RUNNING), (0.05, DONE)])
    trace = runner_with(monkeypatch, [watch], FakeKubectl(harness_stdout())).run(request_)

    m = trace.milestones_ms
    assert list(m) == ["applied", "scheduled", "running", "terminated", "logs_fetched"]
    assert m["applied"] <= m["scheduled"] < m["running"] < m["terminated"] <= m["logs_fetched"]
    # Spacing well under a second is resolved — the old poll could not.
    assert 30 <= m["running"] - m["scheduled"] < 500
    assert 30 <= m["terminated"] - m["running"] < 500
    assert trace.milestones_inexact == ()
    assert trace.sandbox_init_ms == 41.5
    assert watch.closed


def test_initial_and_collapsed_states_are_marked_inexact(monkeypatch, request_) -> None:
    """Already scheduled when the watch began; running never seen on its own."""
    watch = FakeWatch([(0.0, SCHEDULED), (0.05, DONE)])
    trace = runner_with(monkeypatch, [watch], FakeKubectl(harness_stdout())).run(request_)
    assert trace.milestones_inexact == ("running", "scheduled")
    assert trace.milestones_ms["running"] == trace.milestones_ms["terminated"]


def test_a_dropped_watch_is_reopened(monkeypatch, request_) -> None:
    first = FakeWatch([(0.0, SCHEDULED), (0.0, None)])
    second = FakeWatch([(0.0, DONE)])
    runner = runner_with(monkeypatch, [first, second], FakeKubectl(harness_stdout()))
    trace = runner.run(request_)
    assert trace.exit_code == 0
    assert first.closed and second.closed
    # Termination first appeared in the reopened watch's initial state.
    assert "terminated" in trace.milestones_inexact


def test_the_watch_is_open_before_the_pod_is_applied(monkeypatch, request_) -> None:
    """kubectl takes ~1.6 s to start; a watch opened after apply missed the pod's run."""
    order: list[str] = []
    kubectl = FakeKubectl(harness_stdout())
    runner = gvisor_pod_runner(kube_context="t")

    def record_kubectl(*args, **kw):
        order.append(args[0])
        return kubectl(*args, **kw)

    def record_watch(name):
        order.append("watch")
        return FakeWatch([(0.0, DONE)])

    monkeypatch.setattr(runner, "_kubectl", record_kubectl)
    monkeypatch.setattr(runner, "_open_watch", record_watch)
    runner.run(request_)
    assert order.index("watch") < order.index("apply")


def test_a_failed_apply_closes_the_watch(monkeypatch, request_) -> None:
    watch = FakeWatch([])
    runner = gvisor_pod_runner(kube_context="t")
    monkeypatch.setattr(
        runner, "_kubectl",
        lambda *a, **k: subprocess.CompletedProcess(list(a), 1, "", "denied"),
    )
    monkeypatch.setattr(runner, "_open_watch", lambda name: watch)
    with pytest.raises(SandboxExecutionError, match="apply failed"):
        runner.run(request_)
    assert watch.closed


# ------------------------------------------- instrument faults stay faults


def test_a_watch_that_keeps_ending_is_an_infrastructure_failure(
    monkeypatch, request_
) -> None:
    watches = [FakeWatch([(0.0, None)]) for _ in range(10)]
    with pytest.raises(SandboxExecutionError, match="ended"):
        runner_with(monkeypatch, watches, FakeKubectl()).run(request_)


def test_a_stalled_watch_on_a_finished_pod_is_not_an_agent_timeout(
    monkeypatch, request_
) -> None:
    """Fail closed: a fault in the instrument must not become a verdict."""
    expire_deadline_soon(monkeypatch)
    runner = runner_with(
        monkeypatch, [FakeWatch([(0.0, SCHEDULED)])], FakeKubectl(final_phase="Succeeded")
    )
    with pytest.raises(SandboxExecutionError, match="never reported"):
        runner.run(request_)


def test_a_pod_still_running_at_the_deadline_is_a_timeout(monkeypatch, request_) -> None:
    expire_deadline_soon(monkeypatch)
    runner = runner_with(
        monkeypatch, [FakeWatch([(0.0, RUNNING)])], FakeKubectl(final_phase="Running")
    )
    assert runner.run(request_).timed_out


def test_a_start_failure_is_still_refused(monkeypatch, request_) -> None:
    stuck = _pod(scheduled=True, state="waiting", reason="StartError")
    with pytest.raises(SandboxExecutionError, match="never started"):
        runner_with(monkeypatch, [FakeWatch([(0.0, stuck)])], FakeKubectl()).run(request_)


# --------------------------------------------------------- the real reader


def test_stream_decoder_splits_concatenated_pretty_json() -> None:
    objs = [{"a": 1, "s": "ünïcode"}, {"b": [1, 2, {"c": None}]}, {"d": "x"}]
    stream = "".join(json.dumps(o, indent=4, ensure_ascii=False) for o in objs).encode()
    for size in (1, 2, 3, 7, 64, len(stream)):
        decoder = JsonStreamDecoder()
        got: list = []
        for i in range(0, len(stream), size):
            got += decoder.feed(stream[i : i + size])
        assert got == objs, size


def test_kubectl_watch_stamps_objects_as_they_arrive(tmp_path) -> None:
    """The real reader thread, against a stand-in kubectl that emits with gaps."""
    script = tmp_path / "fake_kubectl.py"
    script.write_text(
        "import json, sys, time\n"
        "for i in range(3):\n"
        "    sys.stdout.write(json.dumps({'i': i}, indent=2)); sys.stdout.flush()\n"
        "    time.sleep(0.15)\n",
        encoding="utf-8",
    )
    watch = KubectlPodWatch([sys.executable, str(script)])
    try:
        got = [watch.next(timeout=5) for _ in range(3)]
        assert watch.next(timeout=5) is None
    finally:
        watch.close()
    assert [obj for _, obj in got] == [{"i": 0}, {"i": 1}, {"i": 2}]
    gaps = [b[0] - a[0] for a, b in zip(got, got[1:], strict=False)]
    assert all(0.08 < g < 0.6 for g in gaps), gaps


# ------------------------------------------------------- in-sandbox clock


def test_bootstrap_stamps_its_start_first(request_) -> None:
    """sandbox_init_ms covers the whole start-up path only if the stamp opens it."""
    container = gvisor_pod_runner(kube_context="t").manifest(request_, "a")["spec"][
        "containers"
    ][0]
    assert container["command"][2].startswith(
        "import time,os;os.environ['AGENTFACTORY_BOOT_EPOCH']"
    )


def test_harness_reports_sandbox_init_from_the_boot_stamp(monkeypatch) -> None:
    from agentfactory.validation.sandbox import harness

    monkeypatch.setenv("AGENTFACTORY_BOOT_EPOCH", repr(harness._HARNESS_STARTED_EPOCH - 0.25))
    assert harness._sandbox_init_ms() == pytest.approx(250.0, abs=0.01)
    monkeypatch.delenv("AGENTFACTORY_BOOT_EPOCH")
    assert harness._sandbox_init_ms() is None
