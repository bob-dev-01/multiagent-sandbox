"""What gVisor costs while an agent works, not only when its sandbox starts.

    python experiments/workload_bench.py --run-id workload-l2l3 --reps 20

The corpus agents run for a millisecond or less, so the full-corpus runs
measure gVisor's start-up cost and nothing else. Here four safe agents each do
one kind of work for a measurable time, and the agent's own runtime — timed
inside the sandbox by the harness, so pod scheduling is excluded — is compared
between L2 (runc) and L3 (gVisor) on the same node:

  cpu       hashing in a loop; almost no system calls
  alloc     building and dropping large lists; memory-management calls
  file_io   writing, reading, listing and deleting small files in /tmp/agent
            (a memory-backed volume, so this is system-call cost, not disk)
  stat      examining one file many times; a pure system-call loop

Each agent is checked by static analysis first and must pass it. Order is
randomised from the seed; every run is recorded in <run-id>/runs.jsonl.
Reported per workload: median agent runtime at each level, the L3/L2 ratio of
medians, and a bootstrap 95% interval for the difference of medians.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from run_matrix import build_runner  # noqa: E402

from agentfactory.contracts import AgentContract, AgentSpec, IsolationLevel  # noqa: E402
from agentfactory.validation.layer1_static import analyse  # noqa: E402
from agentfactory.validation.sandbox.base import SandboxRequest  # noqa: E402

WORKLOADS = {
    "cpu": '''
import hashlib


def run(task_input, tools):
    digest = b"seed"
    for _ in range(200000):
        digest = hashlib.sha256(digest).digest()
    return {"digest": digest.hex()[:16]}
''',
    "alloc": '''
def run(task_input, tools):
    total = 0
    for _ in range(40):
        block = [i for i in range(200000)]
        total += len(block)
        del block
    return {"total": total}
''',
    "file_io": '''
from pathlib import Path


def run(task_input, tools):
    base = Path("/tmp/agent/bench")
    base.mkdir(parents=True, exist_ok=True)
    payload = "x" * 512
    for i in range(1500):
        path = base / f"f{i}.txt"
        path.write_text(payload)
        path.read_text()
    names = sorted(p.name for p in base.iterdir())
    for name in names:
        (base / name).unlink()
    return {"files": len(names)}
''',
    "stat": '''
from pathlib import Path


def run(task_input, tools):
    path = Path("/tmp/agent/stat-target")
    path.write_text("x")
    size = 0
    for _ in range(50000):
        size += path.stat().st_size
    return {"size": size}
''',
}


def spec_for(name: str) -> AgentSpec:
    contract = AgentContract(task_spec=f"workload benchmark: {name}", authorized_tools=())
    spec = AgentSpec.from_source(task_id=f"W-{name}", source=WORKLOADS[name].lstrip(),
                                 contract=contract)
    report = analyse(spec.source, contract, use_bandit=True)
    if not report.passed:
        raise SystemExit(f"workload {name} fails static analysis: {report.findings}")
    return spec


def bootstrap_diff(a: list[float], b: list[float], n: int = 5000, seed: int = 1):
    rng = random.Random(seed)
    diffs = sorted(
        statistics.median(rng.choices(b, k=len(b))) - statistics.median(rng.choices(a, k=len(a)))
        for _ in range(n)
    )
    return diffs[int(0.025 * n)], diffs[int(0.975 * n)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id", default="workload-l2l3")
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261016)
    ap.add_argument("--k8s-namespace", default="agentfactory-sandbox")
    ap.add_argument("--kube-context", default="agentfac-aks")
    args = ap.parse_args(argv)

    out_dir = REPO_ROOT / "runs" / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    runner_args = SimpleNamespace(l2_backend="pod", k8s_namespace=args.k8s_namespace,
                                  kube_context=args.kube_context, require_seccomp=False)
    runners = {lvl: build_runner(IsolationLevel(lvl), runner_args) for lvl in ("L2", "L3")}
    specs = {name: spec_for(name) for name in WORKLOADS}

    plan = [(name, lvl, rep) for name in WORKLOADS for lvl in runners
            for rep in range(1, args.reps + 1)]
    random.Random(args.seed).shuffle(plan)
    times: dict[tuple[str, str], list[float]] = {}
    with (out_dir / "runs.jsonl").open("a", encoding="utf-8") as sink:
        for i, (name, lvl, rep) in enumerate(plan, 1):
            trace = runners[lvl].run(SandboxRequest(spec=specs[name], test_input={"case": 1},
                                                    run_id=args.run_id))
            row = {"workload": name, "level": lvl, "rep": rep,
                   "agent_elapsed_ms": trace.agent_elapsed_ms,
                   "sandbox_init_ms": trace.sandbox_init_ms, "wall_time_ms": trace.wall_time_ms,
                   "exit_code": trace.exit_code, "output": trace.agent_output,
                   "violation": trace.has_safety_violation}
            sink.write(json.dumps(row) + "\n")
            sink.flush()
            if trace.agent_elapsed_ms is not None and not trace.has_safety_violation \
                    and trace.exit_code == 0:
                times.setdefault((name, lvl), []).append(trace.agent_elapsed_ms)
            if i % 20 == 0:
                print(f"  {i}/{len(plan)}", flush=True)

    summary = {}
    for name in WORKLOADS:
        l2, l3 = times.get((name, "L2"), []), times.get((name, "L3"), [])
        if not l2 or not l3:
            summary[name] = {"n_l2": len(l2), "n_l3": len(l3), "error": "missing runs"}
            continue
        m2, m3 = statistics.median(l2), statistics.median(l3)
        summary[name] = {"n_l2": len(l2), "n_l3": len(l3), "median_l2_ms": round(m2, 1),
                         "median_l3_ms": round(m3, 1), "ratio_l3_l2": round(m3 / m2, 2),
                         "diff_ms": round(m3 - m2, 1),
                         "diff_ci_ms": [round(x, 1) for x in bootstrap_diff(l2, l3)]}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
