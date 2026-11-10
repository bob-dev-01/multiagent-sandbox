"""Compare the pipeline with the two external baselines on the corpus and the held-out set.

    python experiments/heldout_report.py --validation runs/heldout-l3 \\
        --corpus-ablation runs/ablation4-l2l3

Reads the pipeline's held-out results (heldout_validate.py), the baselines
(bandit_baseline.py, llm_safeguard_baseline.py) and, for the corpus, the
full-corpus matrix and a layer ablation. For each validator: unsafe agents
detected and other agents flagged unsafe, with Wilson intervals; for the
pipeline also what each layer detected on its own, and every held-out unsafe
agent it let through, with its risk class and style.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentfactory.metrics.core import wilson_interval  # noqa: E402

UNSAFE_VERDICTS = {"FAIL_UNSAFE", "FAIL_BOTH"}


def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None,
            "wilson": [round(x, 4) for x in wilson_interval(k, n)] if n else None}


def jl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def corpus_layers(run_dir: Path) -> dict:
    layers: dict[str, dict] = defaultdict(dict)
    cells = {}
    for d in jl(run_dir / f"{run_dir.name}.jsonl"):
        if d.get("event_type") == "layer_completed":
            layers[d["agent_id"]][d["layer"]] = d
        elif d.get("event_type") == "verdict_emitted" and "cell_key" in (d.get("detail") or {}):
            cells[d["agent_id"]] = d
    out = {}
    for level in ("L2", "L3"):
        ids = [a for a, v in cells.items() if v["detail"]["cell_key"].split(":")[2] == level
               and "sandbox" in layers[a]]
        out[level] = {name: rate(sum(not layers[a][name]["passed"] for a in ids), len(ids))
                      for name in ("static", "policy", "sandbox")}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--validation", type=Path, required=True)
    ap.add_argument("--corpus-matrix", type=Path, default=REPO_ROOT / "runs" / "full2-l2l3")
    ap.add_argument("--corpus-ablation", type=Path, default=REPO_ROOT / "runs" / "ablation4-l2l3")
    ap.add_argument("--bandit", type=Path, default=REPO_ROOT / "runs" / "bandit-baseline")
    ap.add_argument("--safeguard", type=Path, default=REPO_ROOT / "runs" / "llm-safeguard")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    rows = [r for r in jl(args.validation / "results.jsonl") if "error" not in r]
    infra = [r for r in jl(args.validation / "results.jsonl") if "error" in r]
    uns = [r for r in rows if r["kind"] == "unsafe"]
    safe = [r for r in rows if r["kind"] == "safe"]

    def flagged_by(r, layer):
        if layer == "sandbox":
            return bool(r["sandbox_flagged"] or r.get("resource_overruns") or r.get("timed_out"))
        return bool(r[f"{layer}_flagged"])

    pipeline = {
        "verdict_unsafe": rate(sum(r["verdict"] in UNSAFE_VERDICTS for r in uns), len(uns)),
        "safe_flagged_unsafe": rate(sum(r["verdict"] in UNSAFE_VERDICTS for r in safe), len(safe)),
        "safe_verdicts": dict(Counter(r["verdict"] for r in safe)),
        "by_layer": {layer: rate(sum(flagged_by(r, layer) for r in uns), len(uns))
                     for layer in ("static", "policy", "sandbox")},
        "sandbox_and_no_other_layer": sum(flagged_by(r, "sandbox") and not flagged_by(r, "static")
                                          and not flagged_by(r, "policy") for r in uns),
        "by_style": {s: rate(sum(r["verdict"] in UNSAFE_VERDICTS for r in uns if r["style"] == s),
                             sum(1 for r in uns if r["style"] == s)) for s in ("plain", "inconspicuous")},
        "by_class": {c: rate(sum(r["verdict"] in UNSAFE_VERDICTS for r in uns if c in r["classes"]),
                             sum(1 for r in uns if c in r["classes"]))
                     for c in sorted({c for r in uns for c in r["classes"]})},
        "missed": [{k: r[k] for k in ("id", "classes", "style", "verdict")} for r in uns
                   if r["verdict"] not in UNSAFE_VERDICTS],
        "safe_flagged": [{k: r[k] for k in ("id", "verdict", "static_findings", "policy_findings", "signals")}
                         for r in safe if r["verdict"] in UNSAFE_VERDICTS],
        "infrastructure_failures": len(infra),
        "kernels": dict(Counter(r["kernel_release"] for r in rows)),
    }

    baselines = {}
    for name, path, key in (("bandit_any", args.bandit, "any"), ("bandit_medium_plus", args.bandit, "medium_plus"),
                            ("llm_safeguard", args.safeguard, "danger")):
        res = {}
        for s in ("corpus", "heldout"):
            sub = [r for r in jl(path / "results.jsonl") if r["set"] == s]
            u = [r for r in sub if r["label"] in ("unsafe_only", "both", "unsafe")]
            o = [r for r in sub if r not in u]
            res[s] = {"unsafe_detected": rate(sum(bool(r[key]) for r in u), len(u)),
                      "others_flagged": rate(sum(bool(r[key]) for r in o), len(o))}
            if s == "heldout":
                res[s]["by_style"] = {st: rate(sum(bool(r[key]) for r in u if r["style"] == st),
                                               sum(1 for r in u if r["style"] == st))
                                      for st in ("plain", "inconspicuous")}
        baselines[name] = res

    matrix_cells = [r for r in jl(args.corpus_matrix / f"{args.corpus_matrix.name}.jsonl")
                    if r.get("event_type") == "verdict_emitted" and (r.get("detail") or {}).get("cell_key")]
    per_agent = defaultdict(set)
    labels = {}
    for r in matrix_cells:
        task, variant = r["detail"]["cell_key"].split(":")[:2]
        per_agent[(task, variant)].add(r["verdict"])
        labels[(task, variant)] = r["detail"]["ground_truth"]
    cu = [a for a in per_agent if labels[a] in ("unsafe_only", "both")]
    co = [a for a in per_agent if a not in cu]
    corpus = {
        "pipeline_unsafe_detected": rate(sum(per_agent[a] <= UNSAFE_VERDICTS for a in cu), len(cu)),
        "pipeline_others_flagged": rate(sum(bool(per_agent[a] & UNSAFE_VERDICTS) for a in co), len(co)),
        "layers_ablation": corpus_layers(args.corpus_ablation),
    }

    report = {"heldout": {"n_unsafe": len(uns), "n_safe": len(safe), "pipeline": pipeline},
              "corpus": corpus, "baselines": baselines}
    out = args.out or (args.validation / "report.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
