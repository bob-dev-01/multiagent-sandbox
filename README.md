# Agent Factory — Sandbox-First Validation Pipeline for LLM-Generated Micro-Agents

Implementation of the architecture in [`architecture.md`](architecture.md): a system that
generates narrowly scoped micro-agents on demand, then decides automatically whether each one
is **safe to deploy** and **correct enough to be useful** before it executes.

Master's research, Solution Architecture and Data Engineering — Bobur Yusupov, IT Park University.

> **Status (2026-10-12): deployed; first full-corpus run complete.** The labelled corpus is
> complete at 30 tasks and 300 agents, and 1,800 of the experiment's 2,700 validation runs are
> done (L2 and L3; L1 is still to run on the orchestrator VM). The corpus labels still await the
> second reviewer and the LLM judge is not yet calibrated, so these are measurements, not yet
> thesis results.

## Results at a glance

| What | Result | Where |
|---|---|---|
| Classification, full corpus, L2 and L3 | Every one of 1,800 verdicts matches its label; FNR 0 (95% upper bound 0.7%), FPR 0 | [Full-corpus run 1](#full-corpus-run-1-2026-10-12) |
| Cost of gVisor (L3 vs L2) | +77.5 ms on sandbox start-up (CI 76–79); +127 ms on a ~5.4 s validation, ≈2.4% | [Full-corpus run 1](#full-corpus-run-1-2026-10-12) |
| Provisioning vs isolation | Container Instances: 43.5 s per validation, almost all provisioning; the same work on a warm AKS node: ~5 s | [Results](#results) |
| End-to-end loop with execution | No unsafe deployment (UDR 0), no catastrophic error (CER 0) | [Integration test 1](#integration-test-1-2026-10-08) |
| Reuse on reworded tasks | Close paraphrase reused 18/18, free paraphrase 0/18 at τ = 0.85 | [Integration test 1](#integration-test-1-2026-10-08) |

Progress against the experimental design:

| Part | Done | Remaining |
|---|---|---|
| Validation matrix (30 tasks × 10 agents × 3 levels × 3 replicates = 2,700) | 1,800 (L2, L3) | 900 at L1, on the orchestrator VM |
| Strictness sweep (RQ4) | — | recomputable offline from the run records |
| Integration test (RQ3) | corpus-replay generator, 10 seeds | live generator (Claude Opus 5) |
| Judge calibration (ADR-9) | blind scoring sheet and tooling | human scores, then κ |

---

## The problem

Enterprise operations generate tasks too specific for a runbook and too urgent for bespoke
engineering. An Agent Factory generates an agent for each one — which turns a capability problem
into a governance problem, because the generated code has to be judged before it runs.

Two independent failure modes, both of which must be caught automatically:

| | What it is | Why it matters |
|---|---|---|
| **Unsafe** | Out-of-scope file reads, unauthorized egress, state-changing commands, secrets in output | Blast radius is bounded only by the agent's privileges |
| **Incorrect** | Safe but wrong: misread spec, wrong tool arguments, hallucinated output | Returns a plausible answer, so it fails silently |

---

## Architecture in one picture

```
task ──> Orchestrator ──> Registry?  ──reuse──────────────────────> Executor
                │                                                      ▲
                └──no match──> Generator ──> Validation Pipeline ──PASS─┘
                                   ▲              │
                                   │              ├─ FAIL_INCORRECT ──> regenerate (max 3)
                                   └──────────────┤
                                                  └─ FAIL_UNSAFE ─────> human review
```

The pipeline runs three layers, cheapest first, and short-circuits on a hard failure:

| Layer | What it sees | Cost |
|---|---|---|
| **1 — Static** | Forbidden imports and calls, dynamic-resolution tricks, secrets, suspicious paths | ~ms, deterministic |
| **2 — Policy** | Declared tools vs. the enterprise registry, network posture, resource bounds, evidence clauses | ~ms, deterministic |
| **3 — Sandbox** | What the agent actually *does*: syscalls, network attempts, filesystem access, output correctness | 100 ms – 8 s |

Layer 1 is a cheap filter, not the defence. It is defeatable by anything that builds a name at
runtime; Layer 3 is what actually holds, because it observes behaviour rather than text.

### Isolation levels

| | Mechanism | Where it runs |
|---|---|---|
| **L1** | `subprocess` + seccomp-bpf + rlimits | Orchestrator VM (Linux only) |
| **L2** | runc pod, one per validation | AKS sandbox pool, same nodes as L3 (ACI and Docker kept as provisioning conditions) |
| **L3** | Pod under a gVisor RuntimeClass | AKS sandbox node pool — **verified** |

L3 is confirmed rather than assumed. The host node runs kernel `6.8.0-1067-azure`; a pod with
`runtimeClassName: gvisor` reports `4.19.0-gvisor`, which is the Sentry, not the host kernel.

Resource limits come from each agent's own contract, not a fixed ceiling — so an agent that
declares 256 MB is held to 256 MB.

---

## Repository layout

```
architecture.md                  design document and open questions
src/agentfactory/
  contracts/                     AgentSpec and ValidationResult — the two frozen interfaces
  validation/
    layer1_static.py             AST + bandit + custom rules
    layer2_policy.py             YAML allowlist policy engine
    sandbox/                     harness + L1/L2/L3 runners
    aggregator.py                verdict + strictness profiles (RQ4)
    pipeline.py                  the three layers, wired
  generator/                     agent generation (Claude Opus 5)
  judge/                         correctness judge (Claude Sonnet 5) behind a calibration gate,
                                 the evidence both raters see, and the blind scoring sheet
  secrets.py                     API key from the environment or Key Vault, fetched at first use
  corpus_check.py                checks every corpus label against the agent's behaviour
  registry/                      PostgreSQL + pgvector, reuse decision, embeddings
  registry/memory.py             the same reuse rules in-process, for the integration harness
  orchestrator/                  reuse-vs-generate, verdict routing, retries
  executor/                      deployment gate + sandboxed execution (TSR, CER, UDR)
  integration.py                 end-to-end loop on the corpus, cold and warm passes
  metrics/                       UDR, FNR, FPR, latency, Wilson intervals
  telemetry/                     JSONL event log with schema-on-write validation
  corpus.py                      ground-truth corpus loader
corpus/tasks/                    labelled agent corpus, one file per task
experiments/run_matrix.py        validation matrix with resume
experiments/run_integration.py   integration test (needs the sandbox infrastructure)
experiments/build_calibration_sheet.py, calibrate_judge.py   judge calibration (ADR-9)
corpus/AUTHORING.md              how a corpus task is written and when it is accepted
infra/                           Bicep templates + gVisor DaemonSet
ops/afctl.py                     resource control panel
tests/                           ~170 tests, no external services required
```

---

## Getting started

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -e ".[dev]"
pytest
```

The test suite runs entirely offline — no Azure, no Docker, no API key.

The generator and the judge read the Anthropic key from `ANTHROPIC_API_KEY` if it is set (for
example in a gitignored `.env`), and otherwise from secret `anthropic-api-key` in the project Key
Vault — through the signed-in `az` CLI on a workstation, through managed identity on the VM. The
key is fetched at the first model call and held only in memory; nothing writes it to disk.

Models are pinned for the whole study: **Claude Opus 5** (`claude-opus-5`) generates agents and
**Claude Sonnet 5** (`claude-sonnet-5`) judges correctness (architecture.md, OQ-12).

---

## Demonstrating it

Everything in this section except the last part runs on a laptop with no cloud resources.

**1. The test suite** — about 190 tests: validation layers, sandbox harness, aggregator and
strictness profiles, the Task Executor and its deployment gate, the registry's reuse rules, the
corpus, judge calibration tooling, key resolution.

```bash
pytest -q
```

**2. The corpus and its labels.** Every task's safe agents are run against the task's own tool
data and must reproduce its expected output; incorrect agents must miss it; unsafe agents must be
caught statically (and are never executed by this check).

```bash
python -m agentfactory.corpus_check            # all 30 tasks
python -m agentfactory.corpus_check T007       # one task, with per-agent detail
```

**3. The whole loop, end to end, locally.** The integration test in the suite runs orchestrator →
generator → pipeline → registry → executor on all 30 tasks and their paraphrases, with a runner
that refuses to execute any agent labelled unsafe.

```bash
pytest -q tests/test_integration.py -v
```

**4. One live judge call** (needs the key, costs about one cent):

```bash
python -c "import sys; sys.path.insert(0,'src'); from agentfactory.corpus import load_corpus; from agentfactory.judge.correctness_judge import CorrectnessJudge; from agentfactory.judge.evidence import tool_data; t=load_corpus()[0]; a=next(x for x in t.agents if x.variant=='i1'); r=CorrectnessJudge().score(a.to_spec(t.contract()), None, t.expected_output, task_rubric=t.correctness_rubric, tools_return=tool_data(t.available_tools, t.tool_fixtures), bypass_gate=True); print(r.score, r.justification)"
```

**5. Live, on the cluster** (about 10 minutes to start, a few minutes to run; stop it afterwards):

```bash
az aks start -g rg-agentfactory-dev -n agentfac-aks
kubectl -n kube-system rollout status ds/gvisor-installer
export AGENTFACTORY_SANDBOX_VMSS_ID=<resource id of the sandbox node pool's scale set>
MSYS_NO_PATHCONV=1 python experiments/run_matrix.py --run-id demo --levels L2,L3 --replicates 1 --test-inputs 1 --limit 20
python -m ops.afctl down --yes
```

Each L3 trace reports kernel `4.19.0-gvisor` and each L2 trace the host's; the run refuses to
record a trace whose kernel does not match its level. Pass resource ids through environment
variables, not arguments: Git Bash rewrites an argument starting with `/` into a Windows path,
and the experiment now refuses such an id rather than sampling nothing.

---

## Deploying

```bash
export AF_PG_PASSWORD='...'
export AF_SSH_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)"
export AF_ADMIN_PRINCIPAL_ID="$(az ad signed-in-user show --query id -o tsv)"

python -m ops.afctl deploy --what-if     # preview
python -m ops.afctl deploy               # apply
python -m ops.afctl bootstrap            # install the gVisor RuntimeClass
```

### Cost control

This deploys a VM and an AKS cluster, both of which bill while idle. The control panel exists
because leaving them up over a weekend costs more than the entire planned model budget.

```bash
python -m ops.afctl status    # what is running, and $/hour
python -m ops.afctl down      # deallocate VM, stop AKS cluster and PostgreSQL
python -m ops.afctl up        # bring it back
python -m ops.afctl cost      # actual month-to-date spend
python -m ops.afctl nuke      # delete everything (asks twice)
```

`down` preserves all data. A monthly budget with alerts at 50%, 80% and 100%-forecast is created
by the deployment itself.

### Network posture

Two subnets, two different rules — this is the containment boundary, and it is the thing to check
first if anything about the setup is ever changed:

- **orchestrator subnet** — may reach the Anthropic API, Blob, Key Vault, PostgreSQL. Holds the
  credentials. Never runs generated code.
- **sandbox subnet** — deny-all egress, no exceptions. Runs generated code. Has no identity and
  no credentials, so it could not authenticate anywhere even if it got out.

---

## Running the experiment

```bash
python experiments/run_matrix.py --levels L2,L3 --replicates 3 --test-inputs 1   # full corpus, AKS
python experiments/run_matrix.py --levels L1 --replicates 3 --test-inputs 1      # on the VM
python experiments/run_matrix.py --run-id <id> --levels L1,L2,L3 --resume        # after an interruption
```

Each run writes a `manifest.json` tying the results to the exact corpus hash, seed, git commit and
configuration, plus a JSONL event log. Interrupted batches resume from the log.

L1 needs Linux for rlimits and seccomp — run it in WSL2 or on the orchestrator VM, not on Windows.

### The integration test

```bash
python experiments/run_integration.py --level L2 --high-risk-level L3 --seeds 5
```

The whole loop — orchestrator, generator, pipeline, registry, Task Executor — on every corpus
task against an empty registry (cold pass), then on each task's paraphrases against the populated
one (warm pass). Every PASS is executed in a sandbox before the task counts as done, so this is
where TSR, ARR, MTTD, UDR at the deployment boundary and CER are measured.

What the Task Executor adds between verdict and execution, all failing closed:

- the code that runs must hash to what was approved;
- the agent's authorised tools must be a subset of what the task provides — on the warm path a
  registered agent is matched by similarity and its contract was written for another task;
- a high-risk task runs under L3 or not at all;
- execution stays sandboxed and is judged by the strict profile. Misbehaviour after PASS is a
  catastrophic error: recorded, reported to the registry so the agent is never served again, and
  its output withheld.

A refusal at the gate is what separates UDR from FNR (OQ-10). A refused warm-path match falls
back to generating a fresh agent; a refused fresh agent goes to a human.

Two things to keep in mind when reading its numbers. The generator is a **replay of the corpus**
— each request returns the next labelled variant in a seeded order — so the error profile is the
corpus distribution, not a model's; wiring the live generator (`AgentGenerator`, Claude Opus 5)
into this harness is the next step. And the warm pass needs the **real embedder** (`pip install -e ".[embeddings]"`); with
`--embedder hashing` reuse is matched on words, the run is marked lexical-only, and its ARR is
not a result.

---

## Two places this departs from the research plan

Both were decided before any data collection and are argued in `architecture.md`.

**UDR and FNR are different quantities.** Task 7 defines both as `unsafe_passed / total_unsafe`,
which makes H_1 and H_2 two names for one measurement. Here, FNR is measured at the verdict
boundary (unsafe agents that received PASS) and UDR at the deployment boundary (unsafe agents that
actually reached execution). The gap between them is what the post-verdict controls are worth — a
result the original definition could not express.

**Replicates apply to the sandbox layer only.** The corpus is fixed and hash-pinned, so no
generation happens during a matrix run and there is no generation stochasticity for replicates to
absorb. Layers 1 and 2 are deterministic, so replicating them is pseudo-replication that would
understate standard errors.

---

## Results

In the order they were measured. The first sections are the pilots on the original 3 tasks (30
agents); the full-corpus run is at the end. Run records are kept outside the repository
(`runs/`, gitignored), each with a manifest naming the corpus hash, configuration and commit.

**Pilots 1–2** — one replicate, one test input, 30 labelled agents per level.

### Classification

| | L1 | L2 | L3 |
|---|---|---|---|
| FNR | 0.000 | 0.000 | 0.000 |
| FPR | 0.000 | 0.000 | 0.000 |
| Accuracy | 1.000 | 1.000 | 1.000 |

Read this as "the filter catches what we planted", not "the filter is robust". The corpus contains
researcher-injected violations of known patterns, which is exactly the optimistic bias recorded as
OQ-9. A perfect score against a corpus you wrote yourself is a smoke test passing, not a result.

### Latency

Sandbox runs only (the 21 of 30 agents that pass Layers 1–2), p50 of wall-clock time:

| Level | Mechanism | Kernel seen by the agent | p50 | Budget |
|---|---|---|---|---|
| **L1** | subprocess + seccomp-bpf, orchestrator VM | host (`6.17.0-azure`) | **59 ms** | < 300 ms ✅ |
| **L2** | runc pod, AKS sandbox pool | host (`6.8.0-azure`) | **6.9 s** | < 3 s ❌ |
| **L3** | gVisor pod, same pool | `4.19.0-gvisor` | **7.0 s** | < 8 s ✅ |
| *(L2 on ACI)* | *ephemeral container group* | *host* | *43.5 s* | *— provisioning condition* |

Every L2 and L3 run reports the kernel it executed under, and a run whose kernel does not match
its level is rejected rather than recorded — all 21 L2 runs saw the host kernel, all 21 L3 runs
saw gVisor's.

**A retraction, before anything is read into these.** An earlier version of this section reported
L3 at 7.5 s and 8.6 s and concluded that L3 was six times faster than L2. Those L3 figures were
invalid: the image had moved to MCR's Python, which ships `python3` only, while the pod command
still called `python`. Every L3 pod failed at `StartError` before the agent ran, and the failure
was scored as `FAIL_INCORRECT` — so the number was the time taken to fail to start a container.
The ACI figure of 43.5 s was genuine and stands. The trace now records whether the harness
reported at all, and a run with no report that cannot be attributed to the agent is refused
rather than scored.

What the valid numbers say:

- **ACI's 43.5 s was provisioning.** Moving L2 onto the same warm node pool as L3 brings it to
  6.9 s with nothing else changed. That is why L2 now runs as a runc pod: so L2 and L3 differ only
  in `runtimeClassName`, and the comparison is about isolation rather than about how Azure
  hands out containers.
- **The agent's own runtime is negligible** — 0.20 ms under runc, 0.26 ms under gVisor. At the
  pod levels almost all of the ~7 s is pod lifecycle: apply, schedule, start, poll, fetch logs.
- **The measured L2→L3 gap (~0.16 s at p50) is below what the instrument can resolve.** The
  runner polls pod status once a second, so wall-clock differences under about a second are
  noise from the poll, not a property of gVisor. RQ2 cannot be answered from these numbers.

  The poll has since been replaced. The pod's own status timestamps were not the fix — the
  Kubernetes API reports them to the second, so they cannot resolve 0.16 s either. Instead the
  runner watches the pod and stamps every state change as it arrives, recording `applied`,
  `scheduled`, `running`, `terminated` and `logs_fetched` as `milestones_ms` on the trace. A
  milestone only known as an upper bound (first seen when the watch opened, or reported in the
  same update as the next one) is listed in `milestones_inexact` rather than passed off as
  exact. The sandbox also times its own start-up on its own clock — bootstrap to harness ready —
  as `sandbox_init_ms`, which is free of clock skew between hosts.

CPU credits on the sandbox node were sampled on every verdict and stayed between 61 and 64
throughout, so this pilot shows no throttling. It was also a light load; a fresh node comes up
with a small balance after every scale-from-zero, which is the case to watch in a long batch.

### Pilot 3 — the watch-based instrument (2026-10-07)

`runs/pilot3-watch`: all 30 agents × L2, L3 × 3 replicates, one test input, `strict` profile,
same node pool and image as before. 126 sandbox runs, none refused as an infrastructure failure;
every L2 run saw the host kernel and every L3 run `4.19.0-gvisor`. The run started from the
working tree that became `79cef8d` (watch opened before apply); its log records the parent,
`ac21d22`.

Classification, per level over 90 cells: 45 PASS, 27 FAIL_UNSAFE, 18 FAIL_INCORRECT — exactly the
corpus labels. FNR 0.000 (95% CI 0.000–0.066), FPR 0.000. The same caveat as above applies:
every unsafe agent was stopped by static analysis, so FNR does not vary by level by construction.

Timing, ms, n = 63 per level. The difference column is the L3 − L2 difference of medians with a
bootstrap 95% CI and a Mann–Whitney p:

| | L2 p50 | L3 p50 | L3 − L2 | 95% CI | p |
|---|---|---|---|---|---|
| `sandbox_init_ms` (in-sandbox, one clock) | 189.7 | 273.2 | **+83.5** | [79.1, 89.5] | < 10⁻¹⁵ |
| apply → scheduled | — | — | +2.6 | [−0.9, 8.0] | 0.05 |
| apply → terminated | 1415.6 | 1467.2 | +51.6 | [−10.6, 159.1] | 0.045 |
| wall clock | 5281.8 | 5347.6 | +65.8 | [−68.8, 199.2] | 0.12 |
| agent itself | 0.2 | 0.3 | — | — | — |

What this says:

- **gVisor's overhead is now measurable, and it is about 84 ms per sandbox start** — +44% on the
  bootstrap-to-harness path, with a tight interval. That is the cost of running the interpreter's
  start-up syscalls through the Sentry. The agent's own work is too short to show anything.
- **It is a small fraction of the pod lifecycle.** At this sample size it is lost in the noise of
  scheduling and container creation end to end — the full run below, with ten times the samples,
  does resolve it (+127 ms wall clock) — and both levels
  sit at ~5.3 s, of which ~3.9 s is kubectl process start-up on the orchestrator (`apply` ~1.6 s,
  log fetch ~2.2 s) — identical across levels and nothing to do with isolation.
- **`running` is almost never seen on its own** (101 of 126 runs): the agent finishes before
  kubelet's next status update, so it is reported together with `terminated` and flagged inexact.
  `apply → terminated` is the exact end-to-end figure; `running → terminated` is not usable.
- Wall time dropped from ~7 s to ~5.3 s against pilot 2 at both levels, mostly from removing the
  one-second poll and its trailing sleep.

Not established yet: whether 84 ms holds under load or on non-burstable hardware (credit sampling
for the AKS scale set reported "unavailable" this time), and how it scales with an agent that does
real I/O.

### seccomp

Verified at the kernel level rather than assumed: with the filter installed, `socket()` returns
`EPERM` from the kernel, not from the harness's own monkeypatch. All 21 L1 sandbox runs report
`seccomp_active: true`.

### Integration test 1 (2026-10-08)

`runs/integ1-l2`, commit `9b1c178`: corpus-replay generator, L2 for validation and execution
(L3 configured for high-risk tasks; the corpus has none), strict profile, all-MiniLM-L6-v2,
10 seeds. Per seed, 3 cold submissions and 6 warm (2 paraphrases per task). No infrastructure
failures.

| | Cold pass (n = 30) | Warm pass (n = 60) |
|---|---|---|
| TSR | 0.600 [0.423, 0.754] | 0.750 [0.628, 0.842] |
| ARR | — | 0.400 [0.270, 0.545] |
| Escalated | 0.400 | 0.250 |
| Mean time to deploy | 7.5 s | 5.0 s (reuse: 0.05 s; fresh: 8.3 s) |

Across both passes: FNR 0.000, **UDR 0.000** [0.000, 0.125] over 27 unsafe agents, **CER 0.000**
[0.000, 0.043] over 85 validated agents, FPR 0.000. All 63 executions were of agents labelled
safe_correct; the deployment gate refused nothing.

What this says, and what it does not:

- **Reuse is decided entirely by the wording of the paraphrase.** Where the cold pass had
  registered an agent, the close paraphrase (cosine 0.88–0.92 to the original) was reused 18 of 18
  times and the free one (0.68–0.84) 0 of 18. τ = 0.85 sits between the two. This is the OQ-8
  point measured: on identical text ARR would have been ~1.0; on rewording it depends on how far
  the rewording goes, and a rewording a person would call the same task can fall under τ.
- **Escalations are the generator's, not the pipeline's.** Every escalation was a correct
  FAIL_UNSAFE on an agent labelled unsafe; the replay serves one 30% of the time, so 40% of cold
  tasks ending with a human is the corpus distribution showing through. The TSR here is a property
  of the replay generator and must not be read as the system's TSR with a real model.
- **UDR and CER are zero, but the pipeline caught everything before the gate**, so the gate's own
  contribution (the UDR–FNR gap) is untested on this corpus. With n = 27 unsafe agents the UDR
  bound is 12.5%; with n = 85 the CER bound is 4.3% — zero observed, not zero shown (OQ-11).
- Reuse cuts time to deploy from ~8 s to ~50 ms: generation and validation are skipped entirely,
  which is also why the warm path's safety rests on the original verdict (section 9.2).

### Full-corpus run 1 (2026-10-12)

`runs/full2-l2l3`, commit `885742d`: all 30 tasks, 300 agents × L2, L3 × 3 replicates = 1,800
cells, strict profile, bandit enabled in Layer 1, one test input. No infrastructure failures;
every L2 run saw the host kernel and every L3 run `4.19.0-gvisor`. The CPU-credit balance was
recorded on all 1,800 verdicts and stayed between 68 and 157, rising through the run — no
throttling. (An earlier attempt, `full1-l2l3`, was stopped after 10 minutes when its credit
samples turned out empty; see `885742d`.)

**Classification — every verdict matches its label at both levels:**

| Label | n (per level) | Verdict |
|---|---|---|
| safe_correct | 450 | PASS 450 |
| incorrect_only | 180 | FAIL_INCORRECT 180 |
| unsafe_only | 180 | FAIL_UNSAFE 180 |
| both | 90 | FAIL_UNSAFE 90 (stopped before execution; correctness unknown, not failed) |

FNR 0.000 [0.000, 0.007] over 540 unsafe cells, FPR 0.000 over 1,260 safe. All 60 incorrect
agents — including the borderline ones with a partial match — are caught under strict. As with
the pilots this is the corpus's own planted faults, so it shows the pipeline works on them, not
that it resists an adaptive adversary; and every unsafe agent was stopped by Layers 1-2, so FNR
cannot differ between levels here (H_2, open).

**Latency, sandbox runs only** (n = 630 per level; L3 − L2 difference of medians, bootstrap 95%
CI, Mann–Whitney p):

| | L2 p50 | L3 p50 | L3 − L2 | 95% CI | p |
|---|---|---|---|---|---|
| `sandbox_init_ms` (in-sandbox) | 193.0 | 270.5 | **+77.5** | [76.1, 79.0] | < 10⁻¹⁵ |
| apply → terminated | 1578.6 | 1637.1 | **+58.4** | [25.2, 91.8] | 2 × 10⁻¹³ |
| wall clock | 5323.2 | 5450.2 | **+127.0** | [95.2, 173.9] | < 10⁻¹⁵ |
| agent itself | 0.3 | 0.4 | — | — | — |

- **gVisor's start-up cost holds at scale**: +78 ms (+40%) on the in-sandbox path, across 30
  tasks rather than 3, with a tighter interval than pilot 3's 84 ms.
- **End to end it is now resolved, and small**: +127 ms on a ~5.4 s wall clock, about 2.4%.
  Pilot 3 could not see it (n = 63); this is the same effect with ten times the samples.
- The agents still run for well under a millisecond at p50 (p95 ≈ 3–4 ms), so syscall overhead
  during the agent's own work is not what this corpus measures — a corpus with heavier agents
  would be needed for that.
- `running` collapsed into `terminated` in 73% of runs; `apply → terminated` remains the exact
  end-to-end figure.

---

## Deployed environment

Live in **centralindia** on an Azure for Students subscription, resource group
`rg-agentfactory-dev`: orchestrator VM, AKS with a system pool and a tainted gVisor sandbox pool,
PostgreSQL Flexible Server, GRS blob storage, Key Vault, Log Analytics and Application Insights,
and a monthly budget with alerts.

Idle burn is about **$0.27/hour (~$194/month)** against a $100 credit, so the environment is meant
to be switched off between batches:

```bash
python -m ops.afctl down    # after every session — takes burn to zero
```

`down` stops the whole AKS cluster rather than scaling its pools, because a system pool cannot go
below one node and would keep billing about $60/month on its own.

Three constraints this subscription imposes, all discovered during deployment:

- **Region.** An allowed-locations policy permits only swedencentral, centralindia, austriaeast,
  denmarkeast and indiasouthcentral.
- **Quota.** 6 vCPU per region, 4 per D-family, and **no v5 or v6 family has any quota at all**.
- **SKU availability.** Every D-family SKU that has quota is either capacity-restricted for VMs or
  refused by AKS. `az vm list-skus` reports these as unrestricted, which is not the same as
  available — only deployment preflight tells the truth.

The intersection is exactly one family, **B-series v2**, so everything runs on `Standard_B2s_v2`.
That puts the two nodes carrying RQ2 latency measurements on burstable hardware. The confound
cannot be removed under this quota, so it is made visible instead: `telemetry/cpu_credits.py`
samples the CPU credit balance and the harness attaches it to every verdict event, next to the
latency it may have affected.

Scaling the sandbox pool to zero destroys the gVisor install, because the node comes back as a new
VM. The installer DaemonSet reinstalls it automatically in a few minutes; `afctl up` says so, and
`GvisorPodRunner.preflight()` refuses to run a batch until the RuntimeClass resolves — L3 never
silently degrades to runc.

---

## What is not done yet

Honest list, so nobody discovers these the hard way:

- **The corpus labels are not yet second-reviewed.** 30 tasks, 10 per domain, 300 agents, each
  task with its own tool data (`corpus/AUTHORING.md`). Tasks T002–T010, T012–T020 and T022–T030
  were drafted with AI assistance and pass `python -m agentfactory.corpus_check`, which confirms
  every safe agent reproduces the expected output, every incorrect one does not, and every unsafe
  one is flagged statically. What it cannot confirm — that each task's expected output is the
  right answer to its description, and that unsafe_only agents are otherwise correct — is the
  reviewer's job. Every injected violation is a plain pattern Layers 1-2 recognise, so FNR does
  not vary by isolation level on this corpus (H_2, open).
- **L1 has not run in the full experiment.** It needs Linux (rlimits, seccomp), so it runs on the
  orchestrator VM: 900 of the 2,700 planned validations.
- **The LLM judge is not calibrated.** The blind scoring sheet and the comparison script exist
  (`experiments/build_calibration_sheet.py`, `experiments/calibrate_judge.py`); the human scores
  do not yet. Until κ ≥ 0.80 the judge does not score unattended (ADR-9).
- **The integration test still uses the corpus-replay generator**, so its TSR and escalation
  rate describe the corpus, not a model. The live generator has its key and model pinned but is
  not yet wired into the harness.
- **The strictness sweep (RQ4) has not been computed.** It should be recomputable from the
  existing run records without new runs.
- **The registry schema has not been applied** to the deployed PostgreSQL server. The
  integration harness uses the in-process registry, which enforces the same rules.
- **The Task Executor is not wrapped in AutoGen**, which the research plan names; it is plain
  Python behind the same interfaces.
- **The agents are light.** They run for under a millisecond, so this corpus measures gVisor's
  start-up cost, not its per-syscall cost during heavy agent work.
- **Practitioner interviews (RQ5, RQ6)** are outside this repository.

See section 16 of `architecture.md` for the fifteen open questions this implementation is built
against. OQ-2 — whether gVisor on AKS is deployable at all — is now answered: it is.
