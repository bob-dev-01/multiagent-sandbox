"""L3 — gVisor pod. Kept as a compatibility shim.

The implementation moved to `pod_runner.py` when L2 was moved onto the same AKS
node pool: the two levels now share one runner and differ only in
`runtimeClassName`. See that module for the reasoning.
"""

from __future__ import annotations

from typing import Any

from agentfactory.contracts import IsolationLevel

from .pod_runner import DEFAULT_IMAGE, DEFAULT_NAMESPACE, GVISOR_RUNTIME_CLASS, PodRunner

DEFAULT_RUNTIME_CLASS = GVISOR_RUNTIME_CLASS

__all__ = ["DEFAULT_IMAGE", "DEFAULT_NAMESPACE", "DEFAULT_RUNTIME_CLASS", "GvisorPodRunner"]


class GvisorPodRunner(PodRunner):
    """L3 isolation: a pod under the gVisor RuntimeClass."""

    def __init__(self, *, runtime_class: str = GVISOR_RUNTIME_CLASS, **kwargs: Any) -> None:
        super().__init__(level=IsolationLevel.L3, runtime_class=runtime_class, **kwargs)
