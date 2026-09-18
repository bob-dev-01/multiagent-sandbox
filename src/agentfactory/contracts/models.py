"""Agent Contract and Validation Verdict — the two versioned interfaces of the system.

These schemas are frozen: the Agent Generator writes `AgentSpec`, the Validation
Pipeline reads it and writes `ValidationResult`, and nothing else crosses a
component boundary. Changing either shape is a breaking change and must bump
`SCHEMA_VERSION`, because every archived run is replayed against the schema it
was produced under.

Reference: architecture.md sections 5.2 and 6.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

SCHEMA_VERSION = "1.1.0"

# Hard ceilings. A contract may declare less than these; it may never declare more.
# The sandbox is configured *from* the contract (see OQ-15 in architecture.md), so
# these bounds are what stops a generated agent from asking for the whole host.
MAX_CPU_CORES = 2
MAX_MEMORY_MB = 512
MAX_DURATION_SEC = 30


class Verdict(StrEnum):
    """The four terminal outcomes of validation.

    The split between UNSAFE and INCORRECT is what drives routing: a correctness
    failure may be retried automatically, a safety failure never is.
    """

    PASS = "PASS"
    FAIL_UNSAFE = "FAIL_UNSAFE"
    FAIL_INCORRECT = "FAIL_INCORRECT"
    FAIL_BOTH = "FAIL_BOTH"

    @property
    def is_pass(self) -> bool:
        return self is Verdict.PASS

    @property
    def is_safety_failure(self) -> bool:
        """Safety failures exit to a human; they are never auto-regenerated."""
        return self in (Verdict.FAIL_UNSAFE, Verdict.FAIL_BOTH)

    @property
    def is_regenerable(self) -> bool:
        return self is Verdict.FAIL_INCORRECT


class Severity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ValidationLayer(StrEnum):
    STATIC = "static"
    POLICY = "policy"
    SANDBOX = "sandbox"


class IsolationLevel(StrEnum):
    """The sandbox isolation spectrum (architecture.md section 8)."""

    L1 = "L1"  # subprocess + seccomp-bpf, shares the host kernel
    L2 = "L2"  # ephemeral container (ACI / Docker), namespace isolation
    L3 = "L3"  # AKS + gVisor RuntimeClass, host kernel unreachable

    @property
    def rank(self) -> int:
        return {"L1": 1, "L2": 2, "L3": 3}[self.value]


class ResourceLimits(BaseModel):
    """Execution Environment Constraints — contract clause 3.

    These values configure the sandbox directly. They are not advisory: the
    runner translates them into subprocess rlimits, container resource requests,
    or pod limits depending on the isolation level.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_cores: Annotated[float, Field(gt=0, le=MAX_CPU_CORES)] = 1.0
    memory_mb: Annotated[int, Field(gt=0, le=MAX_MEMORY_MB)] = 256
    network_access: bool = False
    max_duration_sec: Annotated[int, Field(gt=0, le=MAX_DURATION_SEC)] = 30


class SecurityPolicy(BaseModel):
    """Security Policies — contract clause 5."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    forbidden_syscalls: tuple[str, ...] = ("execve", "socket")
    forbidden_imports: tuple[str, ...] = ("os.system", "subprocess")
    require_human_approval_for: tuple[str, ...] = ("delete", "modify_record")

    @field_validator("forbidden_imports", "forbidden_syscalls", "require_human_approval_for")
    @classmethod
    def _normalise(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # Deduplicate while preserving order, and drop blanks — generated
        # manifests routinely contain both.
        seen: dict[str, None] = {}
        for item in v:
            cleaned = item.strip()
            if cleaned:
                seen.setdefault(cleaned, None)
        return tuple(seen)


class AgentContract(BaseModel):
    """What a generated agent commits to before it is allowed to run.

    The five clauses map one-to-one onto the enforcement points:
      intent               -> Layer 3 correctness rubric
      authorized_tools     -> Layer 2 policy check
      resource_limits      -> Layer 3 sandbox runtime
      evidence_requirements-> Layer 3 trace inspection
      security_policy      -> Layers 1 and 3
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_spec: Annotated[str, Field(min_length=1, max_length=8000)]
    authorized_tools: tuple[str, ...] = ()
    resource_limits: ResourceLimits = ResourceLimits()
    evidence_requirements: tuple[str, ...] = ("output_json", "step_log")
    security_policy: SecurityPolicy = SecurityPolicy()

    @field_validator("authorized_tools")
    @classmethod
    def _tools_are_allowlist(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # An empty allowlist is legal and means "no tools" — deny by default.
        seen: dict[str, None] = {}
        for tool in v:
            cleaned = tool.strip()
            if cleaned:
                seen.setdefault(cleaned, None)
        return tuple(seen)


class AgentSpec(BaseModel):
    """The artifact the Generator produces and the Validation Pipeline consumes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    agent_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    task_id: str
    contract: AgentContract
    generated_code_b64: str = Field(repr=False)
    generator_model: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("generated_code_b64")
    @classmethod
    def _must_decode(cls, v: str) -> str:
        try:
            decoded = base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("generated_code_b64 is not valid base64") from exc
        if not decoded.strip():
            raise ValueError("generated code is empty")
        try:
            decoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("generated code is not valid UTF-8") from exc
        return v

    @property
    def source(self) -> str:
        """The agent's Python source, decoded."""
        return base64.b64decode(self.generated_code_b64).decode("utf-8")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def code_sha256(self) -> str:
        """Identity of the code under validation.

        Every log record carries this so a metric can always be traced back to
        the exact bytes that produced it.
        """
        return hashlib.sha256(base64.b64decode(self.generated_code_b64)).hexdigest()

    @classmethod
    def from_source(
        cls,
        *,
        task_id: str,
        source: str,
        contract: AgentContract,
        generator_model: str | None = None,
        agent_id: uuid.UUID | None = None,
    ) -> AgentSpec:
        return cls(
            task_id=task_id,
            contract=contract,
            generated_code_b64=base64.b64encode(source.encode("utf-8")).decode("ascii"),
            generator_model=generator_model,
            **({"agent_id": agent_id} if agent_id else {}),
        )


class Finding(BaseModel):
    """A single rule violation reported by a validation layer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: ValidationLayer
    rule_id: str
    severity: Severity
    message: str
    line_number: int | None = None

    def __str__(self) -> str:
        where = f":{self.line_number}" if self.line_number is not None else ""
        return f"[{self.severity}] {self.rule_id}{where} {self.message}"


class ExecutionTrace(BaseModel):
    """What the sandbox observed — the only evidence of actual behaviour."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    isolation_level: IsolationLevel
    exit_code: int | None = None
    timed_out: bool = False
    syscalls_blocked: tuple[str, ...] = ()
    network_attempts: tuple[str, ...] = ()
    fs_violations: tuple[str, ...] = ()
    unauthorized_tool_calls: tuple[str, ...] = ()
    output_match_score: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    # Total time the runner spent, including provisioning. This is the number
    # RQ2 compares across isolation levels.
    wall_time_ms: Annotated[float, Field(ge=0)] = 0.0
    # Time the agent itself ran, measured inside the sandbox. Separate from
    # wall_time_ms because provisioning dominates it — an ACI container group
    # takes ~45 s to appear and the agent then runs for milliseconds.
    agent_elapsed_ms: Annotated[float, Field(ge=0)] | None = None
    peak_memory_mb: Annotated[float, Field(ge=0)] = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""
    # Whether a kernel syscall filter was actually in force. Only meaningful
    # for L1, where the filter is optional — an L1 run made without one is a
    # valid run of the rlimits configuration, but it is not the configuration
    # RQ2 describes, and the difference must be visible in the data rather than
    # inferred from a log line.
    seccomp_active: bool | None = None

    @property
    def has_safety_violation(self) -> bool:
        return bool(
            self.syscalls_blocked
            or self.network_attempts
            or self.fs_violations
            or self.unauthorized_tool_calls
        )

    def exceeds(self, limits: ResourceLimits) -> tuple[str, ...]:
        """Resource-limit overruns, as contract violations.

        This closes OQ-15: the contract declares limits, and here we check what
        was actually consumed against what was declared, rather than trusting
        that a fixed container ceiling happened to match.
        """
        overruns: list[str] = []
        if self.peak_memory_mb > limits.memory_mb:
            overruns.append(
                f"memory {self.peak_memory_mb:.1f}MB > declared {limits.memory_mb}MB"
            )
        # Charge the agent for its own runtime, not for the platform's.
        # Using wall_time_ms here made every L2 run a contract violation: an ACI
        # container group takes ~45 s to provision against a declared 30 s
        # limit, so the agent was blamed for time it never got to use, and every
        # safe agent came back FAIL_UNSAFE. Fall back to wall time only when the
        # harness reported nothing — a run that produced no report is one where
        # the timeout is the only signal available.
        elapsed = self.agent_elapsed_ms if self.agent_elapsed_ms is not None else self.wall_time_ms
        if elapsed > limits.max_duration_sec * 1000:
            overruns.append(
                f"agent runtime {elapsed / 1000:.1f}s > declared {limits.max_duration_sec}s"
            )
        return tuple(overruns)


class LayerReport(BaseModel):
    """Per-layer outcome, kept separately so latency can be attributed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: ValidationLayer
    passed: bool
    findings: tuple[Finding, ...] = ()
    latency_ms: Annotated[float, Field(ge=0)] = 0.0
    score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    trace: ExecutionTrace | None = None

    @property
    def high_severity(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.HIGH)


class ValidationResult(BaseModel):
    """The Validation Pipeline's output — interface 2 of 2."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    agent_id: uuid.UUID
    task_id: str
    code_sha256: str
    verdict: Verdict
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    isolation_level: IsolationLevel
    layer_reports: tuple[LayerReport, ...]
    correctness_score: Annotated[float, Field(ge=0.0, le=3.0)] | None = None
    correctness_source: Literal["human", "llm_judge", "rubric_auto"] | None = None
    total_latency_ms: Annotated[float, Field(ge=0)] = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def deployable(self) -> bool:
        return self.verdict.is_pass

    def report_for(self, layer: ValidationLayer) -> LayerReport | None:
        return next((r for r in self.layer_reports if r.layer is layer), None)

    @property
    def all_findings(self) -> tuple[Finding, ...]:
        return tuple(f for report in self.layer_reports for f in report.findings)
