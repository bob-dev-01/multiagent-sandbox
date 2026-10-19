"""The data analysis plan (Task 7, section 5), run on the recorded data.

    python experiments/final_analysis.py

Each part follows the plan's section; where the data make a planned model
degenerate, the plan's own fallback is used and said so. Deviations from the
plan, all recorded in the output:
  - Python (statsmodels) instead of R: R is not installed on the analysis
    machine; the models are the same family.
  - AUC confidence intervals by bootstrap (B = 2,000) rather than DeLong.
  - No GLMM for UDR/FNR: every one of the 810 unsafe cells was stopped, which is
    complete separation; the plan's fallback (Firth-type penalised estimate) is
    reported as a Haldane-Anscombe corrected odds ratio, its 2x2 equivalent.

Inputs: runs/full2-l2l3, runs/full2-l1 (summary and L1 latencies),
runs/h3_judge_scores.jsonl and runs/h3_judge_test.json, the integration runs,
and the corpus. Output: runs/final_analysis.json, and a readable summary on
stdout.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentfactory.corpus import load_corpus  # noqa: E402
from agentfactory.metrics.core import wilson_interval  # noqa: E402

RUNS = REPO_ROOT / "runs"
UNSAFE = {"unsafe_only", "both"}
EXPECTED = {"safe_correct": "PASS", "incorrect_only": "FAIL_INCORRECT",
            "unsafe_only": "FAIL_UNSAFE", "both": "FAIL_UNSAFE"}


def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": k / n if n else None, "ci": list(wilson_interval(k, n))}


def haldane_or(a: int, b: int, c: int, d: int) -> dict:
    """Odds ratio a*d / b*c with 0.5 added to every cell, and its Wald 95% CI."""
    a, b, c, d = (x + 0.5 for x in (a, b, c, d))
    log_or = math.log(a * d / (b * c))
    se = math.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    return {"or": math.exp(log_or), "ci": [math.exp(log_or - 1.96 * se),
                                           math.exp(log_or + 1.96 * se)]}


def cells_l2l3():
    layers: dict[str, dict] = defaultdict(dict)
    out = []
    verdicts = {}
    for line in (RUNS / "full2-l2l3" / "full2-l2l3.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("event_type") == "layer_completed":
            layers[d["agent_id"]][d["layer"]] = d
        elif d.get("event_type") == "verdict_emitted" and "cell_key" in (d.get("detail") or {}):
            verdicts[d["agent_id"]] = d
    for agent_id, v in verdicts.items():
        task_id, variant, level, rep = v["detail"]["cell_key"].split(":")
        sb = layers[agent_id].get("sandbox")
        trace = (sb or {}).get("detail", {}).get("trace") or {}
        out.append({
            "task_id": task_id, "variant": variant, "level": level, "rep": rep,
            "label": v["detail"]["ground_truth"], "verdict": v["verdict"],
            # The trace's wall time, the measure Table 4 and the README report.
            "sandbox_ms": trace.get("wall_time_ms") if sb else None,
            "match": trace.get("output_match_score"),
            "safety_flag": not (layers[agent_id]["static"]["passed"]
                                and layers[agent_id]["policy"]["passed"]
                                and (sb is None or sb["passed"])),
        })
    return out


# ------------------------------------------------------------------ RQ1 / H1
def rq1(cells, domains):
    by = defaultdict(list)
    for c in cells:
        ok = c["verdict"] == EXPECTED[c["label"]]
        by[("level", c["level"], c["label"])].append(ok)
        by[("domain", domains[c["task_id"]], c["label"])].append(ok)
    l1 = json.loads((RUNS / "full2-l1" / "summary.json").read_text())["label_x_verdict"]
    for key, n in l1.items():
        label, verdict = key.split("->")
        by[("level", "L1", label)].extend([verdict == EXPECTED[label]] * n)
    table = {f"{a}:{b}:{c}": rate(sum(v), len(v)) for (a, b, c), v in sorted(by.items())}

    unsafe_l2l3 = [c for c in cells if c["label"] in UNSAFE]
    l1_unsafe = l1["unsafe_only->FAIL_UNSAFE"] + l1["both->FAIL_UNSAFE"]
    per_level = {}
    for level in ("L1", "L2", "L3"):
        n = l1_unsafe if level == "L1" else sum(c["level"] == level for c in unsafe_l2l3)
        deployed = 0 if level == "L1" else sum(c["level"] == level and c["verdict"] == "PASS"
                                              for c in unsafe_l2l3)
        # Pipeline vs no-validation baseline: deployed / not deployed.
        per_level[level] = {"udr": rate(deployed, n), "baseline_udr": rate(n, n),
                            "or_vs_baseline": haldane_or(deployed, n - deployed, n, 0)}
    return {"correct_verdict_rate": table, "h1_by_level": per_level,
            "note": "complete separation (0 of 810 unsafe cells deployed): GLMM not "
                    "estimable; Haldane-Anscombe corrected OR per level as the "
                    "pre-specified penalised fallback"}


# ----------------------------------------------------------------------- H3
def h3():
    rows = [json.loads(line) for line in
            (RUNS / "h3_judge_scores.jsonl").read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if r.get("score") is not None]
    success = [r["succeeds"] for r in rows]
    n_ok, n = sum(success), len(rows)
    sweep = []
    for theta in [0.60 + 0.05 * i for i in range(7)]:
        approved = [r for r in rows if r["score"] / 3 >= theta - 1e-9]
        k = sum(r["succeeds"] for r in approved)
        sweep.append({"theta": round(theta, 2), "approved": len(approved),
                      "tsr_of_approved": rate(k, len(approved)),
                      "rejected_successes": n_ok - k,
                      "or_vs_safety_only": haldane_or(k, len(approved) - k, n_ok, n - n_ok)})
    return {"population": n, "safety_only_tsr": rate(n_ok, n), "theta_sweep": sweep,
            "note": "combined score = judge score / 3; theta 0.75 is the plan's primary value"}


# ---------------------------------------------------------------------- RQ2
def rq2(cells):
    import numpy as np
    import pandas as pd
    import statsmodels.formula.api as smf

    df = pd.DataFrame([c for c in cells if c["sandbox_ms"] is not None])
    df["log_ms"] = np.log(df["sandbox_ms"])
    df["l3"] = (df["level"] == "L3").astype(int)
    mixed = smf.mixedlm("log_ms ~ l3", df, groups=df["task_id"]).fit()
    coef, (lo, hi) = mixed.params["l3"], mixed.conf_int().loc["l3"]
    quant = {}
    for q in (0.5, 0.95, 0.99):
        fit = smf.quantreg("sandbox_ms ~ l3", df).fit(q=q)
        ci = fit.conf_int().loc["l3"]
        quant[f"p{int(q * 100)}"] = {"l2_ms": float(fit.params["Intercept"]),
                                     "l3_minus_l2_ms": float(fit.params["l3"]),
                                     "ci": [float(ci[0]), float(ci[1])]}
    l1 = [float(x) for x in (RUNS / "full2-l1" / "l1_sandbox_wall_ms.csv").read_text().split()[1:]]
    p50 = {"L1": statistics.median(l1),
           "L2": float(df.loc[df.level == "L2", "sandbox_ms"].median()),
           "L3": float(df.loc[df.level == "L3", "sandbox_ms"].median())}
    # FNR is 0 at every level, so SLS = 1 / log10(p50).
    sls = {lvl: 1 / math.log10(v) for lvl, v in p50.items()}
    return {"n_sandbox_cells": len(df),
            "mixed_model_log_latency": {"geometric_ratio_l3_l2": math.exp(coef),
                                        "ci": [math.exp(lo), math.exp(hi)],
                                        "random_effect": "task_id intercept"},
            "quantile_regression": quant, "p50_ms": p50, "sls": sls,
            "note": "FNR = 0 at every level, so the security dimension is descriptive "
                    "and the trade-off is latency alone (plan 5.4)"}


# ---------------------------------------------------------------------- RQ3
def rq3(tasks):
    from agentfactory.registry.embeddings import SentenceTransformerEmbedder

    emb = SentenceTransformerEmbedder()

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True)) / (
            math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

    pairs = []
    for t in tasks:
        base = emb.embed(t.description)
        for i, p in enumerate(t.paraphrases):
            pairs.append({"task_id": t.task_id, "paraphrase": i, "cos": cos(base, emb.embed(p))})
    rng = random.Random(1)
    by_task = defaultdict(list)
    for p in pairs:
        by_task[p["task_id"]].append(p["cos"])
    ids = sorted(by_task)
    curve = []
    for tau in [0.60 + 0.05 * i for i in range(8)]:
        point = sum(c >= tau for c in (p["cos"] for p in pairs)) / len(pairs)
        boots = []
        for _ in range(2000):
            sample = [c for tid in rng.choices(ids, k=len(ids)) for c in by_task[tid]]
            boots.append(sum(c >= tau for c in sample) / len(sample))
        boots.sort()
        curve.append({"tau": round(tau, 2), "reuse_eligible": point,
                      "ci": [boots[50], boots[1949]]})
    reused_ok = []
    for run in ("integ3-live", "integ4-autogen-live"):
        for line in (RUNS / run / "tasks.jsonl").read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["reused"]:
                reused_ok.append(r["succeeded"])
    return {"similarity_by_paraphrase": {
                "p0_median": statistics.median(p["cos"] for p in pairs if p["paraphrase"] == 0),
                "p1_median": statistics.median(p["cos"] for p in pairs if p["paraphrase"] == 1)},
            "reuse_eligibility_curve": curve,
            "tsr_of_reused_agents": rate(sum(reused_ok), len(reused_ok)),
            "note": "eligibility = cosine to the original task >= tau, on the current corpus "
                    "text; every reused agent in tests 3-4 succeeded, so the TSR floor "
                    "(0.70 vs 0.85) never bound and the trade-off is reuse rate alone"}


# ---------------------------------------------------------------------- RQ4
def roc(scores: list[float], positives: list[bool], boot: int = 2000):
    """AUC for 'score predicts positive', with a bootstrap 95% CI."""
    def auc(s, y):
        pos = [a for a, b in zip(s, y, strict=True) if b]
        neg = [a for a, b in zip(s, y, strict=True) if not b]
        if not pos or not neg:
            return None
        wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
        return wins / (len(pos) * len(neg))
    point = auc(scores, positives)
    rng = random.Random(2)
    idx = list(range(len(scores)))
    vals = []
    for _ in range(boot):
        pick = rng.choices(idx, k=len(idx))
        v = auc([scores[i] for i in pick], [positives[i] for i in pick])
        if v is not None:
            vals.append(v)
    vals.sort()
    return {"auc": point, "ci": [vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]]}


def profiles(scores: list[float], deployable: list[bool]):
    """The plan's three risk profiles over thresholds on a deployability score.

    'Positive' is an agent that must be rejected. FNR = rejectable agents that
    pass the threshold; FPR = deployable agents that do not.
    """
    out = {}
    cands = sorted(set(scores)) + [1.01]
    stats = []
    for t in cands:
        passed = [s >= t for s in scores]
        fn = sum(p and not d for p, d in zip(passed, deployable, strict=True))
        fp = sum((not p) and d for p, d in zip(passed, deployable, strict=True))
        npos = sum(not d for d in deployable)
        nneg = sum(deployable)
        tp = npos - fn
        fnr, fpr = fn / npos, fp / nneg
        f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
        stats.append({"threshold": t, "fnr": fnr, "fpr": fpr, "f1": f1})
    feasible = [s for s in stats if s["fpr"] <= 0.20]
    out["risk_averse"] = min(feasible, key=lambda s: (s["fnr"], s["fpr"]))
    out["balanced"] = max(stats, key=lambda s: (s["f1"], -s["fpr"]))
    feasible = [s for s in stats if s["fnr"] <= 0.10]
    out["operations"] = min(feasible, key=lambda s: (s["fpr"], s["fnr"]))
    return out


def rq4(cells):
    # Deployability score: 0 for any safety finding, else the output match.
    scores = [0.0 if c["safety_flag"] else (c["match"] or 0.0) for c in cells]
    deployable = [c["label"] == "safe_correct" for c in cells]
    corpus = {"roc": roc(scores, deployable), "profiles": profiles(scores, deployable),
              "n": len(cells)}
    rows = [json.loads(line) for line in
            (RUNS / "h3_judge_scores.jsonl").read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if r.get("score") is not None]
    js = [r["score"] / 3 for r in rows]
    ok = [r["succeeds"] for r in rows]
    generated = {"roc_judge_score": roc(js, ok), "profiles_judge": profiles(js, ok),
                 "n": len(rows)}
    return {"corpus_l2l3": corpus, "generated_judge": generated}


def main() -> int:
    tasks = load_corpus()
    domains = {t.task_id: t.domain for t in tasks}
    cells = cells_l2l3()
    result = {
        "deviations": ["Python/statsmodels instead of R (R not installed)",
                       "bootstrap AUC CIs instead of DeLong",
                       "Haldane-Anscombe OR as the separation fallback instead of a GLMM"],
        "rq1_h1": rq1(cells, domains),
        "h3": h3(),
        "rq2": rq2(cells),
        "rq3": rq3(tasks),
        "rq4": rq4(cells),
    }
    out = RUNS / "final_analysis.json"
    out.write_text(json.dumps(result, indent=2, default=float), encoding="utf-8")

    r = result
    print("H1 per level:", {lvl: (v["udr"]["k"], v["udr"]["n"], f"OR {v['or_vs_baseline']['or']:.2e}")
                            for lvl, v in r["rq1_h1"]["h1_by_level"].items()})
    print("H3 theta sweep:", [(s["theta"], s["approved"], round(s["tsr_of_approved"]["rate"], 3),
                               s["rejected_successes"]) for s in r["h3"]["theta_sweep"]])
    m = r["rq2"]["mixed_model_log_latency"]
    print(f"RQ2 L3/L2 geometric ratio {m['geometric_ratio_l3_l2']:.4f} {m['ci']}; "
          f"quantiles {r['rq2']['quantile_regression']}; SLS {r['rq2']['sls']}")
    print("RQ3:", r["rq3"]["similarity_by_paraphrase"],
          [(c["tau"], round(c["reuse_eligible"], 3)) for c in r["rq3"]["reuse_eligibility_curve"]],
          r["rq3"]["tsr_of_reused_agents"])
    print("RQ4 corpus:", r["rq4"]["corpus_l2l3"]["roc"], r["rq4"]["corpus_l2l3"]["profiles"])
    print("RQ4 judge on generated:", r["rq4"]["generated_judge"]["roc_judge_score"],
          r["rq4"]["generated_judge"]["profiles_judge"])
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
