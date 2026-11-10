"""External baseline: bandit alone, with its default rules, on the corpus and the held-out set.

    python experiments/bandit_baseline.py --heldout runs/heldout-set

An off-the-shelf static analyser for Python security issues, run on its own,
with default settings, as a team without the pipeline would. An agent counts as
flagged when bandit reports any issue (and, separately, any issue of medium or
high severity). The code is only parsed, never run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentfactory.corpus import load_corpus  # noqa: E402
from agentfactory.metrics.core import wilson_interval  # noqa: E402

UNSAFE = {"unsafe_only", "both", "unsafe"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--heldout", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "runs" / "bandit-baseline")
    args = ap.parse_args(argv)

    items = [{"set": "corpus", "id": f"{a.task_id}_{a.variant}", "label": a.label.value,
              "code": a.source} for t in load_corpus() for a in t.agents]
    if args.heldout is not None:
        for line in (args.heldout / "agents.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if "python_source" in row:
                items.append({"set": "heldout", "id": row["id"], "label": row["kind"],
                              "classes": row["classes"], "style": row["style"],
                              "code": row["python_source"]})

    with tempfile.TemporaryDirectory() as tmp:
        for it in items:
            Path(tmp, f"{it['set']}__{it['id']}.py").write_text(it["code"], encoding="utf-8")
        proc = subprocess.run([sys.executable, "-m", "bandit", "-r", tmp, "-f", "json", "-q"],
                              capture_output=True, text=True, check=False)
        report = json.loads(proc.stdout)
    from importlib.metadata import version as installed
    version = f"bandit {installed('bandit')}"
    issues: dict[str, list] = {}
    for issue in report["results"]:
        issues.setdefault(Path(issue["filename"]).stem, []).append(
            (issue["test_id"], issue["issue_severity"]))

    rows = []
    for it in items:
        found = issues.get(f"{it['set']}__{it['id']}", [])
        rows.append({k: it.get(k) for k in ("set", "id", "label", "classes", "style")}
                    | {"issues": found, "any": bool(found),
                       "medium_plus": any(sev in ("MEDIUM", "HIGH") for _, sev in found)})

    summary = {"bandit": version, "results": {}}
    for name in sorted({r["set"] for r in rows}):
        sub = [r for r in rows if r["set"] == name]
        uns = [r for r in sub if r["label"] in UNSAFE]
        oth = [r for r in sub if r["label"] not in UNSAFE]
        res = {"n_unsafe": len(uns), "n_others": len(oth)}
        for key in ("any", "medium_plus"):
            k, f = sum(r[key] for r in uns), sum(r[key] for r in oth)
            res[key] = {"unsafe_detected": k, "detection_rate": k / len(uns),
                        "detection_ci": wilson_interval(k, len(uns)),
                        "others_flagged": f, "false_positive_rate": f / len(oth),
                        "fp_ci": wilson_interval(f, len(oth))}
        res["tests_fired"] = dict(Counter(t for r in sub for t, _ in r["issues"]).most_common())
        summary["results"][name] = res
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                                            encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
