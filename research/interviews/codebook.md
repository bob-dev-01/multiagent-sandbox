# Codebook (pre-specified)

Deductive codes from the research plan's five constraint dimensions (Task 7, section 5.7) and
the metrics question (RQ6). Inductive codes are added under `X` during open coding, with the date
and the first excerpt that prompted them. A paragraph may carry several codes.

| Code | Name | Definition | Include | Exclude |
|---|---|---|---|---|
| G1 | Policy on generated code | Rules on deploying LLM-generated or automated code without human review | approval rules, bans, review boards | general AI ethics statements |
| G2 | Audit and logging | Audit trails, evidence, explainability a pipeline must produce | log retention, traceability, regulator demands | operational monitoring for uptime |
| G3 | Latency tolerance | Validation time acceptable before an agent runs | numbers, comparisons to existing gates | latency of the agent's own work |
| G4 | Cost tolerance | Acceptable compute or money per validation | budgets, chargeback, cost-vs-risk trade-offs | licence cost of the LLM itself, unless tied to validation |
| G5 | Human in the loop | When a person must approve, review or be informed | approval tiers, escalation, on-call | user experience of non-technical users |
| R1 | Risk tiering | How tasks or agents are classified for isolation or review | data sensitivity, write access, blast radius | — |
| T1 | Trust in automated validation | What would make an automated verdict trustworthy | track record, agreement figures, audits | trust in the LLM's general ability |
| T2 | Threat perception | Concerns about generated agents | exfiltration, privilege, wrong output, prompt injection | concerns about AI generally |
| M1 | Metrics that matter | Metrics practitioners would require or value | UDR, FPR, TSR, MTTR, cost | — |
| M2 | Missing or rejected metrics | Metrics they would add, drop or redefine | proposals, objections | — |
| M3 | Catastrophic failure | What counts as catastrophic in their context | definitions, examples | — |
| C1 | Current practice | What their organisation does today | existing gates, tools | aspirations (code under G or R) |
| X | Inductive | Emergent themes not fitting the above | | |

## Reliability

25% of transcript paragraphs, chosen at random across all four participants, are coded
independently by a second coder using this table only. Cohen's κ is computed over code presence
per paragraph; target κ ≥ 0.70. Disagreements are discussed and the codebook definitions, not the
first coder's codes, are revised if needed, with the change dated below.

## Changes

(none yet)
