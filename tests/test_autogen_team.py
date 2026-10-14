"""The Agent Factory as AutoGen agents: same decisions, carried by messages.

Every submission is run twice from the same seed — once through the AutoGen
runtime, once with the factory called directly — and the two must agree on
everything except timing. The runner refuses unsafe-labelled code, as in
test_integration.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentfactory.autogen_team import AutoGenAgentFactory
from agentfactory.integration import run_integration
from agentfactory.orchestrator.factory import Outcome

from .test_integration import TASKS, TaskOracleEmbedder, _builder

SUBSET = TASKS[:6]
SEEDS = (1, 2)


def _comparable(result):
    return [
        (r.seed, r.phase, r.task_id, r.paraphrase, r.outcome, r.succeeded, r.reused,
         r.attempts, r.refusals, r.executed_labels, r.executed_unsafe, r.verdicts)
        for r in result.records
    ]


def _run(tasks, *, autogen: bool, teams: list | None = None):
    from .conftest import LocalHarnessRunner

    build = _builder(LocalHarnessRunner(), TaskOracleEmbedder())

    def build_team(generator):
        team = AutoGenAgentFactory(build(generator))
        if teams is not None:
            teams.append(team)
        return team

    return run_integration(tasks, build_team if autogen else build, seeds=SEEDS)


@pytest.fixture(scope="module")
def both():
    teams: list[AutoGenAgentFactory] = []
    return _run(SUBSET, autogen=False), _run(SUBSET, autogen=True, teams=teams), teams


def test_autogen_team_decides_exactly_as_the_direct_factory(both) -> None:
    direct, team, _ = both
    assert _comparable(team) == _comparable(direct)
    assert team.safety() == direct.safety()


def test_every_execution_went_through_an_autogen_message(both) -> None:
    _, team, teams = both
    deploys = sum(t.message_counts.get("DeployRequest->task_executor", 0) for t in teams)
    tasks = sum(t.message_counts.get("TaskRequest->orchestrator", 0) for t in teams)
    executions = sum(len(r.executed_labels) for r in team.records)
    assert tasks == len(team.records)
    # One DeployRequest per execution, plus one per gate refusal.
    refusals = sum(len(r.refusals) for r in team.records)
    assert deploys == executions + refusals
    assert executions > 0


def test_a_refusal_crosses_the_message_boundary() -> None:
    # High-risk with no L3 runner configured: the executor agent must refuse,
    # and the refusal must reach the orchestrator as the same outcome.
    high_risk = [replace(t, risk_tier="high") for t in SUBSET]
    direct = _run(high_risk, autogen=False)
    team = _run(high_risk, autogen=True)
    assert _comparable(team) == _comparable(direct)
    assert any(r.outcome == Outcome.DEPLOYMENT_REFUSED.value for r in team.records)
    assert not any(r.executed_labels for r in team.records)


def test_the_team_needs_an_executor() -> None:
    from agentfactory.orchestrator.factory import AgentFactory

    with pytest.raises(ValueError):
        AutoGenAgentFactory(AgentFactory(generator=None, pipeline=None))
