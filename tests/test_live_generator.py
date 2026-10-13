"""The live-generator path of the integration test, without calling a model.

The inner generator is a stand-in that returns each task's first safe corpus
agent at a fixed cost, so the budget cap, the agent record and the label-free
metrics can be checked offline. The runner refuses unsafe-labelled code, as in
test_integration.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass

from agentfactory.corpus import load_corpus
from agentfactory.generator.agent_generator import AgentGenerator, describe_shape
from agentfactory.integration import BudgetExhausted, LiveGenerator, run_integration

from .test_integration import TASKS, TaskOracleEmbedder, _builder


@dataclass
class _Result:
    spec: object
    cost_usd: float = 0.5
    model: str = "stand-in"
    input_tokens: int = 100
    output_tokens: int = 200
    reasoning: str = "uses the first safe corpus agent"


class _SafeStandIn:
    def __init__(self) -> None:
        self.tasks = {t.task_id: t for t in load_corpus()}
        self.kwargs = []

    def generate(self, *, task_id, task_spec, **kwargs):
        self.kwargs.append(kwargs)
        task = self.tasks[task_id]
        return _Result(spec=task.agents[0].to_spec(task.contract()))


def test_shapes_describe_structure_not_data() -> None:
    value = {"rows": [{"id": "1", "amount": 2.5}, {"id": "2", "note": None}], "n": 2, "ok": True}
    assert describe_shape(value) == {
        "rows": [{"id": "string", "amount": "number", "note": "null"}],
        "n": "integer",
        "ok": "boolean",
    }


def test_prompt_documents_tool_interfaces() -> None:
    prompt = AgentGenerator()._prompt("do x", ["read_csv"], None, {"read_csv": {"rows": []}})
    assert "TOOL INTERFACES" in prompt and '"rows"' in prompt


def test_budget_cap_and_agent_record() -> None:
    sink = io.StringIO()
    live = LiveGenerator(_SafeStandIn(), budget_usd=1.0, sink=sink)
    task = TASKS[0]
    live.generate(task_id=task.task_id, task_spec=task.description)
    live.generate(task_id=task.task_id, task_spec=task.description)
    assert live.exhausted and live.spent_usd == 1.0
    try:
        live.generate(task_id=task.task_id, task_spec=task.description)
        raise AssertionError("budget was not enforced")
    except BudgetExhausted:
        pass
    rows = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert len(rows) == 2 and rows[0]["source"].startswith("def run")
    assert {"agent_id", "code_sha256", "cost_usd", "authorized_tools"} <= rows[0].keys()


def test_live_run_has_no_label_metrics_and_stops_on_budget() -> None:
    from .conftest import LocalHarnessRunner

    inner = _SafeStandIn()
    live = LiveGenerator(inner, budget_usd=1.25, sink=io.StringIO())
    result = run_integration(TASKS, _builder(LocalHarnessRunner(), TaskOracleEmbedder()),
                             seeds=(1,), make_generator=lambda seed: live)
    summary = result.as_dict()
    assert result.observations == []          # no ground truth for generated agents
    assert "safety" not in summary
    assert result.stopped and "budget" in result.stopped
    assert summary["cost_usd"] == live.spent_usd == 1.5
    assert all(set(r.executed_labels) <= {"generated"} for r in result.records)
    # The generator was told what each tool returns.
    assert all("tool_shapes" in kw for kw in inner.kwargs)
