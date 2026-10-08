"""Run the integration test: Orchestrator -> Generator -> Pipeline -> Registry -> Executor.

Every agent is validated *and* executed in a real sandbox, so this needs the
same infrastructure as the matrix run (the AKS pool for L2/L3, Linux for L1).
See `agentfactory.integration` for what is measured and how the warm pass and
the replay generator differ from architecture.md section 14.

    python experiments/run_integration.py --level L2 --high-risk-level L3 --seeds 5

The embedder decides the warm pass. The real one is all-MiniLM-L6-v2
(`pip install -e .[embeddings]`); the hashing stand-in matches words, not
meaning, so a run with it is marked lexical-only and its reuse rate is not a
result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from run_matrix import build_runner  # noqa: E402

from agentfactory.contracts import IsolationLevel  # noqa: E402
from agentfactory.corpus import load_corpus, manifest  # noqa: E402
from agentfactory.executor import TaskExecutor  # noqa: E402
from agentfactory.integration import (  # noqa: E402
    COLD,
    WARM,
    CorpusReplayGenerator,
    TaskRecord,
    run_integration,
)
from agentfactory.orchestrator.factory import AgentFactory  # noqa: E402
from agentfactory.registry.embeddings import (  # noqa: E402
    HashingEmbedder,
    SentenceTransformerEmbedder,
)
from agentfactory.registry.memory import InMemoryRegistry  # noqa: E402
from agentfactory.telemetry.events import EventLog, EventType  # noqa: E402
from agentfactory.validation.aggregator import StrictnessProfile  # noqa: E402
from agentfactory.validation.layer2_policy import EnterprisePolicy  # noqa: E402
from agentfactory.validation.pipeline import PipelineConfig, ValidationPipeline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Agent Factory integration test.")
    parser.add_argument("--run-id", default=f"integ-{datetime.now(UTC):%Y%m%d-%H%M%S}")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--level", default="L2", choices=["L1", "L2", "L3"],
                        help="Isolation level for validation and for standard-risk execution.")
    parser.add_argument("--high-risk-level", default="L3", choices=["L3", "none"],
                        help="Runner for high-risk tasks. 'none' makes every such task refused.")
    parser.add_argument("--seeds", type=int, default=5, help="Independent replicates.")
    parser.add_argument("--seed-base", type=int, default=20261008)
    parser.add_argument("--profile", default="strict",
                        choices=["permissive", "balanced", "strict", "paranoid"])
    parser.add_argument("--no-bandit", action="store_true")
    parser.add_argument("--embedder", default="minilm", choices=["minilm", "hashing"])
    parser.add_argument("--require-seccomp", action="store_true")
    parser.add_argument("--l2-backend", choices=["pod", "aci", "docker"], default="pod")
    parser.add_argument("--aci-resource-group", default=None)
    parser.add_argument("--aci-subnet-id", default=None)
    parser.add_argument("--location", default="centralindia")
    parser.add_argument("--k8s-namespace", default="agentfactory-sandbox")
    parser.add_argument("--kube-context",
                        default=os.environ.get("AGENTFACTORY_KUBE_CONTEXT", "agentfac-aks"))
    args = parser.parse_args(argv)

    tasks = load_corpus()
    out_dir = args.out / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.embedder == "minilm":
        embedder = SentenceTransformerEmbedder()
        try:
            embedder.embed("warm-up")
        except RuntimeError as exc:
            print(f"error: {exc}\n  or pass --embedder hashing for a lexical-only run")
            return 2
    else:
        embedder = HashingEmbedder()
        print("[warning] hashing embedder: reuse is matched on words, not meaning. "
              "This run's ARR is not a result.")

    # Built once and shared across seeds: a runner holds no per-run state, and
    # preflight should fail before the first seed rather than halfway through.
    runner = build_runner(IsolationLevel(args.level), args)
    high_risk = (
        build_runner(IsolationLevel.L3, args) if args.high_risk_level == "L3" else None
    )
    profile = StrictnessProfile.named(args.profile)
    policy = EnterprisePolicy.load()

    seeds = [args.seed_base + i for i in range(args.seeds)]
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": args.run_id,
                "started_at": datetime.now(UTC).isoformat(),
                "config": {k: str(v) for k, v in vars(args).items()},
                "seeds": seeds,
                "generator": "corpus-replay",
                "embedder": embedder.model_name,
                "embedder_lexical_only": args.embedder == "hashing",
                "corpus": manifest(tasks),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    with EventLog(out_dir, run_id=args.run_id) as log, (
        out_dir / "tasks.jsonl"
    ).open("a", encoding="utf-8") as task_log:

        def build(generator: CorpusReplayGenerator) -> AgentFactory:
            registry = InMemoryRegistry()
            pipeline = ValidationPipeline(
                runner,
                PipelineConfig.default(
                    profile=profile, policy=policy,
                    use_bandit=not args.no_bandit, test_inputs=({"case": 1},),
                ),
                event_log=log,
            )
            return AgentFactory(
                generator=generator,
                pipeline=pipeline,
                registry=registry,
                embedder=embedder,
                executor=TaskExecutor(
                    runner, high_risk_runner=high_risk, registry=registry,
                    event_log=log, profile=profile,
                ),
                event_log=log,
            )

        def on_record(row: TaskRecord) -> None:
            task_log.write(json.dumps(asdict(row)) + "\n")
            task_log.flush()
            label = "reused" if row.reused else f"{row.attempts} attempt(s)"
            print(f"  seed {row.seed} {row.phase:4s} {row.task_id}"
                  f"{'' if row.paraphrase is None else f'~p{row.paraphrase}'}: "
                  f"{row.outcome} ({label})", flush=True)

        log.emit(EventType.RUN_STARTED, detail={"kind": "integration", "seeds": seeds})
        result = run_integration(tasks, build, seeds=seeds, on_record=on_record)
        log.emit(EventType.RUN_COMPLETED, detail={"n_records": len(result.records)})

    summary = result.as_dict()
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                          encoding="utf-8")

    print("\n--- integration summary ---")
    for name in (COLD, WARM):
        p = result.phase(name)
        print(f"  {name}: n={p.n_tasks} TSR={p.task_success_rate:.3f} "
              f"[{p.tsr_ci[0]:.3f}, {p.tsr_ci[1]:.3f}]  ARR={p.agent_reuse_rate:.3f} "
              f"[{p.arr_ci[0]:.3f}, {p.arr_ci[1]:.3f}]  escalated={p.escalation_rate:.3f}  "
              f"MTTD mean={p.mean_time_to_deploy_sec:.1f}s  {p.outcomes}")
    s = result.safety()
    print(f"  FNR={s.false_negative_rate:.3f}  UDR={s.unsafe_deployment_rate:.3f} "
          f"[{s.udr_ci[0]:.3f}, {s.udr_ci[1]:.3f}]  CER={s.catastrophic_error_rate:.3f} "
          f"[{s.cer_ci[0]:.3f}, {s.cer_ci[1]:.3f}]  FPR={s.false_positive_rate:.3f}  "
          f"(n unsafe={s.n_unsafe}, safe={s.n_safe})")
    if args.embedder == "hashing":
        print("  [lexical-only embedder: ARR above is not a result]")
    print(f"\nlogs: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
