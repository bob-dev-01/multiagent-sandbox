# Interview guide (English)

Duration 45–60 min. Questions in **bold** are core and asked in every session; the others are
asked if time allows. Follow-up prompts are in italics.

## Opening (2 min)

Thank you. This study is about automatically generated software agents in enterprises and how to
decide, before one runs, whether it is safe and correct. There are no right answers; I am
interested in your experience. You can skip any question or stop at any time. Is it still all
right to record?

## 1. Role and context (5 min)

1. **Describe your role and how it touches AI agent systems in your organisation.**
2. Have you worked with systems that generate or deploy new capabilities — agents, scripts,
   microservices — at run time? *What was the context? Who approved them?*

## 2. Threat perception (10 min)

3. **If an LLM wrote a new agent to handle a task no existing agent covers, what would concern
   you most before it ran?**
4. **How does your organisation govern generated or automated code today? Is there a gate?**
5. Have you seen generated or automated code reach production in an unsafe or incorrect state?
   *What happened, and what caught it?*

## 3. Validation and sandbox feasibility (15 min)

*Show the results summary (below). Walk through the three layers in one minute.*

6. **Which of these layers does your organisation apply, plan to apply, or consider
   impractical? Why?**
7. **A validation in a container took about 5 seconds here, mostly starting the container; a
   lighter in-process sandbox took 47 milliseconds. What validation time is acceptable before a
   new agent may run?** *Does it differ for a reused agent?*
8. **gVisor added 3% to validation time for light agents, but made file-heavy work up to seven
   times slower. Would you route agents to sandboxes by risk? What would decide the risk tier?**
9. **What would it take for you to deploy a generated agent with no human review?**
   *Is an agreement of κ = 0.95 between an AI judge and a human reviewer enough? What else?*

## 4. Governance and compliance (10 min)

10. **Which policies apply to agents that use enterprise tools and data? Are agents treated like
    human users, service accounts, or something else?**
11. **Which audit or explainability requirements would shape what a validation pipeline must
    record?** *Here every decision is logged with its evidence. Is that enough? Too much?*
12. **What should a person do in this loop: approve every new agent, only high-risk ones, or
    only failures?** *In this system, any safety finding goes to a person and is never sent back
    to the generator. Is that right?*

## 5. Metrics and evaluation (10 min)

13. **What would you need to see from a validation pipeline to trust it in production?**
14. **This study measures the metrics on the summary. Which matter to you? Which would you drop,
    and what is missing?**
15. **How would you define a catastrophic failure of a system like this in your context?**

## Closing (3 min)

Is there anything about governing generated agents that we have not discussed? May I send you a
one-page summary of your points within two days, so you can correct anything I got wrong?

---

## Results summary to show (one page)

**The system.** When no registered agent fits a task, an LLM (Claude Opus 5) writes a small
Python agent with a contract: the tools it may use, its resource limits, its evidence. Before it
runs, three layers check it: static analysis of the code, a policy check of the contract, and
execution in a sandbox — a subprocess with a system-call filter (L1), a container (L2), or a
container under gVisor, a user-space kernel (L3). Unsafe agents go to a person; incorrect ones are
regenerated up to three times; approved agents are registered and reused for similar tasks.

**What was measured** (30 enterprise tasks, 300 labelled agents, plus about 600 agents written by
the model):

| Metric | Result |
|---|---|
| Unsafe agents deployed (UDR) | 0 of 810 test runs; 0 among model-written agents |
| Safe, correct agents wrongly rejected (FPR) | 0 |
| Tasks completed by the full loop with a real model | 98–99% on original wording, 98–100% on reworded tasks |
| Agents reused for a reworded task | about 50% |
| Validation time per agent | 47 ms (L1), about 5.3 s (L2), about 5.5 s (L3) |
| Cost of gVisor | +3% for light agents; up to ×7 for file-heavy work |
| AI judge vs human reviewer | κ = 0.95 |
| Sandbox alone vs static analysis alone | 72% vs 100% of unsafe test agents; together 100% |
