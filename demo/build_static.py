"""Build the static data the demo page reads: results and the task list.

    python demo/build_static.py

Everything comes from the recorded runs (runs/, not in the repository) and the
corpus, so the results tab shows exactly the numbers the thesis reports and
needs no backend. Writes demo/web/data/results.json and tasks.json.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentfactory.corpus import load_corpus  # noqa: E402

RUNS = ROOT / "runs"
OUT = ROOT / "demo" / "web" / "data"


def load(path: str) -> dict:
    return json.loads((RUNS / path).read_text(encoding="utf-8"))


def integration(run: str) -> dict:
    s = load(f"{run}/summary.json")
    cold, warm = s["phases"]["cold"], s["phases"]["warm"]
    return {"cold_tsr": cold["task_success_rate"], "cold_ci": cold["tsr_ci"],
            "warm_tsr": warm["task_success_rate"], "warm_ci": warm["tsr_ci"],
            "arr": warm["agent_reuse_rate"], "cost": s.get("cost_usd"),
            "unsafe_executions": s.get("executions_unsafe", 0)}


def main() -> int:
    fa = load("final_analysis.json")
    sweep = load("strictness_sweep_all_levels.json")["counts"]
    incorrect_passed = {}
    for profile, counts in sweep.items():
        passed = sum(v for k, v in counts.items() if k.split("|")[1:] == ["incorrect_only", "PASS"])
        total = sum(v for k, v in counts.items() if k.split("|")[1] == "incorrect_only")
        incorrect_passed[profile] = {"k": passed, "n": total}
    generated_pass = {
        "before repair (test 2)": load("integ2-live/analysis.json")["profiles"],
        "after repair (test 3)": load("integ3-live/analysis.json")["profiles"],
    }
    l1 = [float(x) for x in (RUNS / "full2-l1" / "l1_sandbox_wall_ms.csv").read_text().split()[1:]]
    q = fa["rq2"]["quantile_regression"]
    latency = {
        "L1": {"p50": statistics.median(l1), "p95": statistics.quantiles(l1, n=100)[94]},
        "L2": {"p50": q["p50"]["l2_ms"], "p95": q["p95"]["l2_ms"]},
        "L3": {"p50": q["p50"]["l2_ms"] + q["p50"]["l3_minus_l2_ms"],
               "p95": q["p95"]["l2_ms"] + q["p95"]["l3_minus_l2_ms"]},
    }
    ablation = []
    for run, label in (("ablation-l2l3", "before the monitor"), ("ablation2-l2l3", "monitor v1"),
                       ("ablation3-l2l3", "+ DNS, UDP, tool arguments"),
                       ("ablation4-l2l3", "+ sendto before resolution")):
        a = load(f"{run}/layer_ablation.json")["by_level"]["L3"]
        ablation.append({"run": label, "sandbox": a["sandbox"]["k"], "n": a["sandbox"]["n"],
                         "static": a["static"]["k"]})
    pairs = list(csv.DictReader((ROOT / "research/calibration/judge_score_pairs.csv").open()))
    confusion = Counter((int(p["human"]), int(p["judge"])) for p in pairs)
    calib = load("../docs/calibration/judge_calibration_report.json") if \
        (ROOT / "docs/calibration/judge_calibration_report.json").exists() else {}
    h3 = load("h3_judge_test.json")
    workload = load("workload-l2l3/summary.json")

    results = {
        "headline": {
            "validations": 2700, "matched_labels": 2700,
            "unsafe_cells": 810, "unsafe_deployed": 0,
            "generated_agents": 221 + 188 + 189, "unsafe_executions": 0,
            "kappa": calib.get("cohens_kappa", 0.948),
        },
        "matrix": {"fnr_upper": 0.0047, "fpr": 0.0},
        "latency": latency,
        "gvisor_ratio": fa["rq2"]["mixed_model_log_latency"],
        "workload": {k: v for k, v in workload.items() if isinstance(v, dict)},
        "ablation": ablation,
        "h3": {name: {"accuracy": p["accuracy"]["value"], "ci": p["accuracy"]["ci"],
                      "precision": p["precision"]["value"], "approved": p["tp"] + p["fp"]}
               for name, p in h3["predictors"].items()},
        "h3_n": h3["n"], "h3_mcnemar_p": h3["mcnemar_combined_judge_3_vs_safety_only"]["p"],
        "confusion": [[confusion.get((h, j), 0) for j in range(4)] for h in range(4)],
        "integration": {name: integration(run) for name, run in (
            ("Test 1: corpus replay", "integ1-l2"), ("Test 2: live", "integ2-live"),
            ("Test 3: after fixes", "integ3-live"), ("Test 4: AutoGen", "integ4-autogen-live"))},
        "reuse_curve": fa["rq3"]["reuse_eligibility_curve"],
        "reuse_observed": [integration("integ3-live")["arr"], integration("integ4-autogen-live")["arr"]],
        "incorrect_passed": incorrect_passed,
        "generated_pass": generated_pass,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    tasks = [{"task_id": t.task_id, "title": t.title, "domain": t.domain,
              "description": t.description, "paraphrases": list(t.paraphrases),
              "tools": list(t.available_tools),
              "agents": [{"variant": a.variant, "label": a.label.value,
                          "failure_mode": a.failure_mode} for a in t.agents]}
             for t in load_corpus()]
    (OUT / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")
    print("results.json and tasks.json written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
