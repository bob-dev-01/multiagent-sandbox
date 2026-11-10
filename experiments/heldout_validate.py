"""Validate the held-out set (heldout_generate.py) through the pipeline.

    python experiments/heldout_validate.py runs/heldout-set --level L3 --run-id heldout-l3

Each agent is validated once, with the contract its writer declared, under the
strict profile and with short-circuiting off, so that, as in the layer
ablation, every layer's own finding is on record next to the verdict. With
short-circuiting off the verdict on safety is the same as with it on: an agent
any layer flags is unsafe either way. Code runs only inside the sandbox. A run
that exceeds its time limit or never reaches the sandbox is recorded as an
infrastructure failure, never as a verdict, and is retried on the next start.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from run_matrix import build_runner  # noqa: E402

from agentfactory.contracts import (  # noqa: E402
    AgentContract,
    AgentSpec,
    IsolationLevel,
    SecurityPolicy,
)
from agentfactory.corpus import load_corpus  # noqa: E402
from agentfactory.validation.aggregator import StrictnessProfile  # noqa: E402
from agentfactory.validation.layer2_policy import EnterprisePolicy  # noqa: E402
from agentfactory.validation.pipeline import PipelineConfig, ValidationPipeline  # noqa: E402

SIGNALS = ("syscalls_blocked", "network_attempts", "fs_violations", "unauthorized_tool_calls",
           "runtime_violations")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("set_dir", type=Path)
    ap.add_argument("--level", default="L3", choices=["L2", "L3"])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--run-timeout", type=int, default=240)
    ap.add_argument("--k8s-namespace", default="agentfactory-sandbox")
    ap.add_argument("--kube-context", default="agentfac-aks")
    args = ap.parse_args(argv)

    tasks = {t.task_id: t for t in load_corpus()}
    policy = EnterprisePolicy.load()
    agents = [json.loads(x) for x in (args.set_dir / "agents.jsonl").read_text(encoding="utf-8").splitlines()]
    agents = [a for a in agents if "python_source" in a]
    out_dir = REPO_ROOT / "runs" / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results = out_dir / "results.jsonl"
    done = set()
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if "error" not in row:
                done.add(row["id"])

    runner = build_runner(IsolationLevel(args.level), SimpleNamespace(
        l2_backend="pod", k8s_namespace=args.k8s_namespace, kube_context=args.kube_context,
        require_seccomp=False))
    pipeline = ValidationPipeline(runner, PipelineConfig.default(
        profile=StrictnessProfile.strict(), policy=policy, use_bandit=True,
        test_inputs=({"case": 1},), short_circuit=False))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    pending = [a for a in agents if a["id"] not in done]
    print(f"{len(agents)} agents, {len(done)} done, {len(pending)} to run", flush=True)
    with results.open("a", encoding="utf-8") as sink:
        for i, a in enumerate(pending, 1):
            task = tasks[a["task_id"]]
            contract = AgentContract(
                task_spec=task.description,
                authorized_tools=tuple(a["authorized_tools"]),
                evidence_requirements=tuple(sorted(policy.required_evidence)),
                security_policy=SecurityPolicy(),
            )
            spec = AgentSpec.from_source(task_id=task.task_id, source=a["python_source"],
                                         contract=contract, generator_model=a.get("model"))
            future = pool.submit(pipeline.validate, spec, expected_output=task.expected_output,
                                 tool_fixtures=dict(task.tool_fixtures))
            try:
                result = future.result(timeout=args.run_timeout)
            except Exception as exc:  # noqa: BLE001 — infrastructure, never a verdict
                if isinstance(exc, concurrent.futures.TimeoutError):
                    subprocess.run(["taskkill", "/F", "/T", "/IM", "kubectl.exe"],
                                   capture_output=True, check=False)
                    pool.shutdown(wait=False, cancel_futures=True)
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                sink.write(json.dumps({"id": a["id"], "error": f"{type(exc).__name__}: {exc}"[:300]}) + "\n")
                sink.flush()
                print(f"  {i}/{len(pending)} {a['id']} INFRASTRUCTURE {type(exc).__name__}", flush=True)
                continue
            rep = {r.layer.value: r for r in result.layer_reports}
            trace = rep["sandbox"].trace if "sandbox" in rep else None
            row = {
                "id": a["id"], "task_id": a["task_id"], "kind": a["kind"], "classes": a["classes"],
                "style": a["style"], "code_sha256": spec.code_sha256, "level": args.level,
                "verdict": result.verdict.value,
                "static_flagged": not rep["static"].passed,
                "static_findings": [str(f) for f in rep["static"].findings],
                "policy_flagged": not rep["policy"].passed,
                "policy_findings": [str(f) for f in rep["policy"].findings],
                "sandbox_flagged": (not rep["sandbox"].passed) if "sandbox" in rep else None,
                "signals": {s: list(getattr(trace, s)) for s in SIGNALS} if trace else None,
                # Judged by the aggregator, not the sandbox report: under the strict
                # profile an overrun of the declared limits is a safety violation.
                "resource_overruns": list(trace.exceeds(spec.contract.resource_limits)) if trace else None,
                "timed_out": trace.timed_out if trace else None,
                "output_match_score": trace.output_match_score if trace else None,
                "terminated_reason": trace.terminated_reason if trace else None,
                "kernel_release": trace.kernel_release if trace else None,
            }
            sink.write(json.dumps(row) + "\n")
            sink.flush()
            print(f"  {i}/{len(pending)} {a['id']} {a['kind']} {a['classes']} -> {row['verdict']} "
                  f"(static={row['static_flagged']} policy={row['policy_flagged']} "
                  f"sandbox={row['sandbox_flagged']})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
