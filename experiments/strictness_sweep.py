"""RQ4: how the strictness profile moves the error rates — recomputed, not re-run.

    python experiments/strictness_sweep.py runs/full2-l2l3            # one or more run dirs
    python experiments/strictness_sweep.py --combine a.json b.json     # pool saved results

A verdict is a function of the evidence (static findings, policy report,
sandbox trace) and the strictness profile. The evidence is in the event log, so
every cell can be re-judged under every profile with the pipeline's own
aggregator, without running anything again. Short-circuiting is unaffected:
Layer 1 passes or fails on HIGH findings alone and Layer 2 on the contract, so
which agents reached the sandbox does not depend on the profile.

Before trusting the replay, the script re-judges every cell under the profile
the run was made with and requires the recorded verdict back, cell for cell.

Outcome measures, per profile:
  unsafe pass rate     unsafe agents (unsafe_only, both) not flagged unsafe — FNR
  unsafe flag rate     safe agents (safe_correct, incorrect_only) flagged unsafe
  incorrect pass rate  incorrect_only agents that PASS
  false rejection      safe_correct agents that do not PASS — what strictness costs
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentfactory.contracts import (  # noqa: E402
    ExecutionTrace,
    Finding,
    LayerReport,
    Severity,
    ValidationLayer,
    Verdict,
)
from agentfactory.corpus import load_corpus  # noqa: E402
from agentfactory.metrics.core import wilson_interval  # noqa: E402
from agentfactory.validation.aggregator import StrictnessProfile, assess  # noqa: E402

PROFILES = ("permissive", "balanced", "strict", "paranoid")
FINDING = re.compile(r"^\[(HIGH|MEDIUM|LOW)\] (\S+?)(?::(\d+))? (.*)$", re.S)
UNSAFE = {"unsafe_only", "both"}


def parse_findings(strings, layer):
    out = []
    for s in strings:
        m = FINDING.match(s)
        if not m:
            raise ValueError(f"unparseable finding: {s!r}")
        sev, rule, line, msg = m.groups()
        out.append(Finding(layer=layer, rule_id=rule, severity=Severity(sev), message=msg,
                           line_number=int(line) if line else None))
    return tuple(out)


def load_cells(run_dir: Path):
    """One entry per validated cell: label, level, recorded verdict, and its evidence."""
    log = run_dir / f"{run_dir.name}.jsonl"
    layers: dict[str, dict] = defaultdict(dict)
    verdicts: dict[str, dict] = {}
    profile = None
    for line in log.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        det = d.get("detail") or {}
        if d.get("event_type") == "layer_completed":
            layers[d["agent_id"]][d["layer"]] = d
        elif d.get("event_type") == "verdict_emitted" and "cell_key" in det:
            verdicts[d["agent_id"]] = d
            profile = profile or det.get("strictness_profile")
    return layers, verdicts, profile


def reports_for(layer_events):
    reports = []
    st = layer_events["static"]
    reports.append(LayerReport(layer=ValidationLayer.STATIC, passed=st["passed"],
                               score=st.get("score"),
                               findings=parse_findings(st["detail"]["findings"],
                                                       ValidationLayer.STATIC)))
    po = layer_events["policy"]
    reports.append(LayerReport(layer=ValidationLayer.POLICY, passed=po["passed"],
                               score=po.get("score"),
                               findings=parse_findings(po["detail"]["findings"],
                                                       ValidationLayer.POLICY)))
    sb = layer_events.get("sandbox")
    if sb is not None:
        trace = ExecutionTrace.model_validate(sb["detail"]["trace"])
        reports.append(LayerReport(layer=ValidationLayer.SANDBOX, passed=sb["passed"],
                                   score=sb.get("score"), trace=trace))
    return tuple(reports)


def sweep(run_dirs):
    corpus = {t.task_id: t for t in load_corpus()}
    counts = {p: Counter() for p in PROFILES}          # (level, label, verdict) -> n
    checked = mismatched = 0
    for run_dir in run_dirs:
        layers, verdicts, run_profile = load_cells(run_dir)
        for agent_id, v in verdicts.items():
            det = v["detail"]
            task_id, variant, level, _ = det["cell_key"].split(":")
            task = corpus[task_id]
            agent = next(a for a in task.agents if a.variant == variant)
            spec = agent.to_spec(task.contract())
            reports = reports_for(layers[agent_id])
            for name in PROFILES:
                verdict = assess(reports, spec, StrictnessProfile.named(name)).verdict()
                counts[name][(level, det["ground_truth"], verdict.value)] += 1
                if name == run_profile:
                    checked += 1
                    if verdict.value != v["verdict"]:
                        mismatched += 1
    if mismatched:
        raise SystemExit(f"replay disagrees with {mismatched} of {checked} recorded verdicts")
    return counts, checked


def rates(counter: Counter, level=None):
    def n(pred):
        return sum(c for (lv, lab, ver), c in counter.items()
                   if (level is None or lv == level) and pred(lab, ver))

    flagged_unsafe = {Verdict.FAIL_UNSAFE.value, Verdict.FAIL_BOTH.value}
    out = {}
    for key, num, den in (
        ("unsafe_pass_rate", lambda lab, ver: lab in UNSAFE and ver not in flagged_unsafe,
         lambda lab, ver: lab in UNSAFE),
        ("unsafe_flag_rate", lambda lab, ver: lab not in UNSAFE and ver in flagged_unsafe,
         lambda lab, ver: lab not in UNSAFE),
        ("incorrect_pass_rate", lambda lab, ver: lab == "incorrect_only" and ver == "PASS",
         lambda lab, ver: lab == "incorrect_only"),
        ("false_rejection_rate", lambda lab, ver: lab == "safe_correct" and ver != "PASS",
         lambda lab, ver: lab == "safe_correct"),
    ):
        k, total = n(num), n(den)
        lo, hi = wilson_interval(k, total)
        out[key] = {"k": k, "n": total, "rate": k / total if total else math.nan,
                    "ci": [round(lo, 4), round(hi, 4)]}
    return out


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for b vs c discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def report(counts, levels):
    table = {name: {"pooled": rates(counts[name]),
                    **{lv: rates(counts[name], lv) for lv in levels}}
             for name in PROFILES}
    print(f"{'profile':11s} {'unsafe pass':>16s} {'unsafe flag':>16s} "
          f"{'incorrect pass':>18s} {'false reject':>16s}")
    for name in PROFILES:
        row = table[name]["pooled"]
        cell = lambda m: f"{m['k']:>4d}/{m['n']:<5d} {m['rate']:.3f}"  # noqa: E731
        print(f"{name:11s} {cell(row['unsafe_pass_rate']):>16s} {cell(row['unsafe_flag_rate']):>16s} "
              f"{cell(row['incorrect_pass_rate']):>18s} {cell(row['false_rejection_rate']):>16s}")
    return table


def paired_tests(counts):
    """Each incorrect agent judged under two adjacent profiles: discordant pairs only move
    one way, because profiles are ordered, so an exact McNemar test is the right one."""
    tests = {}
    order = list(PROFILES)
    for a, b in zip(order, order[1:], strict=False):
        for key, lab, bad in (("incorrect_pass", "incorrect_only", "PASS"),):
            pa = sum(c for (lv, l_, v), c in counts[a].items() if l_ == lab and v == bad)
            pb = sum(c for (lv, l_, v), c in counts[b].items() if l_ == lab and v == bad)
            # Ordered profiles: everything that passes under the stricter one also passes
            # under the looser, so discordant pairs = the difference in counts.
            tests[f"{key}: {a} vs {b}"] = {"discordant": pa - pb, "p": mcnemar_exact(pa - pb, 0)}
    return tests


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="*", type=Path)
    ap.add_argument("--combine", nargs="*", type=Path, default=None,
                    help="Pool counts from saved strictness_sweep.json files.")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if args.combine:
        counts = {p: Counter() for p in PROFILES}
        checked = 0
        for f in args.combine:
            saved = json.loads(f.read_text(encoding="utf-8"))
            checked += saved["replay_checked"]
            for p in PROFILES:
                for k, c in saved["counts"][p].items():
                    counts[p][tuple(k.split("|"))] += c
    else:
        counts, checked = sweep(args.runs)
    levels = sorted({lv for p in PROFILES for (lv, _, _) in counts[p]})
    print(f"replay check: {checked} recorded verdicts reproduced exactly; levels {levels}")
    table = report(counts, levels)
    tests = paired_tests(counts)
    for k, v in tests.items():
        print(f"  {k}: {v['discordant']} agents change verdict, exact McNemar p = {v['p']:.3g}")

    result = {
        "replay_checked": checked,
        "levels": levels,
        "counts": {p: {"|".join(k): c for k, c in sorted(counts[p].items())} for p in PROFILES},
        "rates": table,
        "tests": tests,
    }
    out = args.out or ((args.runs[0] / "strictness_sweep.json") if args.runs else None)
    if out:
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
