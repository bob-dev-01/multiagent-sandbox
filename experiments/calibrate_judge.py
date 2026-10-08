"""Score the calibration items with the LLM judge and compare against the human.

    python experiments/calibrate_judge.py --evidence runs/pilot3-watch
    python experiments/calibrate_judge.py --evidence runs/pilot3-watch \
        --second-sheet docs/calibration/judge_calibration_sheet_rater2.xlsx

Needs ANTHROPIC_API_KEY and a fully scored sheet. The judge sees each item
exactly as the human did (`judge.evidence`), with the gate bypassed — this is
the one call the bypass exists for. The result decides ADR-9: kappa >= 0.80 and
the judge may score unattended; below it, correctness is reported from human
scores only.

`--second-sheet` adds the human-human agreement on the items both raters
scored (the protocol's two-rater check on 25%).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentfactory.corpus import load_corpus  # noqa: E402
from agentfactory.judge.calibration import (  # noqa: E402
    load_traces,
    quadratic_weighted_kappa,
    read_key,
    read_scores,
)
from agentfactory.judge.correctness_judge import (  # noqa: E402
    KAPPA_THRESHOLD,
    CorrectnessJudge,
    cohens_kappa,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--dir", type=Path, default=REPO_ROOT / "docs" / "calibration")
    parser.add_argument("--second-sheet", type=Path, default=None)
    args = parser.parse_args(argv)

    sheet = args.dir / "judge_calibration_sheet.xlsx"
    human = read_scores(sheet)
    key = read_key(args.dir / "judge_calibration_key.csv")
    if set(human) != set(key):
        print("error: the sheet and the key list different items")
        return 2

    traces = load_traces(args.evidence / f"{args.evidence.name}.jsonl")
    agents = {a.agent_key: (task, a) for task in load_corpus() for a in task.agents}

    judge = CorrectnessJudge()
    rows = []
    for item_id in sorted(human):
        entry = key[item_id]
        task, agent = agents[entry["agent_key"]]
        if agent.source_sha256 != entry["code_sha256"]:
            print(f"error: {item_id} — corpus agent changed since the sheet was built")
            return 2
        result = judge.score(
            agent.to_spec(task.contract()),
            traces.get(agent.source_sha256),
            task.expected_output,
            task_rubric=task.correctness_rubric,
            bypass_gate=True,
        )
        rows.append({
            "item_id": item_id, "human": human[item_id], "judge": result.score,
            "label": entry["label"], "justification": result.justification,
            "cost_usd": round(result.cost_usd, 5),
        })
        print(f"  {item_id}: human {human[item_id]}  judge {result.score}")

    h = [r["human"] for r in rows]
    j = [r["judge"] for r in rows]
    kappa = judge.calibrate(h, j)
    report = {
        "n": len(rows),
        "cohens_kappa": kappa,
        "quadratic_weighted_kappa": quadratic_weighted_kappa(h, j),
        "exact_agreement": sum(a == b for a, b in zip(h, j, strict=True)) / len(rows),
        "threshold": KAPPA_THRESHOLD,
        "gate_open": judge.gate.is_open,
        "judge_model": judge.model,
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
    }

    if args.second_sheet:
        second = read_scores(args.second_sheet, allow_partial=True)
        common = sorted(set(second) & set(human))
        if common:
            a = [human[i] for i in common]
            b = [second[i] for i in common]
            report["inter_rater"] = {
                "n": len(common),
                "cohens_kappa": cohens_kappa(a, b),
                "quadratic_weighted_kappa": quadratic_weighted_kappa(a, b),
            }

    with (args.dir / "judge_calibration_results.csv").open("w", encoding="utf-8",
                                                           newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.dir / "judge_calibration_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    print("\nGATE OPEN — the judge may score unattended." if judge.gate.is_open else
          "\nGATE CLOSED — report correctness from human scores only (ADR-9).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
