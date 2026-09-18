# Agent Factory — Sandbox-First Validation Pipeline for LLM-Generated Micro-Agents

Implementation of the architecture in [`architecture.md`](architecture.md): a system that
generates narrowly scoped micro-agents on demand, then decides automatically whether each one
is **safe to deploy** and **correct enough to be useful** before it executes.

Master's research, Solution Architecture and Data Engineering — Bobur Yusupov, IT Park University.

> **Status: pre-deployment.** The code runs and is tested; no infrastructure has been created and
> no experiment has been run. Every number in the docs is a design target, not a measurement.

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
| **L2** | Ephemeral container, one per validation | Docker locally, ACI in Azure |
| **L3** | Pod under a gVisor RuntimeClass | AKS sandbox node pool |

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
  orchestrator/                  reuse-vs-generate, verdict routing, retries
  metrics/                       UDR, FNR, FPR, latency, Wilson intervals
  telemetry/                     JSONL event log with schema-on-write validation
  corpus.py                      ground-truth corpus loader
corpus/tasks/                    labelled agent corpus, one file per task
experiments/run_matrix.py        experiment harness with resume
infra/                           Bicep templates + gVisor DaemonSet
ops/afctl.py                     resource control panel
tests/                           67 tests, no external services required
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
python -m ops.afctl down      # deallocate VM, scale AKS to 0, stop PostgreSQL
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

## What is not done yet

Honest list, so nobody discovers these the hard way:

- **The corpus has 3 tasks, not 30.** One per domain, fully worked with all ten labelled variants
  (30 agents). Tasks 4–30 are research content — the task design is the researcher's judgment
  call, and inventing 27 more would be guesswork dressed as data. The loader enforces the label
  distribution, so adding a task is mechanical.
- **No infrastructure exists.** The Bicep compiles clean; nothing has been deployed.
- **gVisor on AKS is unverified.** It needs node customization via a privileged DaemonSet, and it
  is not Azure's supported sandboxing path (that is Kata). `preflight()` fails loudly rather than
  silently downgrading. Validate it on an empty cluster before building anything on top.
- **L1 and L2 runners have not been exercised end-to-end.** Unit-tested via the harness; the real
  paths need Linux and Docker respectively.
- **Practitioner interviews (RQ5, RQ6)** are outside this repository.

See section 16 of `architecture.md` for the fifteen open questions this implementation is built
against, including the ones it resolves and the ones it does not.
