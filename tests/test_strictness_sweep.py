"""The RQ4 replay must read findings back exactly and count rates correctly."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

from strictness_sweep import mcnemar_exact, parse_findings, rates  # noqa: E402

from agentfactory.contracts import Finding, Severity, ValidationLayer  # noqa: E402


def test_findings_round_trip_through_their_string_form() -> None:
    original = (
        Finding(layer=ValidationLayer.STATIC, rule_id="AF104", severity=Severity.HIGH,
                message="forbidden call: eval()", line_number=5),
        Finding(layer=ValidationLayer.STATIC, rule_id="B307", severity=Severity.MEDIUM,
                message="Use of possibly insecure function: consider ast.literal_eval."),
    )
    assert parse_findings([str(f) for f in original], ValidationLayer.STATIC) == original


def test_unparseable_finding_is_an_error() -> None:
    with pytest.raises(ValueError):
        parse_findings(["not a finding"], ValidationLayer.STATIC)


def test_mcnemar_exact() -> None:
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(5, 5) == 1.0
    assert mcnemar_exact(10, 0) == pytest.approx(2 / 2**10)


def test_rates_count_each_outcome_against_its_own_denominator() -> None:
    counts = Counter({
        ("L2", "safe_correct", "PASS"): 9, ("L2", "safe_correct", "FAIL_INCORRECT"): 1,
        ("L2", "incorrect_only", "PASS"): 3, ("L2", "incorrect_only", "FAIL_INCORRECT"): 7,
        ("L2", "unsafe_only", "FAIL_UNSAFE"): 4, ("L2", "both", "PASS"): 1,
    })
    r = rates(counts)
    assert (r["false_rejection_rate"]["k"], r["false_rejection_rate"]["n"]) == (1, 10)
    assert (r["incorrect_pass_rate"]["k"], r["incorrect_pass_rate"]["n"]) == (3, 10)
    assert (r["unsafe_pass_rate"]["k"], r["unsafe_pass_rate"]["n"]) == (1, 5)
    assert (r["unsafe_flag_rate"]["k"], r["unsafe_flag_rate"]["n"]) == (0, 20)
