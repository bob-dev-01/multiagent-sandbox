# Agent Factory — Sandbox-First Validation Pipeline for LLM-Generated Micro-Agents

Implementation of the architecture in [`architecture.md`](architecture.md): a system that
generates narrowly scoped micro-agents on demand, then decides automatically whether each one
is **safe to deploy** and **correct enough to be useful** before it executes.

Master's research, Solution Architecture and Data Engineering — Bobur Yusupov, IT Park University.

> **Status: deployed, pilot run complete.** All three isolation levels have been exercised
> end to end against the live environment. The full 2,700-run experiment has not been executed
> and the corpus is still 3 tasks of 30, so nothing here is a thesis result — but the latency
> figures below are measurements, not targets.

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
  generator/                     agent generation (Claude Sonnet 5)
  judge/                         correctness judge (Claude Opus 5) behind a calibration gate
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

To use the generator or judge, put your key in `.env` (copy from `.env.example`). It is
gitignored. In the deployed system the key lives in Key Vault and the VM reads it via managed
identity; nothing in this repo ever writes a key to disk.

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
python experiments/run_matrix.py --levels L1 --replicates 3
python experiments/run_matrix.py --levels L1,L2,L3 --replicates 3 --resume
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
corpus distribution, not a model's; the same harness takes the real generator once the API key is
in place. And the warm pass needs the **real embedder** (`pip install -e ".[embeddings]"`); with
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

## Pilot results

One replicate, one test input, 30 labelled agents per level, run against the live environment.

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
- **It is a small fraction of the pod lifecycle.** End to end it is lost in the noise of
  scheduling and container creation: the wall-clock difference is not significant, and both levels
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

- **The corpus has 3 tasks, not 30.** One per domain, fully worked with all ten labelled variants
  (30 agents). Tasks 4–30 are research content — the task design is the researcher's judgment
  call, and inventing 27 more would be guesswork dressed as data. The loader enforces the label
  distribution, so adding a task is mechanical.
- **No validation batch has been run.** The environment exists and L3 is verified; the experiment
  itself has not been executed.
- **L1 and L2 runners have not been exercised end-to-end.** Unit-tested via the harness; L1 needs
  Linux (run it on the orchestrator VM) and L2 needs Docker or ACI.
- **The registry schema has not been applied** to the deployed PostgreSQL server. The
  integration harness uses the in-process registry, which enforces the same rules.
- **The integration test has not been run on the cluster**, and its generator is a corpus replay
  until the API key is configured. The Task Executor is not wrapped in AutoGen; it is plain Python
  behind the same interfaces.
- **Practitioner interviews (RQ5, RQ6)** are outside this repository.

See section 16 of `architecture.md` for the fifteen open questions this implementation is built
against. OQ-2 — whether gVisor on AKS is deployable at all — is now answered: it is.
