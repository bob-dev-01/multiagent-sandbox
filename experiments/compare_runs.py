"""Headline numbers of the thesis, computed the same way from any runs/ root.

    python experiments/compare_runs.py <runs_root> [--json out.json]

Missing inputs yield None, so the same script reads the recorded runs and the rerun.
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

UNSAFE = {"unsafe_only", "both"}
EXPECTED = {"safe_correct": "PASS", "incorrect_only": "FAIL_INCORRECT",
            "unsafe_only": "FAIL_UNSAFE", "both": "FAIL_UNSAFE"}


def jl(p: Path):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def jload(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def boot(a, b, n=5000, seed=1):
    rng = random.Random(seed)
    d = sorted(statistics.median(rng.choices(b, k=len(b))) - statistics.median(rng.choices(a, k=len(a)))
               for _ in range(n))
    return round(d[int(0.025 * n)], 1), round(d[int(0.975 * n)], 1)


def matrix(run_dir: Path):
    f = run_dir / f"{run_dir.name}.jsonl"
    if not f.exists():
        return None
    rows = jl(f)
    out = {}
    cells = [r for r in rows if r.get("event_type") == "verdict_emitted" and (r.get("detail") or {}).get("cell_key")]
    sb = [r for r in rows if r.get("layer") == "sandbox"]
    done = [r for r in rows if r.get("event_type") == "run_completed"]
    out["n_cells"] = len(cells)
    out["run_completed"] = done[0]["detail"] if done else None
    by_level = defaultdict(Counter)
    for r in cells:
        by_level[r["isolation_level"]][f'{r["detail"]["ground_truth"]}->{r["verdict"]}'] += 1
    out["label_x_verdict"] = {k: dict(sorted(v.items())) for k, v in sorted(by_level.items())}
    out["all_match"] = all(EXPECTED[r["detail"]["ground_truth"]] == r["verdict"] for r in cells)
    out["mismatches"] = [r["detail"]["cell_key"] for r in cells if EXPECTED[r["detail"]["ground_truth"]] != r["verdict"]][:20]
    out["unsafe_passed"] = sum(1 for r in cells if r["detail"]["ground_truth"] in UNSAFE and r["verdict"] == "PASS")
    out["n_unsafe"] = sum(1 for r in cells if r["detail"]["ground_truth"] in UNSAFE)
    out["safe_flagged_unsafe"] = sum(1 for r in cells if r["detail"]["ground_truth"] not in UNSAFE and r["verdict"] == "FAIL_UNSAFE")
    credits = [(r.get("detail") or {}).get("cpu_credits_remaining") for r in cells]
    credits = [c for c in credits if c is not None]
    out["cpu_credits"] = [min(credits), max(credits)] if credits else None
    lat = defaultdict(lambda: defaultdict(list))
    kern = defaultdict(Counter)
    for r in sb:
        t = r["detail"]["trace"]
        lvl = r["isolation_level"]
        kern[lvl][t.get("kernel_release")] += 1
        if t.get("sandbox_init_ms") is not None:
            lat["sandbox_init_ms"][lvl].append(t["sandbox_init_ms"])
        m = t.get("milestones_ms") or {}
        if "applied" in m and "terminated" in m:
            lat["apply_to_terminated_ms"][lvl].append(m["terminated"] - m["applied"])
        if t.get("wall_time_ms") is not None:
            lat["wall_time_ms"][lvl].append(t["wall_time_ms"])
        if t.get("agent_elapsed_ms") is not None:
            lat["agent_elapsed_ms"][lvl].append(t["agent_elapsed_ms"])
    out["kernels"] = {k: dict(v) for k, v in kern.items()}
    out["n_sandbox_runs"] = {k: len(v) for k, v in lat["wall_time_ms"].items()}
    out["latency"] = {}
    for name, d in lat.items():
        e = {lvl: round(statistics.median(v), 1) for lvl, v in sorted(d.items())}
        if "L2" in d and "L3" in d and name != "agent_elapsed_ms":
            e["L3-L2"] = round(statistics.median(d["L3"]) - statistics.median(d["L2"]), 1)
            e["CI"] = boot(d["L2"], d["L3"])
        out["latency"][name] = e
    return out


def subset_check(run_dir: Path):
    m = matrix(run_dir)
    if m is None:
        return None
    return {k: m[k] for k in ("n_cells", "label_x_verdict", "all_match", "mismatches", "run_completed")}


def live(run_dir: Path):
    s = jload(run_dir / "summary.json")
    if s is None:
        return None
    a = jload(run_dir / "analysis.json") or {}
    c, w = s["phases"]["cold"], s["phases"]["warm"]

    def r3(x):
        return round(x, 3)

    out = {
        "TSR cold": r3(c["task_success_rate"]), "TSR cold CI": [r3(x) for x in c["tsr_ci"]],
        "TSR warm": r3(w["task_success_rate"]), "TSR warm CI": [r3(x) for x in w["tsr_ci"]],
        "ARR warm": r3(w["agent_reuse_rate"]), "ARR CI": [r3(x) for x in w["arr_ci"]],
        "escalated cold/warm": [r3(c["escalation_rate"]), r3(w["escalation_rate"])],
        "outcomes cold": c["outcomes"], "outcomes warm": w["outcomes"],
        "MTTD cold/warm s": [round(c["mean_time_to_deploy_sec"], 1), round(w["mean_time_to_deploy_sec"], 1)],
    }
    for k in ("verdicts", "executions_unsafe", "cost_usd", "stopped"):
        if k in s:
            out[k] = s[k]
    if "verdicts" in s:
        out["agents generated"] = sum(s["verdicts"].values())
    if "safety" in s:
        out["safety"] = {k: s["safety"][k] for k in ("n_unsafe", "n_safe", "unsafe_deployment_rate",
                                                       "catastrophic_error_rate", "false_negative_rate")}
        out["n_validations"] = s.get("n_validations")
    if a.get("profiles"):
        out["PASS by profile"] = {p: v.get("PASS") for p, v in a["profiles"].items()}
    if a.get("by_wording"):
        out["by_wording"] = a["by_wording"]
    tj = run_dir / "tasks.jsonl"
    if tj.exists():
        t = jl(tj)
        out["attempts>1"] = sum(1 for x in t if (x.get("attempts") or 1) > 1)
        out["regenerations"] = sum(max(0, (x.get("attempts") or 1) - 1) for x in t)
        out["executed_unsafe(tasks)"] = sum(1 for x in t if x.get("executed_unsafe"))
        out["escalation reasons"] = dict(Counter((x.get("escalation_reason") or "")[:60] for x in t if x.get("escalation_reason")))
    return out


def main():
    root = Path(sys.argv[1])
    res = {
        "matrix L2/L3": matrix(root / "full2-l2l3"),
        "L1": jload(root / "full2-l1" / "summary.json"),
        "strictness sweep L2/L3": jload(root / "full2-l2l3" / "strictness_sweep.json"),
        "integ1 (replay)": live(root / "integ1-l2"),
        "integ4 (live, AutoGen)": live(root / "integ4-autogen-live"),
        "ablation": jload(root / "ablation4-l2l3" / "layer_ablation.json"),
        "fpcheck": subset_check(root / "fpcheck-corpus-l2l3"),
        "revalidate": jload(root / "revalidate-generated-l3" / "summary.json"),
        "workload": jload(root / "workload-l2l3" / "summary.json"),
        "h1": jload(root / "h1_baseline.json"),
        "h3": jload(root / "h3_judge_test.json"),
        "final": jload(root / "final_analysis.json"),
    }
    out = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    if out:
        Path(out).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    m = res["matrix L2/L3"]
    if m:
        print(json.dumps({k: m[k] for k in ("n_cells", "all_match", "unsafe_passed", "n_unsafe", "safe_flagged_unsafe",
                                            "cpu_credits", "n_sandbox_runs", "latency", "kernels", "run_completed")}, indent=1))


if __name__ == "__main__":
    main()
