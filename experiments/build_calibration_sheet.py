"""Build the blind human scoring sheet for judge calibration.

    python experiments/build_calibration_sheet.py --evidence runs/pilot3-watch

Writes `judge_calibration_sheet.xlsx` (what the rater fills in) and
`judge_calibration_key.csv` (item -> agent, label; not to be opened before
scoring) to docs/calibration/. Execution evidence comes from a previous run's
sandbox traces, matched by code hash; an agent that run never executed —
because Layer 1 stopped it — is shown as not executed, exactly as the judge
would see it.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentfactory.corpus import load_corpus  # noqa: E402
from agentfactory.judge.calibration import (  # noqa: E402
    build_items,
    load_traces,
    write_key,
    write_sheet,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evidence", type=Path, required=True,
                        help="Run directory whose sandbox traces supply execution evidence.")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "calibration")
    parser.add_argument("--seed", type=int, default=20261008)
    args = parser.parse_args(argv)

    log = args.evidence / f"{args.evidence.name}.jsonl"
    if not log.exists():
        print(f"error: no event log at {log}")
        return 2

    traces = load_traces(log)
    items = build_items(load_corpus(), traces, seed=args.seed,
                        evidence_source=args.evidence.name)
    args.out.mkdir(parents=True, exist_ok=True)
    sheet = args.out / "judge_calibration_sheet.xlsx"
    key = args.out / "judge_calibration_key.csv"
    if sheet.exists():
        print(f"error: {sheet} exists; it may hold scores. Move it aside first.")
        return 2
    write_sheet(items, sheet)
    write_key(items, key)

    executed = sum(it.evidence is not None for it in items)
    print(f"{len(items)} items ({executed} with execution evidence from {args.evidence.name}, "
          f"{len(items) - executed} not executed)")
    print(f"labels: {dict(Counter(it.label for it in items))}")
    print(f"sheet: {sheet}\nkey:   {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
