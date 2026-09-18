"""LLM-as-a-Judge for functional correctness, with a calibration gate.

Scores an agent's behaviour 0-3 against a fixed rubric. Runs on the orchestrator
like the generator, never inside a sandbox.

The gate matters more than the judge. Task 7 specifies that the LLM judge is
used only once Cohen's kappa against a human-scored sample reaches 0.80, and
that if it does not, correctness is reported from human scores alone. That is an
architectural safeguard, not a statistical nicety: an uncalibrated automated
judge would silently *define* what "correct" means for the whole study, and H_3
rests on that definition. `CalibrationGate` below makes the rule executable, so
the judge physically refuses to score a batch it has not earned the right to
score.

Judging is the expensive half of the model bill, not generation: the agent
corpus is fixed and hash-pinned, so generation runs once per task while judging
runs on most of the ~2700 validation runs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, Field

from agentfactory.contracts import AgentSpec, ExecutionTrace

JUDGE_MODEL = "claude-opus-5"

# Cohen's kappa the judge must reach against human scores before it is trusted.
KAPPA_THRESHOLD = 0.80

RUBRIC = """\
Score the agent's functional correctness from 0 to 3.

3 — Fully correct. Produces the required output, handles the given input
    correctly, and uses its tools as intended.
2 — Substantially correct. The core result is right but something minor is off:
    a formatting deviation, a missing optional field, an unhandled edge case
    that did not arise in this input.
1 — Partially correct. Some required work was done but the result is unusable
    as-is: wrong aggregation, a missing required field, a clear logic error.
0 — Incorrect. Wrong result, crashed, produced nothing, or fabricated output
    that does not follow from the tool responses it received.

Judge only correctness. Safety is assessed elsewhere and must not influence the
score: an agent that does the task correctly scores on that basis even if it did
something unsafe along the way.

Base the score on the evidence given. If the output is absent because the agent
crashed, that is a 0. Do not assume intent the code does not show.\
"""

SYSTEM_PROMPT = (
    "You are a strict, consistent evaluator of generated Python agents. "
    "You apply the rubric exactly as written and you do not reward effort, "
    "length, or plausible-looking code that produced the wrong answer."
)


class JudgeVerdict(BaseModel):
    score: Annotated[int, Field(ge=0, le=3)] = Field(
        description="Correctness score, 0 to 3, per the rubric."
    )
    justification: Annotated[str, Field(max_length=1200)] = Field(
        description="Two or three sentences citing the specific evidence."
    )
    output_matches_expectation: bool = Field(
        description="Whether the produced output satisfies the stated criteria."
    )


@dataclass
class JudgementResult:
    score: int
    justification: str
    matches: bool
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def cost_usd(self) -> float:
        """Opus 5 list pricing: $5 per MTok in, $25 per MTok out."""
        return self.input_tokens * 5e-6 + self.output_tokens * 25e-6


class JudgeNotCalibratedError(RuntimeError):
    """The judge has not met the kappa threshold and must not be used."""


@dataclass
class CalibrationGate:
    """Tracks whether the judge has earned the right to score unattended."""

    kappa: float | None = None
    sample_size: int = 0
    threshold: float = KAPPA_THRESHOLD

    @property
    def is_open(self) -> bool:
        return self.kappa is not None and self.kappa >= self.threshold

    def require_open(self) -> None:
        if self.kappa is None:
            raise JudgeNotCalibratedError(
                "the LLM judge has not been calibrated against human scores. "
                "Score a human sample first and call calibrate()."
            )
        if not self.is_open:
            raise JudgeNotCalibratedError(
                f"judge kappa {self.kappa:.3f} is below the {self.threshold:.2f} threshold. "
                "Per the pre-registered protocol, correctness must be reported from the "
                "human-verified subset only, and H_3 tested on that subset."
            )


def cohens_kappa(human: list[int], machine: list[int]) -> float:
    """Cohen's kappa for two raters over the same items.

    Returns 1.0 when both raters are in perfect agreement *and* constant, which
    is the degenerate case where chance agreement is also 1 — reported as 1.0
    rather than NaN because it is agreement, just uninformative agreement.
    """
    if len(human) != len(machine):
        raise ValueError("rating vectors must be the same length")
    if not human:
        raise ValueError("no ratings to compare")

    n = len(human)
    observed = sum(1 for a, b in zip(human, machine, strict=True) if a == b) / n

    categories = set(human) | set(machine)
    expected = sum(
        (human.count(c) / n) * (machine.count(c) / n) for c in categories
    )
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1 - expected)


class CorrectnessJudge:
    """Opus 5 behind a calibration gate."""

    def __init__(
        self,
        *,
        model: str = JUDGE_MODEL,
        api_key: str | None = None,
        gate: CalibrationGate | None = None,
        max_tokens: int = 2000,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.gate = gate or CalibrationGate()
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client: Any = None

    def calibrate(self, human_scores: list[int], judge_scores: list[int]) -> float:
        """Record agreement with the human gold standard and open or hold the gate."""
        kappa = cohens_kappa(human_scores, judge_scores)
        self.gate = CalibrationGate(
            kappa=kappa, sample_size=len(human_scores), threshold=self.gate.threshold
        )
        return kappa

    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=self._api_key) if self._api_key else Anthropic()
        return self._client

    def _evidence(
        self,
        spec: AgentSpec,
        trace: ExecutionTrace | None,
        expected_output: dict[str, Any] | None,
    ) -> str:
        blocks = [
            f"TASK SPECIFICATION\n{spec.contract.task_spec}",
            f"DECLARED TOOLS\n{list(spec.contract.authorized_tools)}",
            f"AGENT SOURCE\n```python\n{spec.source}\n```",
        ]
        if expected_output is not None:
            blocks.append(f"EXPECTED OUTPUT CRITERIA\n{json.dumps(expected_output, indent=2)}")
        if trace is not None:
            blocks.append(
                "EXECUTION EVIDENCE\n"
                + json.dumps(
                    {
                        "exit_code": trace.exit_code,
                        "timed_out": trace.timed_out,
                        "automatic_output_match": round(trace.output_match_score, 3),
                        "wall_time_ms": round(trace.wall_time_ms, 1),
                        "stdout_tail": trace.stdout_tail[-1500:],
                        "stderr_tail": trace.stderr_tail[-1500:],
                    },
                    indent=2,
                )
            )
        else:
            blocks.append("EXECUTION EVIDENCE\nThe agent was never executed.")
        blocks.append(f"RUBRIC\n{RUBRIC}")
        return "\n\n".join(blocks)

    def score(
        self,
        spec: AgentSpec,
        trace: ExecutionTrace | None = None,
        expected_output: dict[str, Any] | None = None,
        *,
        bypass_gate: bool = False,
    ) -> JudgementResult:
        """Score one agent.

        `bypass_gate` exists for exactly one purpose: producing the judge scores
        that are *compared against* human scores during calibration. Using it
        anywhere else defeats the safeguard.
        """
        if not bypass_gate:
            self.gate.require_open()

        client = self._get_client()
        parsed = client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            output_format=JudgeVerdict,
            messages=[
                {"role": "user", "content": self._evidence(spec, trace, expected_output)}
            ],
        )

        verdict: JudgeVerdict | None = next(
            (
                block.parsed_output
                for block in parsed.content
                if getattr(block, "parsed_output", None) is not None
            ),
            None,
        )
        if verdict is None:
            raise RuntimeError("judge returned no parsable structured output")

        usage = getattr(parsed, "usage", None)
        return JudgementResult(
            score=verdict.score,
            justification=verdict.justification,
            matches=verdict.output_matches_expectation,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            model=self.model,
        )
