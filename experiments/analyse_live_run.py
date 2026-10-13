"""What the live-generator run says, recomputed from its logs.

    python experiments/analyse_live_run.py runs/integ2-live

Generated agents have no ground-truth label, so this looks at how the pipeline
treated them, from four sides:

  wording      verdicts split by the text the agent was generated from: the
               task's own description, or one of its paraphrases
  failures     each FAIL_INCORRECT, as the keys where the agent's output and
               the expected output differ
  static now   which agents today's Layer 1 flags under AF108 (undeclared
               tool use) — the rule was widened after this run
  profiles     every verdict re-judged under each strictness profile (RQ4's
               cost side); the recorded strict verdicts must come back first
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from strictness_sweep import PROFILES, reports_for  # noqa: E402

from agentfactory.contracts import AgentContract, AgentSpec  # noqa: E402
from agentfactory.corpus import load_corpus  # noqa: E402
from agentfactory.validation.aggregator import StrictnessProfile, assess  # noqa: E402
from agentfactory.validation.layer1_static import analyse  # noqa: E402


def load(run_dir: Path):
    agents = {}
    for line in (run_dir / "generated_agents.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        agents[row["agent_id"]] = row
    layers: dict[str, dict] = defaultdict(dict)
    verdicts: dict[str, dict] = {}
    for line in (run_dir / f"{run_dir.name}.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("event_type") == "layer_completed":
            layers[d["agent_id"]][d["layer"]] = d
        elif d.get("event_type") == "verdict_emitted" and d.get("verdict"):
            verdicts[d["agent_id"]] = d
    return agents, layers, verdicts


def wording(task, spec_text: str) -> str:
    if spec_text == task.description:
        return "original"
    if spec_text in task.paraphrases:
        return f"paraphrase {task.paraphrases.index(spec_text)}"
    return "unknown"


def output_diff(expected: dict, got) -> dict:
    if not isinstance(got, dict):
        return {"<output>": [expected, got]}
    return {k: [expected.get(k), got.get(k)]
            for k in sorted(set(expected) | set(got)) if expected.get(k) != got.get(k)}


def analyse_run(run_dir: Path) -> dict:
    tasks = {t.task_id: t for t in load_corpus()}
    agents, layers, verdicts = load(run_dir)

    by_wording: dict[str, Counter] = defaultdict(Counter)
    failures = []
    static_now = Counter()
    profiles = {p: Counter() for p in PROFILES}
    mismatched = 0

    for agent_id, v in verdicts.items():
        g, task = agents[agent_id], tasks[v["task_id"]]
        recorded = v["verdict"]
        source_of = wording(task, g["task_spec"])
        by_wording[source_of][recorded] += 1

        contract = AgentContract(task_spec=g["task_spec"],
                                 authorized_tools=tuple(g["authorized_tools"]))
        flagged = any(f.rule_id == "AF108"
                      for f in analyse(g["source"], contract, use_bandit=False).findings)
        static_now[f"{recorded} / AF108 {'flags' if flagged else 'silent'}"] += 1

        spec = AgentSpec.from_source(task_id=v["task_id"], source=g["source"], contract=contract)
        reports = reports_for(layers[agent_id])
        for name in PROFILES:
            verdict = assess(reports, spec, StrictnessProfile.named(name)).verdict().value
            profiles[name][verdict] += 1
            if name == "strict" and verdict != recorded:
                mismatched += 1

        if recorded == "FAIL_INCORRECT":
            trace = layers[agent_id]["sandbox"]["detail"]["trace"]
            failures.append({
                "task_id": v["task_id"],
                "wording": source_of,
                "diff": output_diff(task.expected_output, trace.get("agent_output")),
            })

    if mismatched:
        raise SystemExit(f"replay disagrees with {mismatched} of {len(verdicts)} recorded verdicts")

    # Identical failures collapse into one row with a count.
    grouped = Counter((f["task_id"], f["wording"], json.dumps(f["diff"], sort_keys=True))
                      for f in failures)
    return {
        "n_verdicts": len(verdicts),
        "by_wording": {k: dict(c) for k, c in sorted(by_wording.items())},
        "static_now": dict(static_now),
        "profiles": {p: dict(c) for p, c in profiles.items()},
        "failures": [
            {"task_id": t, "wording": w, "n": n, "diff": json.loads(d)}
            for (t, w, d), n in sorted(grouped.items())
        ],
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--out", type=Path, help="write the analysis as JSON")
    args = ap.parse_args(argv)

    result = analyse_run(args.run_dir)
    print(f"{result['n_verdicts']} verdicts; replay under strict reproduces every one\n")
    print("Verdicts by the wording the agent was generated from:")
    for k, c in result["by_wording"].items():
        print(f"  {k:13s} {c}")
    print("\nToday's AF108 against the recorded verdicts:")
    for k, n in sorted(result["static_now"].items()):
        print(f"  {k:32s} {n}")
    print("\nRe-judged under each profile:")
    for p, c in result["profiles"].items():
        print(f"  {p:10s} {c}")
    print("\nFAIL_INCORRECT, grouped (expected vs got, differing keys only):")
    for f in result["failures"]:
        print(f"  {f['task_id']} {f['wording']:12s} x{f['n']}: {json.dumps(f['diff'])[:150]}")
    if args.out:
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
