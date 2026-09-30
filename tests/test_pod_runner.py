"""L2 and L3 must differ in exactly one thing: the container runtime.

The pilot compared ACI (L2) against gVisor pods (L3) and found the stronger
isolation level six times faster — because it was comparing provisioning models,
not isolation mechanisms. These tests pin the fix down: if anything other than
runtimeClassName ever differs between the two pod specs, the RQ2 comparison is
confounded again, and a run under the wrong kernel must never be recorded.
"""

from __future__ import annotations

import copy

import pytest

from agentfactory.contracts import AgentContract, ExecutionTrace, IsolationLevel
from agentfactory.validation.sandbox.base import SandboxExecutionError, SandboxRequest
from agentfactory.validation.sandbox.pod_runner import (
    GVISOR_RUNTIME_CLASS,
    gvisor_pod_runner,
    runc_pod_runner,
)

from .conftest import SAFE_AGENT, make_spec


@pytest.fixture
def request_(safe_contract: AgentContract) -> SandboxRequest:
    return SandboxRequest(spec=make_spec(SAFE_AGENT, safe_contract), test_input={"case": 1})


def _strip_identity(manifest: dict) -> dict:
    """Remove the fields that legitimately differ per run or per level label."""
    m = copy.deepcopy(manifest)
    m["metadata"].pop("name")
    m["metadata"]["labels"].pop("level")
    m["spec"].pop("runtimeClassName", None)
    return m


def test_l2_and_l3_specs_differ_only_in_runtime(request_: SandboxRequest) -> None:
    l2 = runc_pod_runner(kube_context="test").manifest(request_, "a")
    l3 = gvisor_pod_runner(kube_context="test").manifest(request_, "a")
    assert _strip_identity(l2) == _strip_identity(l3)


def test_l2_uses_the_default_runtime(request_: SandboxRequest) -> None:
    spec = runc_pod_runner(kube_context="test").manifest(request_, "a")["spec"]
    assert "runtimeClassName" not in spec


def test_l3_requests_gvisor(request_: SandboxRequest) -> None:
    spec = gvisor_pod_runner(kube_context="test").manifest(request_, "a")["spec"]
    assert spec["runtimeClassName"] == GVISOR_RUNTIME_CLASS


def test_both_levels_land_on_the_same_node_pool(request_: SandboxRequest) -> None:
    """Different hardware would be a second varying factor."""
    for runner in (runc_pod_runner(kube_context="t"), gvisor_pod_runner(kube_context="t")):
        spec = runner.manifest(request_, "a")["spec"]
        assert spec["nodeSelector"] == {"agentfactory.io/sandbox": "gvisor"}
        assert spec["tolerations"][0]["key"] == "agentfactory.io/sandbox"


def test_contract_limits_reach_the_pod(safe_contract: AgentContract) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    manifest = runc_pod_runner(kube_context="t").manifest(
        SandboxRequest(spec=spec, test_input={}), "a"
    )
    limits = manifest["spec"]["containers"][0]["resources"]["limits"]
    assert limits["memory"] == f"{safe_contract.resource_limits.memory_mb}Mi"


# ------------------------------------------------------- kernel verification


def _trace(level: IsolationLevel, release: str | None) -> ExecutionTrace:
    return ExecutionTrace(isolation_level=level, exit_code=0, kernel_release=release)


def test_l3_on_gvisor_kernel_is_accepted() -> None:
    gvisor_pod_runner(kube_context="t")._verify_kernel(
        _trace(IsolationLevel.L3, "4.19.0-gvisor")
    )


def test_l3_on_host_kernel_is_rejected() -> None:
    """The failure this check exists for: RuntimeClass silently not in force."""
    with pytest.raises(SandboxExecutionError, match="gVisor runtime is not in force"):
        gvisor_pod_runner(kube_context="t")._verify_kernel(
            _trace(IsolationLevel.L3, "6.8.0-1067-azure")
        )


def test_l2_on_host_kernel_is_accepted() -> None:
    runc_pod_runner(kube_context="t")._verify_kernel(
        _trace(IsolationLevel.L2, "6.8.0-1067-azure")
    )


def test_l2_under_gvisor_is_rejected() -> None:
    with pytest.raises(SandboxExecutionError, match="expected the default runtime"):
        runc_pod_runner(kube_context="t")._verify_kernel(
            _trace(IsolationLevel.L2, "4.19.0-gvisor")
        )


def test_missing_report_is_not_a_kernel_mismatch() -> None:
    """No report means the agent never ran; exit-code handling covers that."""
    gvisor_pod_runner(kube_context="t")._verify_kernel(_trace(IsolationLevel.L3, None))


# --------------------------------------------- no report, no verdict


def test_no_report_without_an_agent_cause_is_not_a_result() -> None:
    """The defect that invalidated two rounds of L3 latency measurements.

    The image had no `python` on PATH, every pod died at StartError before the
    harness ran, and the aggregator read the empty output as FAIL_INCORRECT.
    """
    from agentfactory.validation.sandbox.base import require_attributable

    trace = ExecutionTrace(
        isolation_level=IsolationLevel.L3,
        exit_code=128,
        harness_reported=False,
        terminated_reason="StartError",
    )
    with pytest.raises(SandboxExecutionError, match="produced no report"):
        require_attributable(trace, agent_caused=False)


def test_out_of_memory_is_the_agents_doing() -> None:
    from agentfactory.contracts import ResourceLimits
    from agentfactory.validation.sandbox.base import require_attributable

    trace = ExecutionTrace(
        isolation_level=IsolationLevel.L2,
        exit_code=137,
        harness_reported=False,
        terminated_reason="OOMKilled",
    )
    kept = require_attributable(trace, agent_caused=True)
    assert kept.exceeds(ResourceLimits(memory_mb=256))


def test_timeout_without_report_is_still_a_result() -> None:
    from agentfactory.validation.sandbox.base import require_attributable

    trace = ExecutionTrace(
        isolation_level=IsolationLevel.L1, timed_out=True, harness_reported=False
    )
    assert require_attributable(trace, agent_caused=False) is trace


def test_parse_marks_a_missing_report() -> None:
    from agentfactory.validation.sandbox.base import parse_harness_result

    trace = parse_harness_result(
        "", level=IsolationLevel.L3, exit_code=128, wall_time_ms=5.0
    )
    assert trace.harness_reported is False
