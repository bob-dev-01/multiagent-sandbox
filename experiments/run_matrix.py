"""Experiment harness — runs the validation matrix and writes the event log.

Design of the run matrix, and one deliberate departure from Task 7.

Task 7 specifies three replicates per cell "to account for LLM generation
stochasticity". But the corpus is fixed and hash-pinned before the experiment
begins, so no generation happens during the main run and there is no generation
stochasticity to absorb. Worse, Layers 1 and 2 are deterministic — running the
same pinned agent through static analysis three times produces three identical
reports — so treating those replicates as independent observations is
pseudo-replication that understates standard errors (OQ-6 in architecture.md).

So replicates here apply to the sandbox layer only, where genuine runtime
variance exists. Layer 1 and Layer 2 results are computed once per agent and
reused across replicates, and the event log records `replicate` so the analysis
can keep the two populations apart rather than pooling them.

Resume is not a convenience. A batch of this size will be interrupted — by a
credit alert, a node restart, a dropped connection — and losing six hours of
runs to an unrelated failure is the difference between finishing and not.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentfactory.contracts import IsolationLevel, ValidationLayer  # noqa: E402
from agentfactory.corpus import CorpusTask, load_corpus, manifest  # noqa: E402
from agentfactory.metrics.core import (  # noqa: E402
    GroundTruth,
    Observation,
    by_isolation_level,
    safety_metrics,
)
from agentfactory.telemetry.cpu_credits import build_sampler  # noqa: E402
from agentfactory.telemetry.events import EventLog, EventType  # noqa: E402
from agentfactory.validation.aggregator import StrictnessProfile  # noqa: E402
from agentfactory.validation.layer2_policy import EnterprisePolicy  # noqa: E402
from agentfactory.validation.pipeline import PipelineConfig, ValidationPipeline  # noqa: E402
from agentfactory.validation.sandbox.base import (  # noqa: E402
    SandboxExecutionError,
    SandboxUnavailableError,
)


@dataclass(frozen=True)
class Cell:
    """One point in the matrix."""

    task_id: str
    variant: str
    isolation_level: str
    replicate: int

    @property
    def key(self) -> str:
        return f"{self.task_id}:{self.variant}:{self.isolation_level}:r{self.replicate}"


def build_runner(level: IsolationLevel, args: argparse.Namespace) -> Any:
    """Construct the runner for an isolation level.

    Raises rather than substituting a weaker sandbox. A silent downgrade would
    both weaken containment and corrupt the RQ2 comparison, so it is never the
    right recovery.
    """
    if level is IsolationLevel.L1:
        from agentfactory.validation.sandbox.l1_subprocess import SubprocessRunner

        return SubprocessRunner(require_seccomp=args.require_seccomp)

    if level is IsolationLevel.L2:
        if args.aci_resource_group:
            from agentfactory.validation.sandbox.l2_container import AciRunner

            return AciRunner(
                resource_group=args.aci_resource_group,
                subnet_id=args.aci_subnet_id,
                location=args.location,
            )
        from agentfactory.validation.sandbox.l2_container import DockerRunner

        return DockerRunner()

    from agentfactory.validation.sandbox.l3_gvisor import GvisorPodRunner

    runner = GvisorPodRunner(namespace=args.k8s_namespace)
    runner.preflight()  # fail now, not halfway through the batch
    return runner


def build_matrix(
    tasks: list[CorpusTask], levels: list[str], replicates: int, seed: int
) -> list[Cell]:
    """Full matrix in a randomized but reproducible order.

    Randomizing run order controls for time-of-day and system-load effects on
    latency; the seed is recorded in the manifest so the exact sequence can be
    replayed.
    """
    cells = [
        Cell(task.task_id, agent.variant, level, replicate)
        for task in tasks
        for agent in task.agents
        for level in levels
        for replicate in range(1, replicates + 1)
    ]
    random.Random(seed).shuffle(cells)
    return cells


def load_completed(path: Path) -> set[str]:
    """Cell keys already done, read back from the event log."""
    if not path.exists():
        return set()
    done: set[str] = set()
    for file in path.glob("*.jsonl"):
        with file.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a half-written final line after a hard kill
                if record.get("event_type") == "verdict_emitted":
                    key = record.get("detail", {}).get("cell_key")
                    if key:
                        done.add(key)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Agent Factory validation matrix.")
    parser.add_argument("--run-id", default=f"run-{datetime.now(UTC):%Y%m%d-%H%M%S}")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--levels", default="L1", help="Comma-separated: L1,L2,L3")
    parser.add_argument("--replicates", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260918)
    parser.add_argument("--profile", default="balanced",
                        choices=["permissive", "balanced", "paranoid"])
    parser.add_argument("--limit", type=int, default=0, help="Stop after N cells (smoke test).")
    parser.add_argument("--resume", action="store_true", help="Skip cells already in the log.")
    parser.add_argument("--no-bandit", action="store_true")
    parser.add_argument(
        "--test-inputs", type=int, default=3,
        help=(
            "Synthetic inputs each agent is run against in the sandbox. Every input is a "
            "separate container or pod, so this multiplies wall-clock time directly — "
            "use 1 for a timing pilot, more for the real batch."
        ),
    )
    parser.add_argument("--require-seccomp", action="store_true",
                        help="Refuse to run L1 without an active syscall filter.")
    parser.add_argument("--aci-resource-group", default=None)
    parser.add_argument("--aci-subnet-id", default=None)
    parser.add_argument("--location", default="centralindia")
    parser.add_argument("--k8s-namespace", default="agentfactory-sandbox")
    parser.add_argument(
        "--vm-resource-id",
        default=os.environ.get("AGENTFACTORY_VM_RESOURCE_ID"),
        help=(
            "Resource id of the orchestrator VM. When set, the CPU credit balance is "
            "recorded with every latency sample — necessary on burstable hosts, where "
            "throttling would otherwise be indistinguishable from isolation-level cost."
        ),
    )
    args = parser.parse_args(argv)

    levels = [lvl.strip().upper() for lvl in args.levels.split(",") if lvl.strip()]
    tasks = load_corpus()
    corpus_manifest = manifest(tasks)

    out_dir = args.out / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # The manifest ties every number this batch produces to the exact corpus,
    # seed and configuration that produced it.
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": args.run_id,
                "started_at": datetime.now(UTC).isoformat(),
                "config": {k: str(v) for k, v in vars(args).items()},
                "levels": levels,
                "corpus": corpus_manifest,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    completed = load_completed(out_dir) if args.resume else set()
    matrix = build_matrix(tasks, levels, args.replicates, args.seed)
    pending = [c for c in matrix if c.key not in completed]

    print(f"run {args.run_id}: {len(matrix)} cells, {len(completed)} already done, "
          f"{len(pending)} to run")
    if args.limit:
        pending = pending[: args.limit]
        print(f"  limited to {len(pending)} cells")

    by_task = {t.task_id: t for t in tasks}
    policy = EnterprisePolicy.load()
    profile = StrictnessProfile.named(args.profile)

    credit_sampler = build_sampler(args.vm_resource_id)
    if args.vm_resource_id:
        probe = credit_sampler.sample()
        print(f"  cpu credit sampling: {'active' if probe.available else 'unavailable'}")

    runners: dict[str, Any] = {}
    observations: list[Observation] = []
    failures = 0

    with EventLog(out_dir, run_id=args.run_id) as log:
        log.emit(
            EventType.RUN_STARTED,
            detail={"levels": levels, "n_cells": len(pending),
                    "corpus_sha256": corpus_manifest["corpus_sha256"]},
        )

        for index, cell in enumerate(pending, start=1):
            task = by_task[cell.task_id]
            agent = next(a for a in task.agents if a.variant == cell.variant)
            spec = agent.to_spec(task.contract())
            level = IsolationLevel(cell.isolation_level)

            if cell.isolation_level not in runners:
                try:
                    runners[cell.isolation_level] = build_runner(level, args)
                except SandboxUnavailableError as exc:
                    print(f"  [{cell.isolation_level}] unavailable: {exc}")
                    log.emit(EventType.RUN_FAILED,
                             isolation_level=level,
                             detail={"stage": "runner_init", "error": str(exc)})
                    # Drop every cell for this level rather than silently
                    # running them somewhere weaker.
                    pending = [c for c in pending if c.isolation_level != cell.isolation_level]
                    continue

            pipeline = ValidationPipeline(
                runners[cell.isolation_level],
                PipelineConfig.default(
                    profile=profile,
                    policy=policy,
                    use_bandit=not args.no_bandit,
                    test_inputs=tuple({"case": n} for n in range(1, args.test_inputs + 1)),
                ),
                event_log=log,
            )

            started = time.perf_counter()
            try:
                result = pipeline.validate(spec, expected_output=task.expected_output)
            except SandboxExecutionError as exc:
                # The agent never ran, so no verdict is recorded. The cell stays
                # incomplete and --resume will retry it.
                failures += 1
                print(f"  [{index}/{len(pending)}] {cell.key} SANDBOX FAILED: {exc}")
                log.emit(EventType.RUN_FAILED,
                         task_id=cell.task_id, agent_id=spec.agent_id,
                         isolation_level=level,
                         detail={"cell_key": cell.key, "stage": "sandbox", "error": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001 — a failed cell is data
                failures += 1
                print(f"  [{index}/{len(pending)}] {cell.key} FAILED: {exc}")
                log.emit(EventType.RUN_FAILED,
                         task_id=cell.task_id, agent_id=spec.agent_id,
                         isolation_level=level,
                         detail={"cell_key": cell.key, "error": str(exc)})
                continue

            elapsed = (time.perf_counter() - started) * 1000
            log.emit(
                EventType.VERDICT_EMITTED,
                task_id=cell.task_id,
                agent_id=spec.agent_id,
                code_sha256=spec.code_sha256,
                isolation_level=level,
                verdict=result.verdict,
                score=result.confidence,
                latency_ms=elapsed,
                replicate=cell.replicate,
                detail={
                    "cell_key": cell.key,
                    "variant": cell.variant,
                    "ground_truth": agent.label.value,
                    "failure_mode": agent.failure_mode,
                    "strictness_profile": args.profile,
                    "sandbox_reached": result.report_for(ValidationLayer.SANDBOX) is not None,
                    # Credit state travels with the latency it may have affected,
                    # so the analysis can condition on it instead of guessing.
                    **credit_sampler.sample().as_detail(),
                },
            )

            observations.append(
                Observation(
                    agent_id=str(spec.agent_id),
                    task_id=cell.task_id,
                    ground_truth=GroundTruth(agent.label.value),
                    verdict=result.verdict,
                    isolation_level=cell.isolation_level,
                    latency_ms=elapsed,
                    # In the matrix run nothing is deployed, so UDR is
                    # structurally zero here. It becomes meaningful in the
                    # integration test, where a PASS actually leads to
                    # execution.
                    reached_execution=False,
                    replicate=cell.replicate,
                )
            )

            if index % 25 == 0 or index == len(pending):
                print(f"  [{index}/{len(pending)}] {cell.key} -> {result.verdict.value}")

        log.emit(EventType.RUN_COMPLETED,
                 detail={"n_observations": len(observations), "n_failures": failures})

    if observations:
        overall = safety_metrics(observations)
        print("\n--- interim safety metrics (matrix run) ---")
        print(f"  n unsafe / safe : {overall.n_unsafe} / {overall.n_safe}")
        print(f"  FNR             : {overall.false_negative_rate:.3f} "
              f"[{overall.fnr_ci[0]:.3f}, {overall.fnr_ci[1]:.3f}]")
        print(f"  FPR             : {overall.false_positive_rate:.3f}")
        print(f"  accuracy        : {overall.accuracy:.3f}")
        print(f"  F1 (unsafe)     : {overall.f1:.3f}")
        for level_name, (safety, latency) in by_isolation_level(observations).items():
            print(f"  {level_name}: FNR={safety.false_negative_rate:.3f} "
                  f"p50={latency.p50:.0f}ms p95={latency.p95:.0f}ms n={latency.n}")

    print(f"\nlogs: {out_dir}")
    if failures:
        print(f"[warning] {failures} cells failed; re-run with --resume to retry them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
