"""H3: does combined safety-and-correctness validation predict task success better
than safety-only validation?

    python experiments/h3_judge_test.py runs/integ2-live runs/integ3-live runs/integ4-autogen-live --dry-run
    python experiments/h3_judge_test.py runs/integ2-live runs/integ3-live runs/integ4-autogen-live

Analysis plan, fixed before any judge score was seen (committed with this file):

Population. Every agent the live generator wrote in the given runs that passed
static analysis, the policy check and the sandbox's safety checks — the agents
a safety-only validator would deploy. Agents stopped for safety are excluded:
both validators reject them, so they cannot separate the two.

Outcome: task success. Whether the agent succeeds when the Task Executor runs
it: no safety violation and an output that matches the task's expected output
exactly (the strict profile). Agents and tool data are deterministic and the
executor runs the same input as validation, so this is computed from the
validation trace; for every agent that was actually executed the computed
outcome is checked against the executor's own record, and any disagreement
stops the analysis.

Predictors.
  safety-only   predicts success for every agent in the population.
  combined      safety plus the calibrated LLM judge: predicts success when the
                judge scores the agent 3 (fully correct). The judge, not the
                automatic exact match, is the correctness half: the outcome is
                defined by exact match, so using it as a predictor would be
                circular. The judge sees what it sees in the factory and in
                calibration — code without comments, what the tools return, the
                expected output, the task's scoring guide, the run's output.
  Sensitivity analysis: combined with judge score >= 2.

Measures. For each predictor: accuracy, precision (share of approved agents
that succeed — the deployment-side quantity), recall, specificity, with
Wilson 95% intervals. The two predictors are compared on the same agents with
an exact McNemar test on correct versus incorrect predictions. H3 is supported
if combined accuracy is higher and McNemar p < 0.05.

The judge runs behind its calibration gate, opened by replaying the recorded
calibration (docs/calibration/judge_calibration_results.csv), never bypassed.
Scores are cached per agent in --cache, so an interrupted run resumes.

Addendum, made after scoring started (2026-10-16, at item 104): one call
returned no parsable structured output and stopped the run. Each call is now
tried three times; an agent the judge still cannot score is recorded with
score null, left out of every measure, and counted in the report. This
changes no score already recorded and no measure in the plan above.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env", override=False)

from analyse_live_run import load  # noqa: E402
from strictness_sweep import mcnemar_exact, reports_for  # noqa: E402

from agentfactory.contracts import AgentContract, AgentSpec, ExecutionTrace  # noqa: E402
from agentfactory.corpus import load_corpus  # noqa: E402
from agentfactory.judge.correctness_judge import CorrectnessJudge  # noqa: E402
from agentfactory.judge.evidence import tool_data  # noqa: E402
from agentfactory.metrics.core import wilson_interval  # noqa: E402
from agentfactory.validation.aggregator import StrictnessProfile, assess  # noqa: E402

CALIBRATION = REPO_ROOT / "docs" / "calibration" / "judge_calibration_results.csv"


def population(run_dirs):
    """(run, agent row, spec, trace, outcome) for every safe agent that reached the sandbox."""
    tasks = {t.task_id: t for t in load_corpus()}
    out = []
    for run_dir in run_dirs:
        agents, layers, verdicts = load(run_dir)
        executed: dict[str, list[bool]] = {}
        for line in (run_dir / f"{run_dir.name}.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            if d.get("event_type") == "agent_deployed":
                executed.setdefault(d["agent_id"], []).append(bool(d.get("passed")))
        for agent_id, v in verdicts.items():
            ev = layers[agent_id]
            if "sandbox" not in ev or not ev["sandbox"]["passed"]:
                continue  # stopped for safety: both validators reject it
            g = agents[agent_id]
            task = tasks[v["task_id"]]
            spec = AgentSpec.from_source(
                task_id=v["task_id"], source=g["source"],
                contract=AgentContract(task_spec=g["task_spec"],
                                       authorized_tools=tuple(g["authorized_tools"])),
            )
            reports = reports_for(ev)
            succeeds = assess(reports, spec, StrictnessProfile.strict()).verdict().value == "PASS"
            if succeeds != (v["verdict"] == "PASS"):
                raise SystemExit(f"{agent_id}: recomputed outcome disagrees with the recorded verdict")
            for ran in executed.get(agent_id, []):
                if ran != succeeds:
                    raise SystemExit(f"{agent_id}: executor recorded success={ran}, computed {succeeds}")
            trace = ExecutionTrace.model_validate(ev["sandbox"]["detail"]["trace"])
            out.append((run_dir.name, g, task, spec, trace, succeeds, bool(executed.get(agent_id))))
    return out


def opened_judge() -> CorrectnessJudge:
    rows = list(csv.DictReader(CALIBRATION.open(encoding="utf-8")))
    judge = CorrectnessJudge()
    kappa = judge.calibrate([int(r["human"]) for r in rows], [int(r["judge"]) for r in rows])
    if not judge.gate.is_open:
        raise SystemExit(f"calibration gate closed (kappa {kappa:.3f})")
    print(f"judge gate open from recorded calibration: kappa {kappa:.3f} over {len(rows)} items")
    return judge


def measures(pred: list[bool], truth: list[bool]) -> dict:
    tp = sum(p and t for p, t in zip(pred, truth, strict=True))
    fp = sum(p and not t for p, t in zip(pred, truth, strict=True))
    fn = sum(not p and t for p, t in zip(pred, truth, strict=True))
    tn = sum(not p and not t for p, t in zip(pred, truth, strict=True))
    n = len(truth)

    def rate(k, m):
        return {"value": k / m if m else None, "k": k, "n": m, "ci": wilson_interval(k, m)}

    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "accuracy": rate(tp + tn, n), "precision": rate(tp, tp + fp),
            "recall": rate(tp, tp + fn), "specificity": rate(tn, tn + fp)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dirs", type=Path, nargs="+")
    ap.add_argument("--cache", type=Path, default=REPO_ROOT / "runs" / "h3_judge_scores.jsonl")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "runs" / "h3_judge_test.json")
    ap.add_argument("--dry-run", action="store_true", help="Count the population; call nothing.")
    args = ap.parse_args(argv)

    pop = population(args.run_dirs)
    print(f"population: {len(pop)} safe agents that reached the sandbox; "
          f"{sum(p[5] for p in pop)} succeed, {sum(not p[5] for p in pop)} fail; "
          f"{sum(p[6] for p in pop)} were executed and agree with the computed outcome")
    print("by run:", dict(Counter(p[0] for p in pop)))
    if args.dry_run:
        return 0

    cache: dict[str, dict] = {}
    if args.cache.exists():
        for line in args.cache.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            cache[row["key"]] = row
    judge = opened_judge()
    with args.cache.open("a", encoding="utf-8") as sink:
        for i, (run, g, task, spec, trace, succeeds, _) in enumerate(pop, 1):
            key = f"{run}:{g['agent_id']}"
            if key in cache:
                continue
            result, error = None, None
            for _ in range(3):
                try:
                    result = judge.score(
                        spec, trace, task.expected_output,
                        task_rubric=task.correctness_rubric,
                        tools_return=tool_data(task.available_tools, task.tool_fixtures),
                    )
                    break
                except RuntimeError as exc:
                    error = str(exc)
            row = {"key": key, "run": run, "task_id": task.task_id,
                   "score": result.score if result else None, "succeeds": succeeds,
                   "justification": result.justification if result else None,
                   "error": None if result else error,
                   "cost_usd": round(result.cost_usd, 5) if result else 0.0}
            sink.write(json.dumps(row) + "\n")
            sink.flush()
            cache[key] = row
            if i % 25 == 0:
                print(f"  {i}/{len(pop)} scored", flush=True)

    unscored = [f"{p[0]}:{p[1]['agent_id']}" for p in pop
                if cache[f"{p[0]}:{p[1]['agent_id']}"]["score"] is None]
    pop = [p for p in pop if f"{p[0]}:{p[1]['agent_id']}" not in unscored]
    keys = [f"{p[0]}:{p[1]['agent_id']}" for p in pop]
    truth = [p[5] for p in pop]
    scores = [cache[k]["score"] for k in keys]
    safety_only = [True] * len(pop)
    results = {"n": len(pop), "unscored": unscored,
               "judge_cost_usd": round(sum(cache[k]["cost_usd"] for k in keys), 4),
               "score_by_outcome": {
                   "succeeds": dict(Counter(s for s, t in zip(scores, truth, strict=True) if t)),
                   "fails": dict(Counter(s for s, t in zip(scores, truth, strict=True) if not t))},
               "predictors": {}}
    for name, pred in (("safety_only", safety_only),
                       ("combined_judge_3", [s == 3 for s in scores]),
                       ("combined_judge_ge2", [s >= 2 for s in scores])):
        results["predictors"][name] = measures(pred, truth)
    for name in ("combined_judge_3", "combined_judge_ge2"):
        pred = [s == 3 for s in scores] if name.endswith("_3") else [s >= 2 for s in scores]
        right_c = [p == t for p, t in zip(pred, truth, strict=True)]
        right_s = [p == t for p, t in zip(safety_only, truth, strict=True)]
        b = sum(c and not s for c, s in zip(right_c, right_s, strict=True))  # only combined right
        c = sum(s and not cc for cc, s in zip(right_c, right_s, strict=True))  # only safety right
        results[f"mcnemar_{name}_vs_safety_only"] = {"combined_only_right": b,
                                                    "safety_only_right": c,
                                                    "p": mcnemar_exact(b, c)}
    args.out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
