"""Re-validate the live-generated agents in the sandbox with the current harness.

    python experiments/revalidate_generated.py runs/integ2-live runs/integ3-live \\
        runs/integ4-autogen-live --level L3 --run-id revalidate-generated-l3

A false-positive check for a change to the harness. Every agent the live
generator wrote that passed every safety check in its original run (the H3
population, one per distinct code hash) is validated again at one isolation
level; any agent the new harness now flags unsafe is listed with what it
flagged. The verdicts on correctness are compared too: the harness must not
change what an agent returns.

Each run has a time limit; a run that exceeds it is recorded as an
infrastructure failure, never as a verdict. Agents already in the run's
results are skipped, so the script resumes.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from h3_judge_test import population  # noqa: E402
from run_matrix import build_runner  # noqa: E402

from agentfactory.contracts import IsolationLevel  # noqa: E402
from agentfactory.validation.aggregator import StrictnessProfile  # noqa: E402
from agentfactory.validation.layer2_policy import EnterprisePolicy  # noqa: E402
from agentfactory.validation.pipeline import PipelineConfig, ValidationPipeline  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dirs", type=Path, nargs="+")
    ap.add_argument("--level", default="L3", choices=["L2", "L3"])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--run-timeout", type=int, default=240)
    ap.add_argument("--k8s-namespace", default="agentfactory-sandbox")
    ap.add_argument("--kube-context", default="agentfac-aks")
    args = ap.parse_args(argv)

    out_dir = REPO_ROOT / "runs" / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results = out_dir / "results.jsonl"

    seen: dict[str, tuple] = {}
    for item in population(args.run_dirs):
        seen.setdefault(item[3].code_sha256, item)  # one per distinct code
    done = set()
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if "error" not in row:
                done.add(row["code_sha256"])

    runner = build_runner(IsolationLevel(args.level), SimpleNamespace(
        l2_backend="pod", k8s_namespace=args.k8s_namespace, kube_context=args.kube_context,
        require_seccomp=False))
    pipeline = ValidationPipeline(runner, PipelineConfig.default(
        profile=StrictnessProfile.strict(), policy=EnterprisePolicy.load(),
        use_bandit=True, test_inputs=({"case": 1},)))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    pending = [v for k, v in seen.items() if k not in done]
    print(f"{len(seen)} distinct agents, {len(done)} done, {len(pending)} to run", flush=True)
    with results.open("a", encoding="utf-8") as sink:
        for i, (run, _g, task, spec, _trace, succeeds, _) in enumerate(pending, 1):
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
                sink.write(json.dumps({"code_sha256": spec.code_sha256, "run": run,
                                       "error": f"{type(exc).__name__}: {exc}"[:300]}) + "\n")
                sink.flush()
                continue
            sandbox = next((r for r in result.layer_reports if r.layer.value == "sandbox"), None)
            trace = sandbox.trace if sandbox else None
            row = {
                "code_sha256": spec.code_sha256, "run": run, "task_id": task.task_id,
                "recorded_succeeds": succeeds, "verdict": result.verdict.value,
                "runtime_violations": list(trace.runtime_violations) if trace else None,
                "fs_violations": list(trace.fs_violations) if trace else None,
                "other_safety": bool(trace and (trace.network_attempts
                                                or trace.unauthorized_tool_calls)),
            }
            sink.write(json.dumps(row) + "\n")
            sink.flush()
            if i % 25 == 0:
                print(f"  {i}/{len(pending)}", flush=True)

    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()]
    ok = [r for r in rows if "error" not in r]
    newly_unsafe = [r for r in ok if r["verdict"] in ("FAIL_UNSAFE", "FAIL_BOTH")]
    changed = [r for r in ok if (r["verdict"] == "PASS") != r["recorded_succeeds"]]
    summary = {
        "level": args.level, "distinct_agents": len(seen), "validated": len(ok),
        "infrastructure_failures": len(rows) - len(ok),
        "verdicts": dict(Counter(r["verdict"] for r in ok)),
        "newly_unsafe": newly_unsafe,
        "correctness_changed": changed,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("newly_unsafe", "correctness_changed")}, indent=2))
    print(f"newly unsafe: {len(newly_unsafe)}; correctness changed: {len(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
