"""Contracts, telemetry, and the judge's calibration gate."""

from __future__ import annotations

import base64
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentfactory.contracts import (
    AgentContract,
    AgentSpec,
    ExecutionTrace,
    IsolationLevel,
    ResourceLimits,
    Verdict,
)
from agentfactory.judge.correctness_judge import (
    CalibrationGate,
    CorrectnessJudge,
    JudgeNotCalibratedError,
    cohens_kappa,
)
from agentfactory.telemetry.events import EventLog, EventType, read_events

# ------------------------------------------------------------------ contracts


def test_code_hash_is_content_addressed(safe_contract: AgentContract) -> None:
    a = AgentSpec.from_source(task_id="T", source="def run(a,b): pass", contract=safe_contract)
    b = AgentSpec.from_source(task_id="T", source="def run(a,b): pass", contract=safe_contract)
    c = AgentSpec.from_source(task_id="T", source="def run(a,b): return 1", contract=safe_contract)
    assert a.code_sha256 == b.code_sha256
    assert a.code_sha256 != c.code_sha256


def test_invalid_base64_is_rejected(safe_contract: AgentContract) -> None:
    with pytest.raises(ValueError, match="base64"):
        AgentSpec(task_id="T", contract=safe_contract, generated_code_b64="!!!not base64!!!")


def test_empty_code_is_rejected(safe_contract: AgentContract) -> None:
    with pytest.raises(ValueError, match="empty"):
        AgentSpec(
            task_id="T",
            contract=safe_contract,
            generated_code_b64=base64.b64encode(b"   ").decode(),
        )


def test_contract_is_immutable(safe_contract: AgentContract) -> None:
    with pytest.raises(ValidationError):
        safe_contract.task_spec = "something else"  # type: ignore[misc]


def test_resource_limits_reject_values_above_the_ceiling() -> None:
    with pytest.raises(ValueError):
        ResourceLimits(cpu_cores=99)
    with pytest.raises(ValueError):
        ResourceLimits(max_duration_sec=600)


def test_verdict_routing_properties() -> None:
    assert Verdict.PASS.is_pass
    assert Verdict.FAIL_UNSAFE.is_safety_failure
    assert Verdict.FAIL_BOTH.is_safety_failure
    assert Verdict.FAIL_INCORRECT.is_regenerable
    assert not Verdict.FAIL_UNSAFE.is_regenerable


def test_trace_detects_resource_overrun() -> None:
    """OQ-15: declared limits are checked against what was actually consumed."""
    limits = ResourceLimits(memory_mb=256, max_duration_sec=10)
    trace = ExecutionTrace(
        isolation_level=IsolationLevel.L2, peak_memory_mb=400.0, wall_time_ms=15_000
    )
    overruns = trace.exceeds(limits)
    assert len(overruns) == 2
    assert any("memory" in o for o in overruns)
    assert any("duration" in o for o in overruns)


def test_trace_within_limits_reports_nothing() -> None:
    limits = ResourceLimits(memory_mb=256, max_duration_sec=30)
    trace = ExecutionTrace(
        isolation_level=IsolationLevel.L1, peak_memory_mb=120.0, wall_time_ms=900
    )
    assert trace.exceeds(limits) == ()


# ----------------------------------------------------------------- telemetry


def test_event_log_roundtrip(tmp_path: Path) -> None:
    with EventLog(tmp_path, run_id="r1") as log:
        log.emit(EventType.RUN_STARTED, task_id="T1")
        log.emit(EventType.VERDICT_EMITTED, task_id="T1", verdict=Verdict.PASS, latency_ms=12.5)
        assert log.records_written == 2

    events = list(read_events(tmp_path / "r1.jsonl"))
    assert [e.event_type for e in events] == [EventType.RUN_STARTED, EventType.VERDICT_EMITTED]
    assert events[1].verdict is Verdict.PASS
    assert all(e.run_id == "r1" for e in events)


def test_malformed_event_is_rejected_at_write_time(tmp_path: Path) -> None:
    """Schema-on-write: a bad record never reaches the file."""
    with EventLog(tmp_path, run_id="r2") as log:
        with pytest.raises(ValidationError):
            log.emit(EventType.RUN_STARTED, not_a_real_field="boom")
        assert log.records_written == 0


def test_corrupt_line_raises_in_strict_mode(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"nonsense": true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not a valid event"):
        list(read_events(path))


def test_every_event_carries_provenance(tmp_path: Path) -> None:
    with EventLog(tmp_path, run_id="r3") as log:
        event = log.emit(EventType.RUN_STARTED)
    assert event.git_commit
    assert event.schema_version


# --------------------------------------------------------------------- judge


def test_kappa_perfect_agreement() -> None:
    assert cohens_kappa([0, 1, 2, 3], [0, 1, 2, 3]) == pytest.approx(1.0)


def test_kappa_penalises_chance_agreement() -> None:
    human = [3, 3, 3, 0]
    machine = [3, 3, 0, 3]
    k = cohens_kappa(human, machine)
    assert k < 0.5


def test_kappa_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError):
        cohens_kappa([1, 2], [1])


def test_gate_is_closed_before_calibration() -> None:
    judge = CorrectnessJudge(api_key="unused")
    assert not judge.gate.is_open
    with pytest.raises(JudgeNotCalibratedError, match="not been calibrated"):
        judge.gate.require_open()


def test_gate_stays_closed_below_threshold() -> None:
    judge = CorrectnessJudge(api_key="unused")
    judge.calibrate([3, 3, 3, 0, 1], [3, 0, 1, 3, 3])
    assert not judge.gate.is_open
    with pytest.raises(JudgeNotCalibratedError, match="below the"):
        judge.gate.require_open()


def test_gate_opens_on_strong_agreement() -> None:
    judge = CorrectnessJudge(api_key="unused")
    human = [3, 3, 2, 2, 1, 0, 3, 1, 0, 2]
    kappa = judge.calibrate(human, list(human))
    assert kappa == pytest.approx(1.0)
    assert judge.gate.is_open
    judge.gate.require_open()  # must not raise


def test_gate_message_names_the_fallback() -> None:
    """The error should tell the reader what the protocol says to do instead."""
    gate = CalibrationGate(kappa=0.5, sample_size=20)
    with pytest.raises(JudgeNotCalibratedError, match="human-verified subset"):
        gate.require_open()


def test_nan_is_not_silently_treated_as_zero() -> None:
    from agentfactory.metrics.core import combined_score

    assert math.isnan(combined_score(math.nan, 2.0))
