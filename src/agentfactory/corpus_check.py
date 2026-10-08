"""Check that a corpus task's labels hold up, before anyone measures against them.

    python -m agentfactory.corpus_check            # every task
    python -m agentfactory.corpus_check T004 T012  # some

The loader enforces the shape of a task. This enforces what the labels claim:

- safe_correct and incorrect_only agents clear Layers 1-2;
- unsafe_only and both agents are flagged by Layers 1-2 (the current corpus
  design: every injected violation is a plain, recognisable pattern);
- run against the task's own tool data, every safe_correct agent reproduces the
  expected output exactly, and every incorrect_only agent does not — while
  doing nothing a sandbox would flag;
- at least one incorrect_only agent per task is borderline (a partial match),
  so the strictness sweep of RQ4 has something to move;
- expected_output has at least two keys, so a partial match is possible at all;
- there are two paraphrases, and no agent's code duplicates another's.

Only agents labelled safe_correct or incorrect_only, and only after they have
cleared Layers 1-2, are executed — in a plain subprocess, because this is a
development check on the author's machine. Anything labelled unsafe is
examined statically and never run here. Whether an unsafe_only agent is
otherwise correct cannot be checked without running it, and is left to review.
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from agentfactory.contracts import IsolationLevel
from agentfactory.corpus import CorpusAgent, CorpusTask, load_corpus
from agentfactory.metrics.core import GroundTruth
from agentfactory.validation.layer1_static import analyse
from agentfactory.validation.layer2_policy import EnterprisePolicy, check
from agentfactory.validation.sandbox.base import (
    HARNESS_PATH,
    SandboxRequest,
    parse_harness_result,
)

EXECUTABLE = (GroundTruth.SAFE_CORRECT, GroundTruth.INCORRECT_ONLY)


@dataclass
class AgentResult:
    variant: str
    label: str
    static_passed: bool
    findings: list[str]
    match: float | None = None
    output: object = None
    error: str | None = None
    runtime_flags: list[str] = field(default_factory=list)


@dataclass
class TaskReport:
    task_id: str
    agents: list[AgentResult]
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def _run(task: CorpusTask, agent: CorpusAgent) -> tuple[float, object, str | None, list[str]]:
    spec = agent.to_spec(task.contract())
    request = SandboxRequest(
        spec=spec, test_input={"case": 1}, expected_output=task.expected_output,
        tool_fixtures=dict(task.tool_fixtures),
    )
    proc = subprocess.run(
        [sys.executable, str(HARNESS_PATH)], input=request.payload(),
        capture_output=True, text=True, timeout=30, check=False,
    )
    trace = parse_harness_result(
        proc.stdout, level=IsolationLevel.L1, exit_code=proc.returncode, wall_time_ms=0.0,
        stderr=proc.stderr, expected_output=task.expected_output,
    )
    flags = [*trace.network_attempts, *trace.fs_violations, *trace.unauthorized_tool_calls]
    error = None
    if trace.exit_code not in (0, None) or not trace.harness_reported:
        error = (proc.stderr or "no harness report").strip()[-500:]
    elif trace.stderr_tail.strip():
        error = trace.stderr_tail.strip()[-500:]
    return trace.output_match_score, trace.agent_output, error, flags


def check_task(task: CorpusTask, policy: EnterprisePolicy | None = None) -> TaskReport:
    policy = policy or EnterprisePolicy.load()
    contract = task.contract()
    policy_report = check(contract, policy)
    problems: list[str] = []
    if not policy_report.passed:
        problems.append(f"the task's own contract fails Layer 2: "
                        f"{[str(f) for f in policy_report.findings]}")

    results: list[AgentResult] = []
    for agent in task.agents:
        label = GroundTruth(agent.label.value)
        static = analyse(agent.source, contract, use_bandit=False)
        result = AgentResult(
            variant=agent.variant, label=label.value,
            static_passed=static.passed and policy_report.passed,
            findings=[str(f) for f in static.findings],
        )
        results.append(result)

        if label.is_unsafe:
            if result.static_passed:
                problems.append(
                    f"{agent.variant} ({label.value}): not flagged by Layers 1-2; "
                    "the injected violation is not recognisable"
                )
            continue
        if not result.static_passed:
            problems.append(f"{agent.variant} ({label.value}): flagged by Layers 1-2: "
                            f"{result.findings}")
            continue
        if label not in EXECUTABLE:
            continue

        result.match, result.output, result.error, result.runtime_flags = _run(task, agent)
        if result.runtime_flags:
            problems.append(f"{agent.variant} ({label.value}): sandbox would flag "
                            f"{result.runtime_flags}")
        if label is GroundTruth.SAFE_CORRECT and (result.match != 1.0 or result.error):
            problems.append(
                f"{agent.variant} (safe_correct): match {result.match:.2f}"
                f"{' error: ' + result.error if result.error else ''}; output {result.output!r}"
            )
        if label is GroundTruth.INCORRECT_ONLY and result.match == 1.0:
            problems.append(f"{agent.variant} (incorrect_only): matches the expected output "
                            "exactly, so the automatic check cannot see its error")

    incorrect = [r for r in results if r.label == GroundTruth.INCORRECT_ONLY.value]
    if not any(r.match is not None and 0.0 < r.match < 1.0 for r in incorrect):
        problems.append("no borderline incorrect_only agent (0 < match < 1)")
    if not isinstance(task.expected_output, dict) or len(task.expected_output) < 2:
        problems.append("expected_output needs at least two keys")
    if len(task.paraphrases) != 2:
        problems.append(f"expected 2 paraphrases, found {len(task.paraphrases)}")
    return TaskReport(task_id=task.task_id, agents=results, problems=problems)


def check_corpus(tasks: Iterable[CorpusTask]) -> list[TaskReport]:
    tasks = list(tasks)
    policy = EnterprisePolicy.load()
    reports = [check_task(t, policy) for t in tasks]
    shas = Counter(a.source_sha256 for t in tasks for a in t.agents)
    for task, report in zip(tasks, reports, strict=True):
        for agent in task.agents:
            if shas[agent.source_sha256] > 1:
                report.problems.append(f"{agent.variant}: code duplicates another agent's")
    return reports


def main(argv: list[str] | None = None) -> int:
    wanted = set(argv if argv is not None else sys.argv[1:])
    tasks = [t for t in load_corpus() if not wanted or t.task_id in wanted]
    reports = check_corpus(tasks)
    for report in reports:
        print(f"{report.task_id}: {'ok' if report.ok else 'PROBLEMS'}")
        for agent in report.agents:
            match = "-" if agent.match is None else f"{agent.match:.2f}"
            print(f"  {agent.variant:3s} {agent.label:15s} static={'pass' if agent.static_passed else 'FLAG'} "
                  f"match={match}")
        for problem in report.problems:
            print(f"  ! {problem}")
    bad = [r.task_id for r in reports if not r.ok]
    print(f"\n{len(reports) - len(bad)}/{len(reports)} tasks ok" + (f"; problems in {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
