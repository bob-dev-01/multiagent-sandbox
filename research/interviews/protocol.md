# Practitioner interviews — protocol (RQ5, RQ6)

Fixed before any interview takes place, as the research plan requires (Task 7, sections 4.3.2,
5.7). Changes after the first session are recorded at the end of this file with a date and reason.

## Purpose

RQ5 asks which organisational and architectural constraints shape the feasible deployment of
dynamic agent generation in an enterprise; RQ6 asks which metrics and evaluation protocols best
capture the safety, correctness and performance of generated agents. Neither can be answered by
the experiment. Practitioners are asked to react to the system and to its measured results.

## Participants

Four practitioners, purposive sampling for role diversity, plus one pilot participant outside
the sample:

| Code | Role | Experience sought |
|---|---|---|
| P1 | Platform or solution architect | designing multi-agent or agentic AI systems in an enterprise |
| P2 | DevSecOps or security engineer | governing code execution and sandbox environments |
| P3 | ML engineer or AI product manager | deploying and governing LLM systems |
| P4 | IT operations lead or SRE | incident-response automation, MTTR |
| Pilot | any of the above | validates the guide's clarity and timing; not analysed |

Inclusion: at least one year of direct experience with LLM-based or agentic AI in a production or
near-production enterprise context; able to discuss deployment constraints candidly; English or
Russian. Recruitment: ITPU's professional network, LinkedIn, OWASP / Cloud Security Alliance
chapters, local DevOps meetups.

## Each session

1. Send the information sheet and consent form at least two days before.
2. 45–60 minutes, remote or in person, in the participant's choice of English or Russian.
3. Confirm consent orally at the start of the recording.
4. Follow the interview guide; show the one-page results summary (section 3 of the guide)
   when the guide says so.
5. Record audio locally only. Transcribe with Whisper on the researcher's laptop; the audio never
   leaves the device.
6. De-identify the transcript the same day: replace names of people, organisations, products
   and places with role codes; then delete the audio.
7. Send the participant a one-page summary of their points within 48 hours for member checking.
8. Write a short reflexivity note: what surprised the researcher, what they may have led.

## Analysis

Thematic analysis (Braun and Clarke, 2006), hybrid deductive–inductive, three passes (open,
axial, selective), with the codebook in `codebook.md`. A second coder double-codes 25% of
transcript paragraphs; target Cohen's κ ≥ 0.70. Results are integrated with the experiment in the
joint display (`joint_display.md`): measured metrics against practitioner themes, annotated as
convergence, expansion or discrepancy.

## Data handling

Consent forms and de-identified transcripts are stored outside the public repository. Only
de-identified quotations, attributed to a role code, appear in the thesis. A participant may
withdraw until their transcript is de-identified and merged into the analysis, without giving a
reason.

## Changes after the first session

(none yet)
