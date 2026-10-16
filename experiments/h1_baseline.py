"""H1: does the three-layer pipeline reduce the Unsafe Deployment Rate compared
with no validation?

    python experiments/h1_baseline.py

The no-validation baseline deploys every agent it is given, so its UDR is 1 on
any set of unsafe agents by construction; the comparison is therefore paired
and one-sided by design, and the exact McNemar test on the unsafe cells is
what the protocol asks for rather than a discovery. What the analysis adds is
where the pipeline stops each unsafe agent, which layer, and how the same
holds for agents a real model wrote.

Sources:
  runs/full2-l2l3   L2 and L3 cells, full event log
  runs/full2-l1     L1 cells, summary only (the raw log stays in Blob storage)
  runs/integ2-live, integ3-live, integ4-autogen-live
                    live-generated agents; no ground truth, so "unsafe" here
                    means a safety verdict, not a label
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from strictness_sweep import mcnemar_exact  # noqa: E402

from agentfactory.metrics.core import wilson_interval  # noqa: E402

UNSAFE = {"unsafe_only", "both"}
LIVE = ("integ2-live", "integ3-live", "integ4-autogen-live")


def corpus_cells():
    """(level, label, verdict, stopped_at) for the L2/L3 cells, from the event log."""
    log = REPO_ROOT / "runs" / "full2-l2l3" / "full2-l2l3.jsonl"
    layers: dict[str, dict] = defaultdict(dict)
    verdicts = {}
    for line in log.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("event_type") == "layer_completed":
            layers[d["agent_id"]][d["layer"]] = d
        elif d.get("event_type") == "verdict_emitted" and "cell_key" in (d.get("detail") or {}):
            verdicts[d["agent_id"]] = d
    for agent_id, v in verdicts.items():
        ev = layers[agent_id]
        if not ev["static"]["passed"]:
            stopped = "static"
        elif not ev["policy"]["passed"]:
            stopped = "policy"
        elif "sandbox" in ev and not ev["sandbox"]["passed"]:
            stopped = "sandbox"
        else:
            stopped = None
        level = v["detail"]["cell_key"].split(":")[2]
        yield level, v["detail"]["ground_truth"], v["verdict"], stopped


def main() -> int:
    cells = list(corpus_cells())
    unsafe = [c for c in cells if c[1] in UNSAFE]
    deployed = sum(c[2] == "PASS" for c in unsafe)
    l1 = json.loads((REPO_ROOT / "runs" / "full2-l1" / "summary.json").read_text())["label_x_verdict"]
    l1_unsafe = l1["unsafe_only->FAIL_UNSAFE"] + l1["both->FAIL_UNSAFE"]
    l1_deployed = sum(n for k, n in l1.items() if k.split("->")[0] in UNSAFE and k.endswith("PASS"))

    n = len(unsafe) + l1_unsafe
    k = deployed + l1_deployed
    result = {
        "corpus": {
            "unsafe_cells": n,
            "pipeline_deployed": k,
            "pipeline_udr": k / n,
            "pipeline_udr_ci": wilson_interval(k, n),
            "baseline_deployed": n,
            "baseline_udr": 1.0,
            # Discordant pairs: only the pipeline right (baseline deploys, pipeline stops).
            "mcnemar_p": mcnemar_exact(n - k, 0),
            "stopped_at_l2_l3": dict(Counter(c[3] for c in unsafe)),
            "stopped_by_label_l2_l3": {f"{lbl}->{at}": m for (lbl, at), m in
                                       sorted(Counter((c[1], c[3]) for c in unsafe).items())},
        },
        "live": {},
    }
    for run in LIVE:
        log = REPO_ROOT / "runs" / run / f"{run}.jsonl"
        layers: dict[str, dict] = defaultdict(dict)
        flagged, executed_unsafe = [], 0
        for line in log.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            if d.get("event_type") == "layer_completed":
                layers[d["agent_id"]][d["layer"]] = d
            elif d.get("event_type") == "verdict_emitted" and d.get("verdict") in (
                "FAIL_UNSAFE", "FAIL_BOTH"
            ):
                flagged.append(d["agent_id"])
        summary = json.loads((REPO_ROOT / "runs" / run / "summary.json").read_text())
        executed_unsafe = summary["executions_unsafe"]
        at = Counter(
            "static" if not layers[a]["static"]["passed"]
            else "policy" if not layers[a]["policy"]["passed"] else "sandbox"
            for a in flagged
        )
        result["live"][run] = {"flagged_unsafe": len(flagged), "stopped_at": dict(at),
                               "executed_unsafely": executed_unsafe}
    out = REPO_ROOT / "runs" / "h1_baseline.json"
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
