"""What a correctness rater sees — the same for the human and for the LLM judge.

Calibration compares two raters on the same items, so they must be shown the
same evidence. This module is the single definition of it, used by the judge's
prompt and by the human scoring sheet alike.

Two things are deliberately left out.

**Comments.** The corpus variants carry comments that name their injected fault
(`# Injected error: treats DD/MM as MM/DD.`). Shown to a rater, they hand over
the answer, and a kappa computed on them would measure reading, not judging.
All comments are removed from every agent, not only the incriminating ones, so
that the absence of comments cannot itself give a variant away.

**Safety evidence and the automatic match.** The rubric scores correctness
only. Network attempts, blocked calls and the sandbox's own output-match score
are not shown: the first would bias the rater against an agent for something
the rubric says to ignore, the second would anchor the score to the automatic
check that the graded score exists to go beyond.
"""

from __future__ import annotations

import io
import json
import re
import tokenize
from typing import Any

from agentfactory.contracts import ExecutionTrace
from agentfactory.validation.sandbox.base import HARNESS_SENTINEL

NOT_EXECUTED = "The agent was never executed."


def strip_comments(source: str) -> str:
    """The source with every comment removed and no other change to the code.

    Falls back to the source unchanged if it does not tokenize — a rater should
    see code that does not parse exactly as it is.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    lines = source.splitlines(keepends=True)
    # Blank out comment spans right to left so earlier columns stay valid.
    for tok in reversed(tokens):
        if tok.type != tokenize.COMMENT:
            continue
        row = tok.start[0] - 1
        line = lines[row]
        lines[row] = line[: tok.start[1]].rstrip() + ("\n" if line.endswith("\n") else "")
    kept: list[str] = []
    for original, line in zip(source.splitlines(keepends=True), lines, strict=True):
        # A line that held only a comment goes entirely; a blank line stays.
        if line.strip() == "" and original.strip() != "":
            continue
        kept.append(line)
    return "".join(kept)


def format_task_rubric(rubric: str) -> str:
    """A task's 0-3 scoring guide, one score per line.

    The corpus stores it as folded YAML, which keeps stray indentation inside
    entries; both raters get the same normalised text.
    """
    flat = " ".join(rubric.split())
    return re.sub(r"\s+(?=[0-3] = )", "\n", flat).strip()


def harness_report(trace: ExecutionTrace) -> dict[str, Any] | None:
    """The harness's own report from a trace, or None if it never reported."""
    for line in reversed(trace.stdout_tail.splitlines()):
        if line.startswith(HARNESS_SENTINEL):
            try:
                return json.loads(line[len(HARNESS_SENTINEL) :])
            except json.JSONDecodeError:
                return None
    return None


def execution_evidence(trace: ExecutionTrace | None) -> dict[str, Any] | None:
    """Correctness-relevant facts about one run; None if the agent never ran."""
    if trace is None:
        return None
    report = harness_report(trace) or {}
    output = trace.agent_output if trace.agent_output is not None else report.get("output")
    return {
        "exit_code": trace.exit_code,
        "timed_out": trace.timed_out,
        "output": output,
        "error": report.get("error"),
        # The agent's traceback and anything it printed; the harness writes
        # nothing else there.
        "stderr_tail": trace.stderr_tail.strip()[-1200:],
    }
