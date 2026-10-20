"""The demo session end to end, offline: stored corpus agents, a local runner.

Only scenarios that never execute unsafe code locally are run here: the safe
corpus agent (executed by the test-only local runner, as the integration test
does) and an unsafe one, which static analysis stops before any sandbox.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agentfactory.demo.session import DemoSession, RunRequest

from .test_integration import GuardedRunner, TaskOracleEmbedder


@pytest.fixture
def session():
    from .conftest import LocalHarnessRunner

    runner = GuardedRunner(LocalHarnessRunner())
    return DemoSession(runner_factory=lambda level: runner, use_judge=False,
                       embedder=TaskOracleEmbedder())


def wait(session, req) -> list[dict]:
    run = session.start(req)
    deadline = time.time() + 120
    while not run.done and time.time() < deadline:
        time.sleep(0.1)
    assert run.done, "run did not finish"
    return run.events


def types(events) -> list[str]:
    return [e["type"] for e in events]


def first(events, kind) -> dict:
    return next(e["data"] for e in events if e["type"] == kind)


def test_a_correct_agent_goes_through_every_room_and_is_registered(session) -> None:
    events = wait(session, RunRequest(task_id="T001", mode="safe_a1", level="L2"))
    seen = types(events)
    for kind in ("run_started", "registry_lookup", "generation_finished", "validation_started",
                 "layer_finished", "sandbox_started", "sandbox_finished", "verdict",
                 "deployed", "registered", "autogen_message", "run_finished"):
        assert kind in seen, (kind, seen)
    assert first(events, "registry_lookup")["decision"] == "generate"
    verdict = first(events, "verdict")
    assert verdict["verdict"] == "PASS"
    assert set(verdict["by_profile"]) == {"permissive", "balanced", "strict", "paranoid"}
    phases = [e["data"]["phase"] for e in events if e["type"] == "sandbox_finished"]
    assert phases == ["validation", "execution"]
    done = first(events, "run_finished")
    assert done["outcome"] == "generated_and_deployed" and done["succeeded"]
    assert [e["seq"] for e in events] == list(range(len(events)))


def test_a_reworded_task_reuses_the_registered_agent(session) -> None:
    wait(session, RunRequest(task_id="T001", mode="safe_a1", level="L2"))
    events = wait(session, RunRequest(task_id="T001", mode="safe_a1", wording="p0", level="L2"))
    assert first(events, "registry_lookup")["decision"] == "reuse"
    assert "reused" in types(events)
    assert "generation_started" not in types(events)
    assert first(events, "run_finished")["outcome"] == "reused"


def test_an_unsafe_agent_is_stopped_before_the_sandbox_and_escalated(session) -> None:
    events = wait(session, RunRequest(task_id="T001", mode="unsafe_u1", level="L2"))
    static = next(e["data"] for e in events
                  if e["type"] == "layer_finished" and e["data"]["layer"] == "static")
    assert not static["passed"] and static["findings"]
    assert first(events, "verdict")["verdict"] == "FAIL_UNSAFE"
    assert "escalated" in types(events)
    assert "sandbox_started" not in types(events)
    assert first(events, "run_finished")["outcome"] == "escalated_unsafe"


def test_one_run_at_a_time_and_unknown_tasks_refused(session) -> None:
    from agentfactory.demo.session import RunRefused

    with pytest.raises(RunRefused):
        session.start(RunRequest(task_id="T999"))


def test_protected_routes_need_the_access_code(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_ACCESS_CODE", "test-code-123")
    from agentfactory.demo import app as demo_app

    client = TestClient(demo_app.app)
    assert client.get("/api/health").json() == {"ok": True}
    assert client.post("/api/runs", json={"task_id": "T001"}).status_code == 401
    assert client.post("/api/runs", json={"task_id": "T001"},
                       headers={"X-Demo-Code": "wrong"}).status_code == 401
    assert client.post("/api/infra/aks/start").status_code == 401
    assert client.post("/api/auth", headers={"X-Demo-Code": "test-code-123"}).json()["ok"]
    tasks = client.get("/api/session").json()["tasks"]
    assert len(tasks) == 30 and tasks[0]["agents"]
