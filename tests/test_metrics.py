"""Tests for the metric definitions.

The first group is the important one: it pins down that UDR and FNR are
different quantities, which is the point of departure from Task 7.
"""

from __future__ import annotations

import math

from agentfactory.contracts import Verdict
from agentfactory.metrics.core import (
    GroundTruth,
    Observation,
    by_isolation_level,
    combined_score,
    latency_metrics,
    safety_metrics,
    security_latency_score,
    wilson_interval,
)


def obs(
    gt: GroundTruth,
    verdict: Verdict,
    *,
    reached: bool = False,
    executed_unsafely: bool = False,
    latency: float = 100.0,
    level: str = "L2",
    agent: str = "a",
) -> Observation:
    return Observation(
        agent_id=agent,
        task_id="T1",
        ground_truth=gt,
        verdict=verdict,
        isolation_level=level,
        latency_ms=latency,
        reached_execution=reached,
        executed_unsafely=executed_unsafely,
    )


# ------------------------------------------------- the UDR / FNR distinction


def test_fnr_and_udr_differ_when_a_later_control_intervenes() -> None:
    """Two unsafe agents pass validation; only one of them reaches execution.

    FNR sees both (the classifier failed twice). UDR sees one (the post-verdict
    controls stopped the other). Under Task 7's definition these two numbers
    would be identical and the second control would be invisible.
    """
    observations = [
        obs(GroundTruth.UNSAFE_ONLY, Verdict.PASS, reached=True),
        obs(GroundTruth.UNSAFE_ONLY, Verdict.PASS, reached=False),
        obs(GroundTruth.UNSAFE_ONLY, Verdict.FAIL_UNSAFE),
        obs(GroundTruth.UNSAFE_ONLY, Verdict.FAIL_UNSAFE),
    ]
    m = safety_metrics(observations)
    assert m.false_negative_rate == 0.5
    assert m.unsafe_deployment_rate == 0.25
    assert m.udr_fnr_gap == 0.25


def test_udr_never_exceeds_fnr() -> None:
    """Deployment is downstream of the verdict, so UDR <= FNR always holds."""
    observations = [
        obs(GroundTruth.UNSAFE_ONLY, Verdict.PASS, reached=True),
        obs(GroundTruth.BOTH, Verdict.PASS, reached=True),
        obs(GroundTruth.UNSAFE_ONLY, Verdict.FAIL_UNSAFE),
    ]
    m = safety_metrics(observations)
    assert m.unsafe_deployment_rate <= m.false_negative_rate


def test_gap_is_zero_when_nothing_intervenes() -> None:
    observations = [
        obs(GroundTruth.UNSAFE_ONLY, Verdict.PASS, reached=True),
        obs(GroundTruth.UNSAFE_ONLY, Verdict.PASS, reached=True),
    ]
    m = safety_metrics(observations)
    assert m.false_negative_rate == 1.0
    assert m.unsafe_deployment_rate == 1.0
    assert m.udr_fnr_gap == 0.0


# --------------------------------------------------------- classifier metrics


def test_false_positive_rate_counts_only_safe_agents() -> None:
    observations = [
        obs(GroundTruth.SAFE_CORRECT, Verdict.FAIL_UNSAFE),
        obs(GroundTruth.SAFE_CORRECT, Verdict.PASS),
        obs(GroundTruth.SAFE_CORRECT, Verdict.PASS),
        obs(GroundTruth.SAFE_CORRECT, Verdict.PASS),
        obs(GroundTruth.UNSAFE_ONLY, Verdict.FAIL_UNSAFE),
    ]
    assert safety_metrics(observations).false_positive_rate == 0.25


def test_incorrect_only_is_not_a_safety_positive() -> None:
    """FAIL_INCORRECT is not a safety prediction — it must not inflate FPR."""
    observations = [
        obs(GroundTruth.SAFE_CORRECT, Verdict.FAIL_INCORRECT),
        obs(GroundTruth.SAFE_CORRECT, Verdict.PASS),
    ]
    assert safety_metrics(observations).false_positive_rate == 0.0


def test_perfect_classifier() -> None:
    observations = [
        obs(GroundTruth.UNSAFE_ONLY, Verdict.FAIL_UNSAFE),
        obs(GroundTruth.BOTH, Verdict.FAIL_BOTH),
        obs(GroundTruth.SAFE_CORRECT, Verdict.PASS),
        obs(GroundTruth.SAFE_CORRECT, Verdict.PASS),
    ]
    m = safety_metrics(observations)
    assert m.false_negative_rate == 0.0
    assert m.false_positive_rate == 0.0
    assert m.accuracy == 1.0
    assert m.f1 == 1.0


def test_empty_population_gives_nan_not_zero() -> None:
    """'No unsafe agents were deployed' must not be confused with 'there were none'."""
    m = safety_metrics([obs(GroundTruth.SAFE_CORRECT, Verdict.PASS)])
    assert math.isnan(m.false_negative_rate)
    assert math.isnan(m.unsafe_deployment_rate)


# ------------------------------------------------------------------ CER / CI


def test_zero_catastrophic_events_still_reports_an_upper_bound() -> None:
    """OQ-11: an observed 0/30 is not evidence the true rate is zero."""
    observations = [obs(GroundTruth.SAFE_CORRECT, Verdict.PASS) for _ in range(30)]
    m = safety_metrics(observations)
    assert m.catastrophic_error_rate == 0.0
    low, high = m.cer_ci
    assert low == 0.0
    assert 0.08 < high < 0.15


def test_wilson_interval_brackets_the_estimate() -> None:
    low, high = wilson_interval(5, 20)
    assert low < 0.25 < high
    assert 0.0 <= low <= high <= 1.0


# -------------------------------------------------------------------- scores


def test_security_latency_score_is_scale_invariant_in_ranking() -> None:
    """OQ-5: changing the reference must not reorder the isolation levels."""
    fast, slow = 200.0, 8000.0
    ms = (
        security_latency_score(0.02, fast),
        security_latency_score(0.01, slow),
    )
    rescaled = (
        security_latency_score(0.02, fast, reference_ms=1000.0),
        security_latency_score(0.01, slow, reference_ms=1000.0),
    )
    assert (ms[0] > ms[1]) == (rescaled[0] > rescaled[1])


def test_security_latency_score_is_defined_below_one_millisecond() -> None:
    """The original formula divides by zero at 1 ms and goes negative below it."""
    score = security_latency_score(0.0, 0.5)
    assert score > 0
    assert not math.isnan(score)


def test_combined_score_weights_safety_higher() -> None:
    safe_but_wrong = combined_score(0.0, 0.0)
    unsafe_but_right = combined_score(1.0, 3.0)
    assert safe_but_wrong > unsafe_but_right


# ----------------------------------------------------------------- latency


def test_latency_percentiles_and_tail_ratios() -> None:
    values = [obs(GroundTruth.SAFE_CORRECT, Verdict.PASS, latency=float(i)) for i in range(1, 101)]
    m = latency_metrics(values)
    assert m.n == 100
    assert 50 <= m.p50 <= 51
    assert m.p95 >= m.p50
    assert m.tail_ratio_p95 >= 1.0


def test_breakdown_by_isolation_level() -> None:
    observations = [
        obs(GroundTruth.UNSAFE_ONLY, Verdict.PASS, reached=True, level="L1", latency=50),
        obs(GroundTruth.UNSAFE_ONLY, Verdict.FAIL_UNSAFE, level="L3", latency=5000),
    ]
    breakdown = by_isolation_level(observations)
    assert set(breakdown) == {"L1", "L3"}
    l1_safety, l1_latency = breakdown["L1"]
    l3_safety, _ = breakdown["L3"]
    assert l1_safety.false_negative_rate == 1.0
    assert l3_safety.false_negative_rate == 0.0
    assert l1_latency.p50 == 50
