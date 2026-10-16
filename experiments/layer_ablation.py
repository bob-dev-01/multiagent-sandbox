"""Layer ablation: what does the sandbox detect on its own?

    python experiments/run_matrix.py --run-id ablation-l2l3 --levels L2,L3 \\
        --replicates 1 --labels unsafe_only,both --no-short-circuit
    python experiments/layer_ablation.py runs/ablation-l2l3

In every other run Layers 1-2 stop each unsafe corpus agent before the
sandbox, so the sandbox's own detection rate is never observed, and "Layer 3
is what actually holds" is an architectural claim rather than a measurement.
Here the unsafe agents run in the sandbox regardless, and each cell records
what each layer said on its own. Reported per isolation level and per
injected failure mode: detected by static analysis, by the policy check, by
the sandbox, and by the sandbox with no other layer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentfactory.corpus import load_corpus  # noqa: E402
from agentfactory.metrics.core import wilson_interval  # noqa: E402

SIGNALS = ("syscalls_blocked", "network_attempts", "fs_violations", "unauthorized_tool_calls")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args(argv)

    modes = {(t.task_id, a.variant): a.failure_mode for t in load_corpus() for a in t.agents}
    layers: dict[str, dict] = defaultdict(dict)
    cells = {}
    for line in (args.run_dir / f"{args.run_dir.name}.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("event_type") == "layer_completed":
            layers[d["agent_id"]][d["layer"]] = d
        elif d.get("event_type") == "verdict_emitted" and "cell_key" in (d.get("detail") or {}):
            cells[d["agent_id"]] = d

    rows = []
    for agent_id, v in cells.items():
        task_id, variant, level, _ = v["detail"]["cell_key"].split(":")
        ev = layers[agent_id]
        if "sandbox" not in ev:
            continue  # sandbox did not run (infrastructure failure); not a result
        trace = ev["sandbox"]["detail"]["trace"] or {}
        rows.append({
            "level": level, "label": v["detail"]["ground_truth"],
            "mode": modes[(task_id, variant)],
            "static": not ev["static"]["passed"], "policy": not ev["policy"]["passed"],
            "sandbox": not ev["sandbox"]["passed"],
            "signals": [s for s in SIGNALS if trace.get(s)],
            "verdict": v["verdict"],
        })

    def rate(sub, key):
        k = sum(r[key] for r in sub)
        return {"k": k, "n": len(sub), "rate": k / len(sub) if sub else None,
                "ci": wilson_interval(k, len(sub))}

    result = {"n_cells": len(rows), "by_level": {}, "by_mode": {}}
    for level in sorted({r["level"] for r in rows}):
        sub = [r for r in rows if r["level"] == level]
        result["by_level"][level] = {
            "static": rate(sub, "static"), "policy": rate(sub, "policy"),
            "sandbox": rate(sub, "sandbox"),
            "sandbox_only": sum(r["sandbox"] and not (r["static"] or r["policy"]) for r in sub),
            "missed_by_all": sum(not (r["static"] or r["policy"] or r["sandbox"]) for r in sub),
            "signals": dict(Counter(s for r in sub for s in r["signals"])),
            "verdicts": dict(Counter(r["verdict"] for r in sub)),
        }
    for mode in sorted({r["mode"] for r in rows}):
        sub = [r for r in rows if r["mode"] == mode]
        result["by_mode"][mode] = {
            lvl: {"n": len(s), "sandbox": sum(r["sandbox"] for r in s),
                  "static": sum(r["static"] for r in s)}
            for lvl in sorted({r["level"] for r in sub})
            for s in [[r for r in sub if r["level"] == lvl]]
        }
    out = args.run_dir / "layer_ablation.json"
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
