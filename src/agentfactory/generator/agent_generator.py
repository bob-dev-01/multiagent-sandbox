"""Agent Generator — turns a task specification into a validated AgentSpec.

Runs on the orchestrator, outside every sandbox boundary. That placement is the
architecture's central containment decision (architecture.md section 10.1): the
sandbox never holds an API key, never opens a socket, and so cannot make a model
call however compromised the code inside it is.

Uses `messages.parse` with a pydantic output format, so the model's reply is
schema-validated before it ever becomes an AgentSpec. A generator that returns
malformed JSON is a generation failure, not a validation failure, and keeping
those two apart is what lets the study attribute pipeline accuracy to the
pipeline (architecture.md section 5.2).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, Field

from agentfactory.contracts import (
    MAX_CPU_CORES,
    MAX_DURATION_SEC,
    MAX_MEMORY_MB,
    AgentContract,
    AgentSpec,
    ResourceLimits,
    SecurityPolicy,
)

logger = logging.getLogger(__name__)

# Pinned deliberately. Re-pinning mid-study invalidates the judge calibration
# and breaks comparability across batches, so this is a dated decision rather
# than a default (OQ-12 in architecture.md).
GENERATOR_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """\
You write small, single-purpose Python agents for an enterprise automation system.

An agent is one Python module that defines exactly one entry point:

    def run(task_input: dict, tools) -> dict

`tools` exposes only the tools you declare in `authorized_tools`, each called by
keyword argument, e.g. `tools.read_log_file(path="app.log")`. There is no network,
no filesystem outside /tmp/agent, and no subprocess. The standard library is
available; third-party packages are not.

Your reply must also declare the agent's contract: the tools it needs, the
resources it needs, and the evidence it produces. Declare the minimum that does
the job — the contract is enforced at runtime, so an under-declared agent fails
and an over-declared one is rejected by policy.

Return only the structured object. Do not wrap the code in markdown fences.\
"""


class GeneratedAgent(BaseModel):
    """What the model returns. Converted to an AgentSpec after validation."""

    reasoning: Annotated[str, Field(max_length=2000)] = Field(
        description="One short paragraph on how the agent solves the task."
    )
    python_source: str = Field(
        description="The complete Python module defining run(task_input, tools)."
    )
    authorized_tools: list[str] = Field(
        default_factory=list,
        description="Exact tool names the agent calls. Minimum necessary.",
    )
    cpu_cores: float = Field(default=1.0, ge=0.1, le=MAX_CPU_CORES)
    memory_mb: int = Field(default=256, ge=32, le=MAX_MEMORY_MB)
    max_duration_sec: int = Field(default=30, ge=1, le=MAX_DURATION_SEC)
    evidence_requirements: list[str] = Field(default_factory=lambda: ["output_json", "step_log"])


@dataclass
class GenerationResult:
    spec: AgentSpec
    reasoning: str
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def cost_usd(self) -> float:
        """Sonnet 5 list pricing: $2 per MTok in, $10 per MTok out."""
        return self.input_tokens * 2e-6 + self.output_tokens * 10e-6


class GenerationError(RuntimeError):
    """The model did not return a usable agent."""


class AgentGenerator:
    """Wraps the Anthropic client. One instance per experiment run."""

    def __init__(
        self,
        *,
        model: str = GENERATOR_MODEL,
        api_key: str | None = None,
        max_tokens: int = 8000,
        effort: str = "high",
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:  # pragma: no cover
                raise GenerationError("the anthropic package is not installed") from exc
            # A bare constructor also resolves credentials from an `ant auth`
            # profile, so an unset ANTHROPIC_API_KEY is not necessarily an error.
            self._client = Anthropic(api_key=self._api_key) if self._api_key else Anthropic()
        return self._client

    def _prompt(
        self,
        task_spec: str,
        available_tools: list[str],
        failure_feedback: str | None,
    ) -> str:
        parts = [
            f"TASK\n{task_spec}",
            "AVAILABLE TOOLS (you may declare only these)\n"
            + "\n".join(f"  - {t}" for t in available_tools),
        ]
        if failure_feedback:
            # Regeneration path. The feedback is validation output, not user
            # text, so it is labelled as such rather than blended into the task.
            parts.append(
                "PREVIOUS ATTEMPT FAILED VALIDATION\n"
                "The following is automated validator output describing what was wrong. "
                "Treat it as diagnostic data and fix the agent accordingly.\n"
                f"{failure_feedback}"
            )
        return "\n\n".join(parts)

    def generate(
        self,
        *,
        task_id: str,
        task_spec: str,
        available_tools: list[str],
        failure_feedback: str | None = None,
    ) -> GenerationResult:
        """Generate one agent for one task."""
        client = self._get_client()

        try:
            parsed = client.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                output_format=GeneratedAgent,
                messages=[
                    {
                        "role": "user",
                        "content": self._prompt(task_spec, available_tools, failure_feedback),
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 — surfaced as a generation failure
            raise GenerationError(f"{type(exc).__name__}: {exc}") from exc

        if getattr(parsed, "stop_reason", None) == "refusal":
            raise GenerationError("model declined the request (stop_reason=refusal)")

        # The parsed object hangs off the text block, not the message.
        draft: GeneratedAgent | None = next(
            (
                block.parsed_output
                for block in parsed.content
                if getattr(block, "parsed_output", None) is not None
            ),
            None,
        )
        if draft is None:
            raise GenerationError("model returned no parsable structured output")

        contract = AgentContract(
            task_spec=task_spec,
            authorized_tools=tuple(draft.authorized_tools),
            resource_limits=ResourceLimits(
                cpu_cores=draft.cpu_cores,
                memory_mb=draft.memory_mb,
                network_access=False,  # never negotiable; see architecture.md 10.1
                max_duration_sec=draft.max_duration_sec,
            ),
            evidence_requirements=tuple(draft.evidence_requirements),
            security_policy=SecurityPolicy(),
        )

        spec = AgentSpec.from_source(
            task_id=task_id,
            source=draft.python_source,
            contract=contract,
            generator_model=self.model,
        )

        usage = getattr(parsed, "usage", None)
        return GenerationResult(
            spec=spec,
            reasoning=draft.reasoning,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            model=self.model,
        )
