# Joint display (template)

The plan integrates the two strands in one table: each measured result beside what the
practitioners said about it, annotated **convergence** (they agree), **expansion** (the
interviews add something the numbers do not show), or **discrepancy** (they conflict). The
discussion of RQ5 and RQ6 is written from this table, not from either strand alone.

| Measured result | Code(s) | P1 architect | P2 security | P3 ML / PM | P4 ops / SRE | Annotation |
|---|---|---|---|---|---|---|
| UDR 0, FPR 0 on the corpus; 0 unsafe executions among ~600 generated agents | T1, M1 | | | | | |
| Validation 47 ms (L1) / ~5.3 s (L2) / ~5.5 s (L3) | G3 | | | | | |
| gVisor +3% for light agents, up to ×7 for I/O-heavy work | G4, R1 | | | | | |
| Any safety finding goes to a person, never back to the generator | G5 | | | | | |
| AI judge agrees with a human at κ = 0.95; combined validation predicts success 0.99 vs 0.91 | T1, M1 | | | | | |
| Sandbox alone 72%, static analysis 100%, together 100% of unsafe test agents | T2, R1 | | | | | |
| Every decision logged with its evidence (event log, run manifests) | G2 | | | | | |
| Reuse of about half of reworded tasks at τ = 0.85 | M1 | | | | | |
| Metrics used: UDR, FNR, FPR, TSR, ARR, MTTD, CER, κ | M1, M2, M3 | | | | | |
