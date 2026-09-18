"""Tests for the validation layers and the verdict they produce together."""

from __future__ import annotations

import pytest

from agentfactory.contracts import (
    AgentContract,
    ResourceLimits,
    SecurityPolicy,
    Severity,
    ValidationLayer,
    Verdict,
)
from agentfactory.validation.aggregator import StrictnessProfile, aggregate
from agentfactory.validation.layer1_static import analyse
from agentfactory.validation.layer2_policy import EnterprisePolicy, check
from agentfactory.validation.pipeline import PipelineConfig, ValidationPipeline

from .conftest import (
    INCORRECT_AGENT,
    SAFE_AGENT,
    UNSAFE_IMPORT_AGENT,
    UNSAFE_NETWORK_AGENT,
    UNSAFE_OBFUSCATED_AGENT,
    make_spec,
)

# ---------------------------------------------------------------- Layer 1


def test_safe_agent_passes_static(safe_contract: AgentContract) -> None:
    report = analyse(SAFE_AGENT, safe_contract, use_bandit=False)
    assert report.passed
    assert not report.high_severity


def test_forbidden_import_is_caught(safe_contract: AgentContract) -> None:
    report = analyse(UNSAFE_IMPORT_AGENT, safe_contract, use_bandit=False)
    assert not report.passed
    assert any(f.rule_id == "AF101" for f in report.findings)


def test_obfuscated_access_is_caught(safe_contract: AgentContract) -> None:
    """getattr(os, "system") — the case a naive import check misses (OQ-9)."""
    report = analyse(UNSAFE_OBFUSCATED_AGENT, safe_contract, use_bandit=False)
    assert not report.passed
    assert any(f.rule_id == "AF105" for f in report.findings)


def test_from_import_of_forbidden_callable(safe_contract: AgentContract) -> None:
    source = "from os import system\n\ndef run(task_input, tools):\n    system('id')\n"
    report = analyse(source, safe_contract, use_bandit=False)
    assert not report.passed
    assert any(f.rule_id in {"AF103", "AF104"} for f in report.findings)


def test_dynamic_import_is_caught(safe_contract: AgentContract) -> None:
    source = 'def run(task_input, tools):\n    m = __import__("socket")\n    return {}\n'
    report = analyse(source, safe_contract, use_bandit=False)
    assert not report.passed


def test_introspection_escape_is_caught(safe_contract: AgentContract) -> None:
    source = "def run(task_input, tools):\n    return {}.__class__.__bases__\n"
    report = analyse(source, safe_contract, use_bandit=False)
    assert any(f.rule_id == "AF109" for f in report.findings)


def test_hardcoded_secret_is_caught(safe_contract: AgentContract) -> None:
    source = 'API_KEY = "sk-abcdef0123456789abcdef"\n\ndef run(task_input, tools):\n    return {}\n'
    report = analyse(source, safe_contract, use_bandit=False)
    assert any(f.severity is Severity.HIGH for f in report.findings)


def test_syntax_error_fails_cleanly(safe_contract: AgentContract) -> None:
    report = analyse("def run(:\n", safe_contract, use_bandit=False)
    assert not report.passed
    assert report.findings[0].rule_id == "AF001"


def test_static_layer_is_deterministic(safe_contract: AgentContract) -> None:
    """Determinism matters for the experiment design (OQ-6)."""
    a = analyse(UNSAFE_IMPORT_AGENT, safe_contract, use_bandit=False)
    b = analyse(UNSAFE_IMPORT_AGENT, safe_contract, use_bandit=False)
    assert [str(f) for f in a.findings] == [str(f) for f in b.findings]


# ---------------------------------------------------------------- Layer 2


@pytest.fixture
def policy() -> EnterprisePolicy:
    return EnterprisePolicy.load()


def test_registered_tool_passes(safe_contract: AgentContract, policy: EnterprisePolicy) -> None:
    assert check(safe_contract, policy).passed


def test_unregistered_tool_fails(policy: EnterprisePolicy) -> None:
    contract = AgentContract(task_spec="x", authorized_tools=("definitely_not_a_tool",))
    report = check(contract, policy)
    assert not report.passed
    assert any(f.rule_id == "POL102" for f in report.findings)


def test_denied_tool_fails(policy: EnterprisePolicy) -> None:
    contract = AgentContract(task_spec="x", authorized_tools=("execute_shell",))
    report = check(contract, policy)
    assert not report.passed
    assert any(f.rule_id == "POL101" for f in report.findings)


def test_network_without_a_reason_fails(policy: EnterprisePolicy) -> None:
    contract = AgentContract(
        task_spec="x",
        authorized_tools=("read_log_file",),
        resource_limits=ResourceLimits(network_access=True),
    )
    report = check(contract, policy)
    assert not report.passed
    assert any(f.rule_id == "POL201" for f in report.findings)


def test_missing_approval_clause_fails(policy: EnterprisePolicy) -> None:
    contract = AgentContract(
        task_spec="x",
        authorized_tools=("read_log_file",),
        security_policy=SecurityPolicy(require_human_approval_for=()),
    )
    report = check(contract, policy)
    assert not report.passed
    assert any(f.rule_id == "POL501" for f in report.findings)


def test_resource_bounds_are_enforced(policy: EnterprisePolicy) -> None:
    """A contract cannot declare more than the enterprise ceiling."""
    with pytest.raises(ValueError):
        ResourceLimits(memory_mb=4096)


# ------------------------------------------------------------- Aggregator


def test_safe_and_correct_passes(safe_contract: AgentContract) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    reports = (
        analyse(SAFE_AGENT, safe_contract, use_bandit=False),
        check(safe_contract),
    )
    result = aggregate(spec, reports, correctness_score=2.5)
    assert result.verdict is Verdict.PASS
    assert result.deployable


def test_unsafe_routes_to_escalation(safe_contract: AgentContract) -> None:
    spec = make_spec(UNSAFE_IMPORT_AGENT, safe_contract)
    reports = (
        analyse(UNSAFE_IMPORT_AGENT, safe_contract, use_bandit=False),
        check(safe_contract),
    )
    result = aggregate(spec, reports, correctness_score=2.5)
    assert result.verdict is Verdict.FAIL_UNSAFE
    assert result.verdict.is_safety_failure
    assert not result.verdict.is_regenerable


def test_incorrect_routes_to_regeneration(safe_contract: AgentContract) -> None:
    spec = make_spec(INCORRECT_AGENT, safe_contract)
    reports = (
        analyse(INCORRECT_AGENT, safe_contract, use_bandit=False),
        check(safe_contract),
    )
    result = aggregate(spec, reports, correctness_score=0.5)
    assert result.verdict is Verdict.FAIL_INCORRECT
    assert result.verdict.is_regenerable
    assert not result.verdict.is_safety_failure


def test_both_dimensions_fail(safe_contract: AgentContract) -> None:
    spec = make_spec(UNSAFE_IMPORT_AGENT, safe_contract)
    reports = (
        analyse(UNSAFE_IMPORT_AGENT, safe_contract, use_bandit=False),
        check(safe_contract),
    )
    result = aggregate(spec, reports, correctness_score=0.2)
    assert result.verdict is Verdict.FAIL_BOTH


def test_strictness_shifts_the_verdict(safe_contract: AgentContract) -> None:
    """The RQ4 mechanism: the same evidence, a different threshold."""
    spec = make_spec(SAFE_AGENT, safe_contract)
    reports = (
        analyse(SAFE_AGENT, safe_contract, use_bandit=False),
        check(safe_contract),
    )
    permissive = aggregate(
        spec, reports, profile=StrictnessProfile.permissive(), correctness_score=1.2
    )
    paranoid = aggregate(
        spec, reports, profile=StrictnessProfile.paranoid(), correctness_score=1.2
    )
    assert permissive.verdict is Verdict.PASS
    assert paranoid.verdict is Verdict.FAIL_INCORRECT


# --------------------------------------------------------------- Pipeline


def test_pipeline_short_circuits_before_the_sandbox(
    safe_contract: AgentContract, local_runner
) -> None:
    spec = make_spec(UNSAFE_IMPORT_AGENT, safe_contract)
    pipeline = ValidationPipeline(local_runner, PipelineConfig.default(use_bandit=False))
    result = pipeline.validate(spec)
    assert result.verdict is Verdict.FAIL_UNSAFE
    assert result.report_for(ValidationLayer.SANDBOX) is None


def test_pipeline_runs_the_sandbox_for_a_clean_agent(
    safe_contract: AgentContract, local_runner
) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    pipeline = ValidationPipeline(
        local_runner,
        PipelineConfig.default(use_bandit=False, test_inputs=({"case": 1},)),
    )
    result = pipeline.validate(spec, expected_output={"total": 2})
    sandbox = result.report_for(ValidationLayer.SANDBOX)
    assert sandbox is not None
    assert sandbox.trace is not None
    assert not sandbox.trace.has_safety_violation


def test_network_attempt_is_recorded_at_runtime(
    safe_contract: AgentContract, local_runner
) -> None:
    """Layer 3 catches what Layer 1 would miss if the import were hidden."""
    spec = make_spec(UNSAFE_NETWORK_AGENT, safe_contract)
    pipeline = ValidationPipeline(
        local_runner,
        PipelineConfig.default(
            use_bandit=False, short_circuit=False, test_inputs=({"case": 1},)
        ),
    )
    result = pipeline.validate(spec)
    sandbox = result.report_for(ValidationLayer.SANDBOX)
    assert sandbox is not None and sandbox.trace is not None
    assert sandbox.trace.network_attempts
    assert result.verdict.is_safety_failure


def test_undeclared_tool_call_is_recorded(safe_contract: AgentContract, local_runner) -> None:
    from .conftest import UNDECLARED_TOOL_AGENT

    spec = make_spec(UNDECLARED_TOOL_AGENT, safe_contract)
    pipeline = ValidationPipeline(
        local_runner,
        PipelineConfig.default(
            use_bandit=False, short_circuit=False, test_inputs=({"case": 1},)
        ),
    )
    result = pipeline.validate(spec)
    sandbox = result.report_for(ValidationLayer.SANDBOX)
    assert sandbox is not None and sandbox.trace is not None
    assert sandbox.trace.unauthorized_tool_calls
