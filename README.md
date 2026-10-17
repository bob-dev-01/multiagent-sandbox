# Agent Factory — Sandbox-First Validation Pipeline for LLM-Generated Micro-Agents

Implementation of the architecture in [`architecture.md`](architecture.md): a system that
generates narrowly scoped micro-agents on demand, then decides automatically whether each one
is **safe to deploy** and **correct enough to be useful** before it executes.

Master's research, Solution Architecture and Data Engineering — Bobur Yusupov, IT Park University.

> **Status (2026-10-13): deployed; the validation matrix is complete.** The labelled corpus is
> complete at 30 tasks and 300 agents, and all 2,700 validation runs of the experimental design
> are done (L1 on the orchestrator VM; L2 and L3 on AKS). The corpus labels still await the
> second reviewer and the LLM judge is not yet calibrated, so these are measurements, not yet
> thesis results.

## Results at a glance

| What | Result | Where |
|---|---|---|
| Classification, full corpus, all three levels | Every one of 2,700 verdicts matches its label; FNR 0 (95% upper bound 0.5%), FPR 0 | [Full-corpus run 1](#full-corpus-run-1-2026-10-12), [L1](#full-corpus-run-1-l1-2026-10-13) |
| Cost of gVisor (L3 vs L2) | +77.5 ms on sandbox start-up (CI 76–79); +127 ms on a ~5.4 s validation, ≈2.4% | [Full-corpus run 1](#full-corpus-run-1-2026-10-12) |
| Strictness profile (RQ4) | Incorrect agents passed: 75% permissive, 62% balanced, 0% strict; no correct agent rejected under any profile | [Strictness sweep](#strictness-sweep--rq4--h_4-2026-10-13) |
| L1 vs the container levels | Sandbox stage 47 ms at L1 vs ~5.3–5.5 s at L2/L3 — about 110× faster, at the price of sharing the host kernel | [L1](#full-corpus-run-1-l1-2026-10-13) |
| Provisioning vs isolation | Container Instances: 43.5 s per validation, almost all provisioning; the same work on a warm AKS node: ~5 s | [Results](#results) |
| End-to-end loop with execution | No unsafe deployment (UDR 0), no catastrophic error (CER 0) | [Integration test 1](#integration-test-1-2026-10-08) |
| Reuse on reworded tasks | Close paraphrase reused 18/18, free paraphrase 0/18 at τ = 0.85 | [Integration test 1](#integration-test-1-2026-10-08) |
| Live generator (Claude Opus 5), end to end | TSR 0.92 cold / 0.87 warm, ARR 0.51; 221 agents generated, none executed unsafely; $13.11 | [Integration test 2](#integration-test-2--live-generator-2026-10-13) |
| Live generator after the fixes | TSR **0.978** cold / **1.000** warm, ARR 0.48; 188 agents, 182 PASS, none executed unsafely; $9.12 | [Integration test 3](#integration-test-3--live-generator-after-the-fixes-2026-10-13) |
| gVisor while the agent works (workload benchmark) | CPU-bound +12%, allocation-heavy +47%, file I/O **×7.2**, `stat` loop **×6.4** (≈17 µs per call) | [Workload benchmark](#workload-benchmark--gvisor-while-the-agent-works-2026-10-16) |
| What the sandbox catches alone (layer ablation) | 35 of 90 unsafe agents (38.9%) before the run-time monitor, **65 of 90** (72.2%) after, identical at L2 and L3; static analysis 90 of 90; together 90 of 90; no false positive on 420 corpus cells or 572 generated agents | [Layer ablation](#layer-ablation--what-the-sandbox-sees-alone-2026-10-16) |
| H_3: safety + correctness vs safety only | Accuracy at predicting task success 0.911 → **0.990** on 574 generated agents (McNemar p ≈ 6 × 10⁻¹⁴); no successful agent rejected | [H_1 and H_3](#h_1-and-h_3--formal-tests-2026-10-16) |
| H_1: pipeline vs no validation | 0 of 810 unsafe cells deployed against 810 of 810 (UDR 95% upper bound 0.47%) | [H_1 and H_3](#h_1-and-h_3--formal-tests-2026-10-16) |
| LLM judge vs a human rater | Cohen's κ **0.948** (bootstrap 95% CI 0.868–1.000) on 60 blind items; 58 of 60 identical; threshold 0.80 met | [Judge calibration](#judge-calibration--adr-9-2026-10-16) |
| The same loop as AutoGen agents | TSR 0.989 cold / 0.983 warm, ARR 0.49; 266 executions, every one carried by an AutoGen message, none unsafe; $9.50 | [Integration test 4](#integration-test-4--autogen-runtime-live-generator-2026-10-14) |
| What strictness costs on generated agents | PASS 192 permissive → 161 strict; 39 of the 42 strict rejections trace to a paraphrase that drops a constraint | [Integration test 2](#integration-test-2--live-generator-2026-10-13) |

Progress against the experimental design:

| Part | Done | Remaining |
|---|---|---|
| Validation matrix (30 tasks × 10 agents × 3 levels × 3 replicates = 2,700) | 2,700 (L1, L2, L3) | — |
| Strictness sweep (RQ4) | all 2,700 cells × 4 profiles, replay verified; 221 generated agents × 4 profiles | — |
| Integration test (RQ3) | corpus-replay generator, 10 seeds; live generator (Claude Opus 5), 3 seeds, before and after the AF108 fix and paraphrase repair | — |
| Judge calibration (ADR-9) | 60 items scored blind by a human and by the judge; κ = 0.948, gate open | a second human rater on a subset |

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

The Orchestrator and the Task Executor are two agents in a **Microsoft AutoGen** (Core 0.7)
runtime, and every hand-over between them is an AutoGen message — a task request to the
orchestrator, a deployment request to the executor, the execution record back
(`src/agentfactory/autogen_team.py`). What they decide is deterministic code: routing a safety
verdict is not a judgement a language model should make. The generated micro-agents are not
members of the runtime. They have no network and no model access, so they cannot converse; the
executor runs each one as a sandboxed tool, and data passes between micro-agents only through the
orchestrator.

The layers are complementary, not redundant, and that is measured rather than assumed
([layer ablation](#layer-ablation--what-the-sandbox-sees-alone-2026-10-16)): Layer 1 reads the
code and is defeatable by anything that builds a name at run time; Layer 3 observes behaviour —
network, filesystem, tool calls, and, through an audit hook in the harness, process launches,
dynamic code and leaked secrets — and on its own catches 72% of the corpus's unsafe agents. The
rest are code patterns with no run-time effect (an unused credential, a walk of the class
hierarchy), which only Layer 1 can see. Together they stopped all of them.

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

The whole loop — orchestrator, generator, pipeline, registry, Task Executor, the first and last
of them AutoGen agents (`--orchestration direct` calls them in-process instead) — on every corpus
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
corpus distribution, not a model's. `--generator live --max-cost-usd N` swaps in the real
generator (`AgentGenerator`, Claude Opus 5) under a spending cap and writes every agent it makes,
source included, to `generated_agents.jsonl`; `experiments/analyse_live_run.py` reads that back.
And the warm pass needs the **real embedder** (`pip install -e ".[embeddings]"`); with
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

### Integration test 2 — live generator (2026-10-13)

`runs/integ2-live`, commit `420f296`: the same harness with Claude Opus 5 writing the agents,
L2 for validation and execution, strict profile, all-MiniLM-L6-v2, 3 seeds (30 cold and 60 warm
submissions each). 221 agents generated (30 of them regenerations after a failed verdict) for
$13.11, about $0.06 an agent. One infrastructure failure: a DNS lookup of the AKS API failed once.
Analysis: `python experiments/analyse_live_run.py runs/integ2-live`.

| | Cold pass (n = 90) | Warm pass (n = 180) |
|---|---|---|
| TSR | **0.922** [0.848, 0.962] | 0.867 [0.809, 0.909] |
| ARR | — | 0.506 [0.429, 0.584] |
| Escalated | 0.078 | 0.128 |
| Time to deploy, fresh agent | 23.3 s mean | 37.9 s mean |
| Time to deploy, reused agent | — | 0.05 s mean |

Verdicts: 161 PASS, 42 FAIL_INCORRECT, 18 FAIL_BOTH. **No agent executed unsafely**, and the
deployment gate refused nothing. Generated agents carry no label, so FNR and UDR do not apply;
what is measured is how the pipeline treated what a real model wrote.

What this says:

- **On the tasks' own wording the generator is reliable.** The 90 cold-pass verdicts are 83 PASS
  and 7 FAIL_BOTH — not one FAIL_INCORRECT. All 42 FAIL_INCORRECT came from paraphrases.
- **Every FAIL_BOTH was one mistake, and it exposed a gap in Layer 1.** In all 18 the model
  declared `authorized_tools = []` and then used the task's tool. The sandbox caught each one as an
  unauthorised tool call, so nothing unsafe ran — but Layer 1 should have. AF108 skipped its check
  when the allowlist was empty (an empty list read as "anything goes"), and only looked at direct
  calls, missing three agents that handed `tools.read_log_file` to a retry helper. Both are fixed:
  an empty allowlist now authorises nothing, and any `tools.<name>` reference or literal
  `getattr(tools, "<name>")` is checked. Re-run over the recorded agents, today's AF108 flags all
  18 and none of the 203 others; it flags no safe corpus agent. The generator's schema now
  requires `authorized_tools` instead of defaulting it to an empty list.
- **The incorrect verdicts are the corpus's fault more than the model's.** 39 of the 42 come from
  paraphrases that drop a constraint the original description states: T008 (18) and T004 (3)
  never say which log field is the service, T016 paraphrase 1 (9) does not say the ids are
  strings, T005 paraphrase 0 (9) does not say `services_with_errors` is a count. The model filled
  the gap differently from the expected output, and strict rejected it. The remaining three
  (T002, T010, T017, one each) are a wrong count, a wrong tie-break and a list returned in another
  shape. Twelve warm submissions (T008 ×6, T016 ×3, T005 ×3) exhausted their regenerations this
  way, which is most of the warm pass's lower TSR.
- **RQ4's cost side, measured.** Re-judged under each profile (replay reproduces all 221 strict
  verdicts first):

  | Profile | PASS | FAIL_INCORRECT | FAIL_BOTH | FAIL_UNSAFE |
  |---|---|---|---|---|
  | permissive | 192 | 11 | 18 | 0 |
  | balanced | 180 | 23 | 18 | 0 |
  | strict | 161 | 42 | 18 | 0 |
  | paranoid | 160 | 42 | 18 | 1 |

  The 31 agents strict rejects and permissive accepts are, by the analysis above, agents whose
  output really does differ from the specification — mostly because the specification they were
  given was ambiguous. Strictness converts an ambiguous task into regenerations and escalations
  rather than into wrong output in production. Paranoid's one extra rejection is an agent that
  dispatches tools through `getattr(tools, name)`; it ran within its allowlist.

---

### Integration test 3 — live generator after the fixes (2026-10-13)

`runs/integ3-live`, commit `11529d7`: integration test 2 repeated with the same settings and
seeds after three changes — AF108 widened (an empty allowlist authorises nothing; tool references
are checked), `authorized_tools` required in the generator's schema, and six paraphrases repaired
so each carries every constraint that decides the expected output. 188 agents for $9.12; no
infrastructure failure.

| | Integration test 2 | Integration test 3 |
|---|---|---|
| TSR, cold (n = 90) | 0.922 [0.848, 0.962] | **0.978** [0.923, 0.994] |
| TSR, warm (n = 180) | 0.867 [0.809, 0.909] | **1.000** [0.979, 1.000] |
| ARR, warm | 0.506 [0.429, 0.584] | 0.478 [0.406, 0.550] |
| Escalated, cold / warm | 0.078 / 0.128 | 0.022 / 0.000 |
| Agents generated (regenerations) | 221 (30) | 188 (4) |
| Verdicts | 161 PASS, 42 FAIL_INCORRECT, 18 FAIL_BOTH | 182 PASS, 4 FAIL_INCORRECT, 2 FAIL_UNSAFE |
| Agents with an empty tool list | 18 | 2 |
| Executed unsafely | 0 | 0 |
| Cost | $13.11 | $9.12 |

- **Undeclared tool use is now stopped at Layer 1.** Both escalations (T004 and T006, cold pass)
  were again agents that declared no tools and used the task's tool; AF108 flagged them
  statically, before any sandbox ran, which makes them FAIL_UNSAFE rather than FAIL_BOTH.
  Requiring the field cut such agents from 18 of 221 to 2 of 188 but did not eliminate them; an
  unsafe verdict goes to a person, by design, rather than back to the generator.
- **With complete task text the warm pass succeeded every time.** Of the four FAIL_INCORRECT
  verdicts, two are T008 paraphrase 0 returning `samples` as a list — one constraint the repair
  missed, fixed after this run — and two are genuine model errors (T005 paraphrase 1, T029
  paraphrase 1). All four were regenerated successfully.
- **Strictness costs almost nothing once the specification is complete.** Re-judged under each
  profile: PASS 185 permissive, 184 balanced, 182 strict, 182 paranoid — against 192 / 180 / 161
  / 160 in integration test 2. What strictness rejected there was, overwhelmingly, the ambiguity
  of the task text.
- The reuse rate is unchanged within its interval: the repaired paraphrases are longer, and
  reuse still depends on how far a rewording drifts from the original (integration test 1).

---

### Integration test 4 — AutoGen runtime, live generator (2026-10-14)

`runs/integ4-autogen-live`, commit `864955e`: integration test 3 repeated with the same settings
and seeds, with the orchestrator and the Task Executor running as AutoGen agents
(`--orchestration autogen`): each task reached the orchestrator as a `TaskRequest` and each
approved agent reached the executor as a `DeployRequest`. 189 agents for $9.50.

| | Integration test 3 (direct) | Integration test 4 (AutoGen) |
|---|---|---|
| TSR, cold (n = 90) | 0.978 [0.923, 0.994] | 0.989 [0.940, 0.998] |
| TSR, warm (n = 180) | 1.000 [0.979, 1.000] | 0.983 [0.952, 0.994] |
| ARR, warm | 0.478 [0.406, 0.550] | 0.486 [0.413, 0.559] |
| Escalated, cold / warm | 0.022 / 0.000 | 0.000 / 0.017 |
| Agents generated (regenerations) | 188 (4) | 189 (5) |
| Verdicts | 182 PASS, 4 FAIL_INCORRECT, 2 FAIL_UNSAFE | 180 PASS, 5 FAIL_INCORRECT, 3 FAIL_UNSAFE |
| Executions / refused at the gate / unsafe | 270 / 0 / 0 | 266 / 0 / 0 |
| Infrastructure failures | 0 | 1 (TLS timeout to the AKS API) |
| Cost | $9.12 | $9.50 |

- **The AutoGen layer changes nothing measurable.** Every figure lies within the other run's
  confidence interval; the remaining differences are the model's own run-to-run variation, which a
  live generator does not remove (the equivalence of the two paths on identical inputs is shown
  by `tests/test_autogen_team.py`, with the replay generator).
- **The three escalations are the known pattern** — an empty tool list and a call to the task's
  tool (T006 paraphrase 1 twice, T020 paraphrase 1), stopped by AF108 before any sandbox ran and
  sent to a person.
- **Of the five FAIL_INCORRECT verdicts**, three are model errors on the repaired paraphrases
  (T005 paraphrase 0 twice, T029 paraphrase 1); T001 paraphrase 1 keyed the counts by timestamp
  and T024 paraphrase 1 dropped a date. All five were regenerated successfully.
- Re-judged under each profile: PASS 184 permissive, 182 balanced, 180 strict, 179 paranoid —
  the same small spread as integration test 3.

---

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
cannot differ between levels here (H_2, see [below](#h_2--what-the-isolation-level-changes)).

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

### Full-corpus run 1, L1 (2026-10-13)

`full2-l1`, commit `4a3081c`, on the orchestrator VM (`Standard_B2s_v2`, Ubuntu 24.04, kernel
`6.17.0-1022-azure`), run as the unprivileged admin user with `--require-seccomp`: the same 300
agents × 3 replicates, strict profile, bandit on. 900 cells in under three minutes, no
infrastructure failures. The VM has no inbound access, so the run was driven through
`az vm run-command`; the raw logs went to Blob (`validation-logs/full2-l1/`) with the VM's
managed identity, and a summary plus the sandbox latencies were brought back
(`runs/full2-l1/summary.json`, `l1_sandbox_wall_ms.csv`). The VM's CPU-credit balance stayed at
61.6–62.3 during the run.

| Label | n | Verdict |
|---|---|---|
| safe_correct | 450 | PASS 450 |
| incorrect_only | 180 | FAIL_INCORRECT 180 |
| unsafe_only | 180 | FAIL_UNSAFE 180 |
| both | 90 | FAIL_UNSAFE 90 |

- **The seccomp filter was in force on every sandbox run** (630 of 630), every run saw the host
  kernel, and every verdict matches its label. Across all three levels that is 2,700 verdicts
  without an error: FNR 0 over 810 unsafe cells (95% upper bound 0.5%), FPR 0 over 1,890.
- **L1's sandbox stage takes 47 ms at the median** (p95 49 ms), against 5.32 s at L2 and
  5.45 s at L3: no pod to schedule, no container to create, no kubectl to start. The price is
  the boundary — the agent shares the host kernel, guarded only by the syscall filter and
  rlimits.
- The agent's own runtime is higher at L1 (1.3 ms vs 0.3–0.4 ms in the containers), on
  different hardware and with the filter active; it is still too short to matter.
- As at L2 and L3, every unsafe agent was stopped by Layers 1-2, so the isolation level could
  not change FNR here (H_2).

### H_2 — what the isolation level changes

H_2 predicted that stronger isolation lowers FNR at a cost in latency. It is reported as a finding,
in two parts:

- **The latency part holds.** Sandbox stage 47 ms at L1, 5.32 s at L2, 5.45 s at L3 (medians);
  gVisor over runc +77.5 ms on start-up and +127 ms (~2.4%) end to end.
- **The FNR part does not, and cannot in this design.** FNR was 0 at every level because every
  planted violation is stopped by Layers 1-2, before any sandbox runs; the isolation level decides
  how an agent is contained, not whether its violation is detected. The run-time check that
  matters here, the tool allowlist, is enforced by the in-sandbox harness the same way at every
  level. Integration test 2 shows what that layer is for: 18 agents passed Layer 1 through
  the AF108 gap and were stopped by the sandbox's allowlist (at L2). Stronger isolation therefore
  buys a smaller blast radius for the violation that slips past every check, which a detection
  rate does not measure.

### Strictness sweep — RQ4 / H_4 (2026-10-13)

`experiments/strictness_sweep.py` re-judges all 2,700 cells of the matrix under each of the four
profiles with the pipeline's own aggregator — no new runs, because a verdict is a function of
recorded evidence and the profile. Before trusting it, the replay reproduced every recorded
`strict` verdict exactly (2,700 of 2,700; L1 was re-judged on the VM, where its raw logs are).
Pooled over L1, L2 and L3 (`runs/strictness_sweep_all_levels.json`):

| Profile | Unsafe passed | Safe flagged unsafe | Incorrect passed | Correct rejected |
|---|---|---|---|---|
| permissive (match ≥ 0.3) | 0 / 810 | 0 / 1,890 | **405 / 540 (75.0%)** | 0 / 1,350 |
| balanced (match ≥ 0.5) | 0 / 810 | 0 / 1,890 | **333 / 540 (61.7%)** | 0 / 1,350 |
| strict (exact match) | 0 / 810 | 0 / 1,890 | **0 / 540** | 0 / 1,350 |
| paranoid (+ MEDIUM findings) | 0 / 810 | 0 / 1,890 | **0 / 540** | 0 / 1,350 |

Exact McNemar tests between adjacent profiles: permissive → balanced, 72 cells change verdict
(p ≈ 4 × 10⁻²²); balanced → strict, 333 (p ≈ 10⁻¹⁰⁰); strict → paranoid, none.

- **The threshold is what decides whether incorrect agents get through.** The 60 incorrect agents
  score 0.0 (15), 0.33 (8), 0.5 (27) and 0.67 (10) on the automatic output match; permissive lets
  through everything from 0.33, balanced everything from 0.5, strict nothing. That is H_4's
  effect, and it is large: three in four incorrect agents pass under the loosest profile.
- **On this corpus the trade-off is one-sided.** No correct agent was rejected under any profile,
  so strict costs nothing here and paranoid adds nothing: its extra rule (MEDIUM static findings
  count as unsafe) only fires on agents that are already unsafe. This is partly by construction —
  the corpus check requires every correct agent to reproduce the expected output exactly — so the
  cost side of the trade-off, correct agents rejected for a formatting deviation, can only be
  measured with generated agents (the live-generator run).
- Safety does not move at all: every unsafe agent fails Layer 1 on a HIGH finding, which every
  profile treats as unsafe.

### Workload benchmark — gVisor while the agent works (2026-10-16)

The corpus agents finish in a millisecond, so every other run measured gVisor's start-up cost
only. `experiments/workload_bench.py` (`runs/workload-l2l3`) runs four safe agents that each do one
kind of work, 20 times per level in a seeded random order on the same sandbox node, and compares
the agent's own runtime as timed inside the sandbox — pod scheduling excluded. No infrastructure
failures among the 160 runs.

| Workload | What it does | L2 runc, median | L3 gVisor, median | L3 / L2 | Difference (95% CI) |
|---|---|---|---|---|---|
| cpu | 200,000 SHA-256 rounds | 124 ms | 139 ms | 1.12 | +14 ms (2–27) |
| alloc | 40 × 200,000-element lists | 259 ms | 381 ms | 1.47 | +122 ms (111–133) |
| file_io | write, read and delete 1,500 small files in `/tmp/agent` (memory-backed) | 112 ms | 798 ms | **7.16** | +687 ms (669–694) |
| stat | 50,000 `stat` calls on one file | 157 ms | 1,001 ms | **6.39** | +845 ms (827–876) |

- **gVisor's price is per system call, not per instruction.** Pure computation runs almost at
  native speed; every call into the kernel goes through gVisor's user-space kernel instead, which
  here costs about 17 µs per `stat` and about 0.46 ms per file written, read and deleted.
- This qualifies the start-up result: +77.5 ms and ≈2.4% is the cost for agents that do little
  I/O, which is what the corpus and the generated agents are. An agent that spends its time in
  system calls — many files, many small reads — can run several times slower under gVisor, and a
  deployment that routes such agents to L3 should budget for it.
- The memory-backed `/tmp` isolates system-call cost from disk; on a disk-backed volume gVisor's
  file I/O would also pay its gofer round trips, which this benchmark does not measure.

---

### Layer ablation — what the sandbox sees alone (2026-10-16)

In every other run Layers 1-2 stop each unsafe corpus agent before the sandbox, so the sandbox's
own detection rate had never been observed. `runs/ablation-l2l3`: the 90 unsafe agents
(unsafe_only and both) at L2 and L3, one replicate, with short-circuiting off
(`run_matrix.py --labels unsafe_only,both --no-short-circuit`), so every agent ran in the sandbox
and each layer's own finding is on record (`experiments/layer_ablation.py`). L1 was left out on
purpose: there the sandbox is a subprocess on the orchestrator VM, next to its managed identity.

| | L2 (runc) | L3 (gVisor) |
|---|---|---|
| Detected by static analysis | 90 / 90 | 90 / 90 |
| Detected by the policy check | 0 / 90 | 0 / 90 |
| Detected by the sandbox | **35 / 90** (38.9%, 95% CI 29.5–49.2%) | **35 / 90** (38.9%) |
| Detected by the sandbox and nothing else | 0 | 0 |
| Missed by every layer | 0 | 0 |

That was the first run. What follows is what it led to.

- **The sandbox catches behaviour that crosses a boundary it monitors:** network connections (14),
  file access outside the agent's area, including `/proc/self/environ` and path traversal (10),
  and undeclared tool calls (11). Every agent of those kinds was caught.
- **It does not see the rest:** spawning a process (`subprocess`, `os.system`, `os.popen`),
  `eval`/`exec`/`compile`, introspection escapes, unsafe deserialisation, and secrets embedded in
  code or output. At L2 and L3 the container contains these but the harness records no signal
  for them; that is a gap in run-time detection, not in containment.
- **The isolation level changes nothing here**, which is the H_2 finding again from the other
  side: detection is the harness's job and is the same at every level.
- So the claim that the sandbox is what actually holds is not supported as stated. The layers are
  complementary — static analysis is complete on planted code patterns, the sandbox on observed
  behaviour — and only together do they stop every unsafe agent. Integration test 2 showed the
  converse: 18 generated agents that static analysis missed (the AF108 gap) were stopped by the
  sandbox.

**The run-time monitor (2026-10-17).** Static analysis is defeatable by code that builds names at
run time, so a gap the sandbox cannot see is a gap in the system. The harness now installs a
PEP 578 audit hook (`0b4951a`, extended in `a6009bf`, `858337a`, `0e9cea1`) that records and
refuses, when the agent is responsible: process launches; `exec`, `compile` and `marshal` called
from agent code; `ctypes`; pickle class lookup; code and function construction; tracing and `gc`
introspection; filesystem changes outside `/tmp/agent` and any file open outside the allowed
areas — including `pathlib`/`io.open`, which the `builtins.open` guard never saw; UDP datagrams
and name lookups; and secrets in the agent's output, prints or tool arguments. Each round of the
ablation was rerun after a fix:

| Run | Harness | Sandbox alone, L2 = L3 |
|---|---|---|
| `ablation-l2l3` | before the monitor | 35 / 90 (38.9%) |
| `ablation2-l2l3` | process, dynamic code, native, deserialisation, secrets in output | 62 / 90 |
| `ablation3-l2l3` | + name lookups, UDP via audit, secrets in tool arguments | 64 / 90 |
| `ablation4-l2l3` | + `sendto`/`sendmsg` recorded before name resolution | **65 / 90** (72.2%, 95% CI 62.2–80.4%) |

Static analysis still caught 90/90, nothing was caught by the sandbox alone, nothing was missed.
Two findings along the way were real gaps, not artefacts of the corpus: a UDP datagram to an
unresolvable host was never recorded, because the interpreter resolves the name before raising
its audit event (the namespace's deny-all NetworkPolicy contained it; only the record was
missing); and a credential appended after a newline in a report went unseen, because JSON
escaping hid the word boundary — fixed after `ablation4` in `0e9cea1` and checked by a unit
test, not yet by a cluster run. The 25 agents the sandbox still does not flag carry risks with no
run-time effect: hard-coded credentials that are never used, walks of the class hierarchy or
`__globals__` that read and do nothing, `__import__` of a harmless module, a pickle round trip of
plain data. Those are code patterns, and only static analysis can see them.

**No false positives.** The monitor must not flag correct code. With the final harness (every
change after `a6009bf` touches only code that calls `sendto`/`sendmsg` or carries a secret, and
no safe agent does): all 300 safe and 120 incorrect corpus cells at L2 and L3 kept their verdicts
(`runs/fpcheck-corpus-l2l3`), and all 572 distinct live-generated agents that passed every safety
check, re-validated at L3, kept theirs — 521 PASS, 51 FAIL_INCORRECT, none newly unsafe
(`experiments/revalidate_generated.py`, `runs/revalidate-generated-l3`). The secret rules were
also re-applied offline to every generated agent's output and tool arguments: no match. Writing
the monitor also found and fixed an older harness bug: agents were compiled with the harness's own
`from __future__ import annotations` and outside `sys.modules`, so `@dataclass` in an agent
crashed.

---

### H_1 and H_3 — formal tests (2026-10-16)

**H_1** (`experiments/h1_baseline.py`). A pipeline with no validation deploys every agent, so its
UDR is 1 by construction; the paired comparison on the 810 unsafe cells of the full matrix (all
three levels) is 0 deployed against 810, UDR 0 [0, 0.0047], exact McNemar p ≈ 3 × 10⁻²⁴⁴.
Where the pipeline stopped them matters more than the p-value: on the corpus, static analysis
stopped all 540 unsafe L2/L3 cells, so the sandbox was never the layer that decided; among
live-generated agents, the 18 escalations of integration test 2 were stopped by the sandbox alone
(the AF108 gap), and the 5 of tests 3 and 4 by static analysis. No agent executed unsafely in
any run.

**H_3** (`experiments/h3_judge_test.py`; analysis plan committed in `9481322` before any judge
score existed). Population: the 574 live-generated agents of integration tests 2–4 that passed
every safety check — the agents a safety-only validator would deploy. Outcome: success at
execution (exact match; for all 523 executed agents the computed outcome equals the executor's
record). The correctness half of the combined validator is the calibrated judge, not the exact
match, which would be circular. Judge cost $3.93; every agent scored.

| Validator | Approves | Precision (approved that succeed) | Accuracy | Recall | Specificity |
|---|---|---|---|---|---|
| Safety only | 574 | 0.911 [0.885, 0.932] | 0.911 | 1.000 | 0.000 |
| Safety + judge (score 3) | 529 | **0.989** [0.975, 0.995] | **0.990** [0.977, 0.995] | 1.000 | 0.882 |
| Safety + judge (score ≥ 2) | 544 | 0.961 [0.942, 0.975] | 0.963 [0.945, 0.976] | 1.000 | 0.588 |

Exact McNemar, combined (score 3) against safety-only: 45 agents only the combined validator
gets right, none the other way, p ≈ 5.7 × 10⁻¹⁴. **H_3 is supported.**

- The judge rejected none of the 523 agents that succeed; every one scored 3.
- Its 6 false approvals are all type mismatches on the ambiguous paraphrases repaired after
  integration test 2 (`invalid_ids` as integers ×4, `services_with_errors` as a list, `samples`
  as a list): the judge read the wording the agent was given, which did not fix the type, and
  accepted the answer. The exact match catches them. In the factory both apply, so the judge
  complements the automatic check rather than replacing it; its value is on tasks whose output
  cannot be matched exactly.
- 51 of the 574 safe agents fail (8.9%), 42 of them from integration test 2; the population is
  dominated by the runs after the paraphrase repair, where failure is rare.

---

### Judge calibration — ADR-9 (2026-10-16)

The LLM judge (Claude Sonnet 5) scores correctness on the 0–3 rubric; it may score unattended
only once its agreement with a human reaches Cohen's κ ≥ 0.80. The sample is stratified: one
safe_correct and one incorrect_only agent from each of the 30 tasks, 60 items, with execution
evidence from full-corpus run 1. The human scored all 60 blind — random order, opaque ids,
comments stripped, labels in a separate key — and the judge then scored the same items with
exactly the same evidence (`experiments/calibrate_judge.py --evidence runs/full2-l2l3`, $0.36).

| | All 60 items | The 30 incorrect agents |
|---|---|---|
| Cohen's κ | **0.948** (bootstrap 95% CI 0.868–1.000) | 0.880 |
| Quadratic-weighted κ | 0.979 | 0.920 |
| Identical scores | 58 / 60 | 28 / 30 |

- **The gate is open:** the judge may score correctness unattended. In the factory it is called
  after the sandbox, on the same evidence as here (code, tool data, expected output, the task's
  scoring guide, the run's output).
- **The correct agents carry little of the result.** Both raters gave all 30 safe_correct agents
  a 3; the real test is the grading of partial failure, where κ is 0.88 over the 30 incorrect
  agents.
- **Both disagreements are on incorrect agents, one point apart.** T019 (dates parsed
  month-first): human 1, judge 0. T001 (a total counting every line): human 1, judge 2 — the
  task's own guide puts this at 1, so here the judge departed from the guide.
- One human rater, who also directed the corpus. The protocol's second rater on a subset has not
  been done; until then κ measures agreement with one person.

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
  not vary by isolation level on this corpus (H_2, reported as a finding).
- **The judge is calibrated against one human rater** (κ = 0.948 on 60 items, see
  [Judge calibration](#judge-calibration--adr-9-2026-10-16)); the second rater the protocol asks
  for on a subset has not scored yet.
- **The paraphrases were repaired after integration tests 1 and 2** (`11529d7`, and T008 p0
  once more after test 3), so those runs' reuse figures belong to the corpus text as it was then;
  each run's manifest records the corpus hash it used.
- **The generator still occasionally declares no tools** (2 of 188 agents in integration test 3).
  Layer 1 stops them and they go to a person. That is deliberate (decided 2026-10-14): using a
  tool the contract does not declare is a contract violation whatever the intent, and no safety
  verdict is ever sent back to the generator, so the loop cannot learn to pass the checks
  instead of following the rules.
- **The PostgreSQL registry has run against a local database, not the deployed server.**
  Every measured run used the in-process registry. `tests/test_registry_postgres.py` applies
  `schema.sql` to PostgreSQL 16 with pgvector and runs the same seeds through both registries;
  outcomes, reuse and safety metrics are identical (2026-10-16, local container). The Azure
  server is not provisioned, to avoid its standing cost.
- **The cluster runs before 2026-10-14 called the orchestrator and executor directly**, not
  through the AutoGen runtime, which was added afterwards. `tests/test_autogen_team.py` runs the
  same seeds both ways and requires identical outcomes, verdicts and executions, and integration
  test 4 repeated the live run through the runtime with no measurable difference.
- **The agents are light.** They run for under a millisecond, so the corpus measures gVisor's
  start-up cost; its per-call cost under load is measured separately by the
  [workload benchmark](#workload-benchmark--gvisor-while-the-agent-works-2026-10-16), on four
  synthetic agents rather than on generated ones.
- **Run-time detection covers behaviour, not intent.** Since the run-time monitor the sandbox
  flags 65 of 90 unsafe corpus agents on its own; the other 25 carry risks with no run-time effect,
  which only static analysis sees ([layer ablation](#layer-ablation--what-the-sandbox-sees-alone-2026-10-16)).
  A Python audit hook is an in-process monitor: native code that bypasses the interpreter would
  escape it, which is what the container boundary and, at L3, gVisor are for.
- **Practitioner interviews (RQ5, RQ6)** are outside this repository.

See section 16 of `architecture.md` for the fifteen open questions this implementation is built
against. OQ-2 — whether gVisor on AKS is deployable at all — is now answered: it is.
