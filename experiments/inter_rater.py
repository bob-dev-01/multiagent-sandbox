"""Second-rater agreement for the judge calibration, without calling the judge again.

    python experiments/inter_rater.py docs/calibration/judge_calibration_sheet_rater2.scored-2026-11-20.xlsx

Compares a fully scored second-rater sheet (built with
`build_calibration_sheet.py --second-rater N`) against the first rater's
scores and the judge's scores already recorded in
docs/calibration/judge_calibration_results.csv. With perfect agreement a
bootstrap interval for kappa is degenerate, so the exact-agreement proportion
is reported with its Clopper-Pearson 95% interval as well.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from scipy.stats import beta  # noqa: E402

from agentfactory.judge.calibration import (  # noqa: E402
    quadratic_weighted_kappa,
    read_key,
    read_scores,
)
from agentfactory.judge.correctness_judge import cohens_kappa  # noqa: E402


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    lo = 0.0 if k == 0 else float(beta.ppf(alpha / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1 - alpha / 2, k + 1, n - k))
    return lo, hi


def agreement(a: list[int], b: list[int]) -> dict:
    same = sum(x == y for x, y in zip(a, b, strict=True))
    lo, hi = clopper_pearson(same, len(a))
    return {
        "identical": same, "n": len(a),
        "exact_agreement_ci95": [round(lo, 3), round(hi, 3)],
        "cohens_kappa": round(cohens_kappa(a, b), 3),
        "quadratic_weighted_kappa": round(quadratic_weighted_kappa(a, b), 3),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sheet", type=Path)
    ap.add_argument("--dir", type=Path, default=REPO_ROOT / "docs" / "calibration")
    args = ap.parse_args(argv)

    second = read_scores(args.sheet)  # refuses a partly scored sheet
    with (args.dir / "judge_calibration_results.csv").open(encoding="utf-8", newline="") as f:
        first = {r["item_id"]: r for r in csv.DictReader(f)}
    key = read_key(args.dir / "judge_calibration_key.csv")
    ids = sorted(second)
    if not set(ids) <= set(first):
        print("error: the second sheet lists items the calibration did not score")
        return 2

    r1 = [int(first[i]["human"]) for i in ids]
    judge = [int(first[i]["judge"]) for i in ids]
    r2 = [second[i] for i in ids]
    report = {
        "sheet": args.sheet.name,
        "items": ids,
        "labels": {lab: sum(key[i]["label"] == lab for i in ids)
                   for lab in sorted({key[i]["label"] for i in ids})},
        "rater1_vs_rater2": agreement(r1, r2),
        "judge_vs_rater2": agreement(judge, r2),
        "rater1_vs_judge_on_subset": agreement(r1, judge),
        "subset_contains_rater1_judge_disagreements":
            [i for i in first if first[i]["human"] != first[i]["judge"] and i in second],
        "score_counts_rater2": {s: r2.count(s) for s in range(4)},
    }
    (args.dir / "inter_rater_report.json").write_text(json.dumps(report, indent=2),
                                                      encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
