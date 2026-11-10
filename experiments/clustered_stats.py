"""Effective sample sizes and task-clustered intervals for the classification claims.

    python experiments/clustered_stats.py

Supervisor's review, I4: the 2,700 corpus validations are not 2,700
independent observations. Layers 1-2 are deterministic, so the three replicates
and the three levels repeat the same verdict for every agent that is stopped
before the sandbox, and the ten agents of a task share its data and its author.
This script reports each classification claim at the level of the distinct
agent, with the agents' agreement across their cells, and gives intervals that
treat the task as the unit of resampling (a cluster bootstrap over the 30
tasks) next to the per-item Wilson intervals used so far. Where no event
occurred a bootstrap is degenerate, so the bound is reported per agent and per
task instead. Nothing here is new data; it reads the recorded runs.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentfactory.metrics.core import wilson_interval  # noqa: E402

UNSAFE = {"unsafe_only", "both"}
EXPECTED = {"safe_correct": "PASS", "incorrect_only": "FAIL_INCORRECT",
            "unsafe_only": "FAIL_UNSAFE", "both": "FAIL_UNSAFE"}
B = 10_000
SEED = 20261109


def upper(k: int, n: int) -> dict:
    """Two bounds for a proportion with few or no events."""
    return {"k": k, "n": n, "wilson_95": wilson_interval(k, n),
            "exact_one_sided_95_upper": 1 - 0.05 ** (1 / n) if k == 0 else None}


def cluster_boot(units: dict[str, list], stat, b: int = B, seed: int = SEED):
    """Percentile interval of `stat` over bootstrap resamples of whole tasks."""
    rng = random.Random(seed)
    keys = sorted(units)
    vals = []
    for _ in range(b):
        sample = [x for k in rng.choices(keys, k=len(keys)) for x in units[k]]
        v = stat(sample)
        if v is not None:
            vals.append(v)
    vals.sort()
    return [round(vals[int(0.025 * len(vals))], 4), round(vals[int(0.975 * len(vals)) - 1], 4)]


def binom_two_sided(k: int, n: int) -> float:
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(k, n - k) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def corpus(matrix_dirs: list[Path]) -> dict:
    cells: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for d in matrix_dirs:
        for line in (d / f"{d.name}.jsonl").read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            det = r.get("detail") or {}
            if r.get("event_type") == "verdict_emitted" and det.get("cell_key"):
                task, variant, _level, _rep = det["cell_key"].split(":")
                cells[(task, variant)].append((det["ground_truth"], r["verdict"]))
    agents = {k: {"label": v[0][0], "verdicts": [x[1] for x in v]} for k, v in cells.items()}
    n_cells = sum(len(a["verdicts"]) for a in agents.values())
    consistent = sum(len(set(a["verdicts"])) == 1 for a in agents.values())
    unsafe = {k: a for k, a in agents.items() if a["label"] in UNSAFE}
    others = {k: a for k, a in agents.items() if a["label"] not in UNSAFE}
    missed = [k for k, a in unsafe.items() if any(v == "PASS" for v in a["verdicts"])]
    over = [k for k, a in others.items() if any(v in ("FAIL_UNSAFE", "FAIL_BOTH") for v in a["verdicts"])]
    wrong = [k for k, a in agents.items() if any(v != EXPECTED[a["label"]] for v in a["verdicts"])]
    tasks = sorted({k[0] for k in agents})
    tasks_with_miss = sorted({k[0] for k in missed})
    return {
        "cells": n_cells, "distinct_agents": len(agents), "tasks": len(tasks),
        "agents_with_one_verdict_across_all_their_cells": consistent,
        "agents_classified_wrongly_in_any_cell": len(wrong),
        "fnr_per_cell": upper(sum(1 for k in unsafe for v in unsafe[k]["verdicts"] if v == "PASS"),
                              sum(len(a["verdicts"]) for a in unsafe.values())),
        "fnr_per_agent": upper(len(missed), len(unsafe)),
        "fnr_per_task": upper(len(tasks_with_miss), len(tasks)),
        "unsafe_flag_on_safe_or_incorrect_per_agent": upper(len(over), len(others)),
    }


def h3(cache: Path, runs: set[str]) -> dict:
    rows = [json.loads(x) for x in cache.read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if r["run"] in runs and r["score"] is not None]
    units: dict[str, list] = defaultdict(list)
    for r in rows:
        units[r["task_id"]].append((r["score"] == 3, r["succeeds"]))

    def acc_safety(s):
        return sum(t for _, t in s) / len(s)

    def acc_combined(s):
        return sum(p == t for p, t in s) / len(s)

    def diff(s):
        return acc_combined(s) - acc_safety(s)

    flat = [x for v in units.values() for x in v]
    better = worse = 0
    for v in units.values():
        b = sum((p == t) and not t for p, t in v)       # only the combined predictor right
        c = sum((p != t) and t for p, t in v)           # only safety-only right
        better += b > c
        worse += b < c
    k_s, k_c = sum(t for _, t in flat), sum(p == t for p, t in flat)
    return {
        "agents": len(flat), "tasks": len(units),
        "accuracy_safety_only": round(k_s / len(flat), 4),
        "accuracy_combined": round(k_c / len(flat), 4),
        "wilson_safety_only": wilson_interval(k_s, len(flat)),
        "wilson_combined": wilson_interval(k_c, len(flat)),
        "cluster_boot_safety_only": cluster_boot(units, acc_safety),
        "cluster_boot_combined": cluster_boot(units, acc_combined),
        "difference": round((k_c - k_s) / len(flat), 4),
        "cluster_boot_difference": cluster_boot(units, diff),
        "tasks_where_combined_better": better, "tasks_where_safety_only_better": worse,
        "task_level_sign_test_p": binom_two_sided(worse, better + worse),
    }


def ablation(run_dir: Path) -> dict:
    layers: dict[str, dict] = defaultdict(dict)
    cells = {}
    for line in (run_dir / f"{run_dir.name}.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("event_type") == "layer_completed":
            layers[d["agent_id"]][d["layer"]] = d
        elif d.get("event_type") == "verdict_emitted" and "cell_key" in (d.get("detail") or {}):
            cells[d["agent_id"]] = d
    out = {}
    for level in ("L2", "L3"):
        units: dict[str, list] = defaultdict(list)
        for agent_id, v in cells.items():
            task, _variant, lvl, _ = v["detail"]["cell_key"].split(":")
            if lvl != level or "sandbox" not in layers[agent_id]:
                continue
            units[task].append(not layers[agent_id]["sandbox"]["passed"])
        flat = [x for v in units.values() for x in v]
        k = sum(flat)
        out[level] = {"sandbox_alone": k, "n": len(flat), "rate": round(k / len(flat), 4),
                      "wilson": wilson_interval(k, len(flat)),
                      "cluster_boot": cluster_boot(units, lambda s: sum(s) / len(s))}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--matrix", type=Path, nargs="+",
                    default=[REPO_ROOT / "runs" / "full2-l2l3", REPO_ROOT / "runs" / "full2-l1"])
    ap.add_argument("--h3-cache", type=Path, default=REPO_ROOT / "runs" / "h3_judge_scores.jsonl")
    ap.add_argument("--h3-runs", default="integ2-live,integ3-live,integ4-autogen-live")
    ap.add_argument("--ablation", type=Path, nargs="*", default=[REPO_ROOT / "runs" / "ablation4-l2l3"])
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "runs" / "clustered_stats.json")
    args = ap.parse_args(argv)

    res = {"bootstrap": {"resamples": B, "seed": SEED, "unit": "task"},
           "corpus": corpus(args.matrix),
           "h3": h3(args.h3_cache, set(args.h3_runs.split(","))),
           "ablation": {str(d): ablation(d) for d in args.ablation}}
    args.out.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
