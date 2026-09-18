"""CPU credit sampling for burstable hosts.

Why this exists. The Azure for Students subscription has no quota for any
non-burstable VM family that AKS will accept, so the orchestrator VM and the
sandbox nodes all run on B-series v2. B-series accrues CPU credits while idle
and spends them under load; once the balance hits zero the host is throttled to
its baseline, which for a B2s_v2 is a fraction of the two vCPUs it nominally
has.

That matters because a validation batch is sustained load. Latency would drift
upward over the course of a run for reasons that have nothing to do with the
isolation level being measured — and since runs are randomized across levels,
the drift would smear across L1, L2 and L3 as extra variance, or worse, bias
whichever level happened to be scheduled late.

The fix is not to remove the confound, which the quota makes impossible, but to
make it visible: sample the credit balance alongside every latency measurement
so the analysis can test for it, condition on it, or exclude throttled windows.
A confound you can see in the data is a limitation; one you cannot is an error.

Sampling is best-effort by design. Azure Monitor lags by a minute or two and the
call can fail; a missing credit reading must never fail a validation run, so
every path here degrades to None.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Below this fraction of the maximum, the host is close enough to throttling
# that latency samples should be treated as suspect.
LOW_CREDIT_FRACTION = 0.10


@dataclass(frozen=True)
class CreditSample:
    """One reading of a burstable host's CPU credit state."""

    sampled_at: datetime
    remaining: float | None
    consumed: float | None
    resource_id: str

    @property
    def available(self) -> bool:
        return self.remaining is not None

    def is_low(self, maximum: float) -> bool:
        """Whether the host is near or at its throttling floor."""
        if self.remaining is None or maximum <= 0:
            return False
        return self.remaining <= maximum * LOW_CREDIT_FRACTION

    def as_detail(self) -> dict[str, float | str | None]:
        """Shape for the `detail` field of a telemetry event."""
        return {
            "cpu_credits_remaining": self.remaining,
            "cpu_credits_consumed": self.consumed,
            "sampled_at": self.sampled_at.isoformat(),
        }


class CpuCreditSampler:
    """Reads CPU credit metrics for a burstable VM via the az CLI.

    Results are cached for `cache_seconds` because Azure Monitor publishes these
    metrics at one-minute granularity — polling faster costs API calls and
    returns the same number.
    """

    def __init__(self, resource_id: str, *, cache_seconds: float = 60.0) -> None:
        self.resource_id = resource_id
        self.cache_seconds = cache_seconds
        self._cached: CreditSample | None = None

    @staticmethod
    def _az() -> str | None:
        return shutil.which("az") or shutil.which("az.cmd")

    def _fresh_enough(self) -> bool:
        if self._cached is None:
            return False
        age = (datetime.now(UTC) - self._cached.sampled_at).total_seconds()
        return age < self.cache_seconds

    def sample(self) -> CreditSample:
        """Current credit state, or an empty sample if it cannot be read."""
        if self._fresh_enough() and self._cached is not None:
            return self._cached

        empty = CreditSample(
            sampled_at=datetime.now(UTC),
            remaining=None,
            consumed=None,
            resource_id=self.resource_id,
        )

        executable = self._az()
        if executable is None:
            return empty

        try:
            proc = subprocess.run(
                [
                    executable, "monitor", "metrics", "list",
                    "--resource", self.resource_id,
                    "--metric", "CPU Credits Remaining,CPU Credits Consumed",
                    "--interval", "PT1M",
                    "--aggregation", "Average",
                    "--top", "1",
                    "-o", "json",
                ],
                capture_output=True, text=True, timeout=45, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("CPU credit sampling failed: %s", exc)
            return empty

        if proc.returncode != 0 or not proc.stdout.strip():
            return empty

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return empty

        readings: dict[str, float] = {}
        for metric in payload.get("value", []):
            name = (metric.get("name") or {}).get("value", "")
            points = [
                point.get("average")
                for series in metric.get("timeseries", [])
                for point in series.get("data", [])
                if point.get("average") is not None
            ]
            if points:
                readings[name] = float(points[-1])

        sample = CreditSample(
            sampled_at=datetime.now(UTC),
            remaining=readings.get("CPU Credits Remaining"),
            consumed=readings.get("CPU Credits Consumed"),
            resource_id=self.resource_id,
        )
        self._cached = sample
        return sample


class NullSampler:
    """Used when the host is not burstable, or when sampling is switched off.

    Returning a sample with `remaining=None` rather than raising keeps the call
    site free of conditionals — a run on non-burstable hardware simply records
    no credit data.
    """

    def sample(self) -> CreditSample:
        return CreditSample(
            sampled_at=datetime.now(UTC), remaining=None, consumed=None, resource_id=""
        )


def build_sampler(resource_id: str | None) -> CpuCreditSampler | NullSampler:
    return CpuCreditSampler(resource_id) if resource_id else NullSampler()
