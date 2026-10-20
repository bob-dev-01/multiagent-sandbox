"""Record demo runs as replays the page can play without a backend.

    python demo/record_replays.py --sandbox aks        # real pods; every scenario
    python demo/record_replays.py --sandbox local      # no cluster; safe scenarios only

Each scenario runs through the same DemoSession the live backend uses and its
event stream is saved to demo/web/data/replays/<id>.json, with an index. With
`--sandbox local` the runner is the test suite's unisolated one, so only
scenarios that never execute model-written or unsafe code are allowed: stored
correct agents, and unsafe ones that static analysis stops before any run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from agentfactory.demo.session import DemoSession, RunRequest  # noqa: E402

OUT = ROOT / "demo" / "web" / "data" / "replays"

# id, title, request; in order, because reuse needs the earlier registration.
SCENARIOS = [
    ("live-new", "Claude writes a new agent (T001)",
     RunRequest(task_id="T001", mode="live", level="L3")),
    ("live-reuse", "A reworded task reuses it",
     RunRequest(task_id="T001", mode="live", wording="p0", level="L3")),
    ("wrong-fixed", "A wrong agent, fixed by Claude (T016)",
     RunRequest(task_id="T016", mode="wrong_i1", level="L3")),
    ("unsafe-static", "An unsafe agent stopped by static analysis (T001 u1)",
     RunRequest(task_id="T001", mode="unsafe_u1", level="L3")),
    ("unsafe-sandbox", "The same kind of agent run in the sandbox anyway (T008 u2)",
     RunRequest(task_id="T008", mode="unsafe_u2", level="L3", sandbox_always=True)),
    ("both", "Unsafe and wrong at once (T005 b1)",
     RunRequest(task_id="T005", mode="both_b1", level="L2")),
    ("stored-l2", "A stored correct agent at L2 (T011)",
     RunRequest(task_id="T011", mode="safe_a1", level="L2")),
]
LOCAL_SAFE = {"unsafe-static", "both", "stored-l2"}


def local_session() -> DemoSession:
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import LocalHarnessRunner  # the test-only runner, guarded below

    runner = LocalHarnessRunner()
    return DemoSession(runner_factory=lambda level: runner, use_judge=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sandbox", choices=["aks", "local"], required=True)
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    args = ap.parse_args()

    if args.sandbox == "local":
        session = local_session()
    else:
        from agentfactory.demo.session import runner_factory_from_env

        session = DemoSession(runner_factory=runner_factory_from_env(), daily_budget_usd=5)
    _ = session.embedder  # load the model now, not inside the first recorded run
    wanted = {s for s in args.only.split(",") if s}
    OUT.mkdir(parents=True, exist_ok=True)
    index_path = OUT / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    for sid, title, req in SCENARIOS:
        if wanted and sid not in wanted:
            continue
        if args.sandbox == "local" and sid not in LOCAL_SAFE:
            print(f"skip {sid}: needs the real sandbox")
            continue
        run = session.start(req)
        while not run.done:
            time.sleep(0.2)
        (OUT / f"{sid}.json").write_text(json.dumps(run.events, default=str), encoding="utf-8")
        outcome = next((e["data"].get("outcome") for e in run.events if e["type"] == "run_finished"), "?")
        print(f"{sid}: {len(run.events)} events, {outcome}")
        index = [e for e in index if e["id"] != sid]
        index.append({"id": sid, "title": title, "mode": req.mode, "file": f"{sid}.json",
                      "sandbox": args.sandbox})
    order = [s[0] for s in SCENARIOS]
    index.sort(key=lambda e: order.index(e["id"]) if e["id"] in order else 99)
    index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
