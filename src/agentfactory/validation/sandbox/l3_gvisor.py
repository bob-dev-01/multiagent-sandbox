"""L3 — Kubernetes pod under a gVisor RuntimeClass. Host kernel unreachable.

gVisor's Sentry intercepts syscalls in userspace before they reach the host
kernel, which is a stronger boundary than the namespace isolation of L2 and the
reason this level exists for agents touching sensitive data.

Deployment note, because it decides whether this level works at all. AKS does
not ship a gVisor RuntimeClass; Azure's supported sandboxing path is Kata (Pod
Sandboxing). Getting gVisor requires installing `runsc` onto the nodes and
registering a containerd runtime handler — `infra/scripts/install-gvisor-daemonset.yaml`
does that with a privileged DaemonSet. It works, but it is node customization
on a managed control plane and can break across AKS upgrades. `preflight()`
below exists so that a broken RuntimeClass surfaces as a clear failure before an
experiment batch starts, rather than as a silent fallback to weaker isolation.

If the DaemonSet route proves unstable, the fallback is `runtimeClassName:
kata-mshv-vm-isolation`, which is supported natively but is Kata rather than
gVisor — a change that belongs in the thesis delimitations, not in a quiet
config edit.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
import uuid
from typing import Any

import yaml

from agentfactory.contracts import ExecutionTrace, IsolationLevel

from .base import (
    HARNESS_PATH,
    SandboxRequest,
    SandboxUnavailableError,
    parse_harness_result,
)

DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_RUNTIME_CLASS = "gvisor"
DEFAULT_NAMESPACE = "agentfactory-sandbox"


class GvisorPodRunner:
    """L3 isolation via kubectl against an AKS cluster."""

    level = IsolationLevel.L3

    def __init__(
        self,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        runtime_class: str = DEFAULT_RUNTIME_CLASS,
        image: str = DEFAULT_IMAGE,
        kubeconfig: str | None = None,
        node_selector: dict[str, str] | None = None,
    ) -> None:
        self.namespace = namespace
        self.runtime_class = runtime_class
        self.image = image
        self.kubeconfig = kubeconfig
        self.node_selector = node_selector or {"agentfactory.io/sandbox": "gvisor"}

    # -- plumbing ---------------------------------------------------------

    def _kubectl(self, *args: str, timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess[str]:
        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        cmd += ["-n", self.namespace, *args]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=check
        )

    def available(self) -> bool:
        if shutil.which("kubectl") is None:
            return False
        probe = self._kubectl("get", "ns", self.namespace, "-o", "name", timeout=30)
        return probe.returncode == 0

    def preflight(self) -> None:
        """Fail loudly if L3 is not actually L3.

        Called before an experiment batch. Running agents labelled high-risk
        under an isolation level that silently degraded would invalidate both
        the safety claim and the RQ2 comparison.
        """
        if shutil.which("kubectl") is None:
            raise SandboxUnavailableError("kubectl not found on PATH")

        rc = subprocess.run(
            ["kubectl", *(["--kubeconfig", self.kubeconfig] if self.kubeconfig else []),
             "get", "runtimeclass", self.runtime_class, "-o", "json"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if rc.returncode != 0:
            raise SandboxUnavailableError(
                f"RuntimeClass '{self.runtime_class}' is not registered on the cluster. "
                "Apply infra/scripts/install-gvisor-daemonset.yaml, or switch to the "
                "Kata fallback and record that change in the delimitations."
            )

        ns = self._kubectl("get", "ns", self.namespace, "-o", "name", timeout=30)
        if ns.returncode != 0:
            raise SandboxUnavailableError(f"namespace '{self.namespace}' does not exist")

    # -- manifest ---------------------------------------------------------

    def _manifest(self, request: SandboxRequest, name: str) -> dict[str, Any]:
        limits = request.spec.contract.resource_limits
        harness_b64 = base64.b64encode(HARNESS_PATH.read_bytes()).decode()
        job_b64 = base64.b64encode(request.payload().encode("utf-8")).decode()

        bootstrap = (
            "import base64,os,sys;"
            f"open('/tmp/h.py','wb').write(base64.b64decode('{harness_b64}'));"
            f"open('/tmp/job.json','wb').write(base64.b64decode('{job_b64}'));"
            "os.makedirs('/tmp/agent',exist_ok=True);"
            "os.execve(sys.executable,[sys.executable,'-I','-S','/tmp/h.py'],os.environ)"
        )

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "labels": {"app": "agentfactory-sandbox", "level": "l3"},
            },
            "spec": {
                "runtimeClassName": self.runtime_class,
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "nodeSelector": self.node_selector,
                "activeDeadlineSeconds": limits.max_duration_sec + 30,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 65534,
                    "runAsGroup": 65534,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "agent",
                        "image": self.image,
                        "command": ["python", "-c", bootstrap],
                        "env": [
                            {"name": "AGENTFACTORY_JOB_FILE", "value": "/tmp/job.json"},
                            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                            {"name": "PYTHONHASHSEED", "value": "0"},
                            {"name": "PYTHONUNBUFFERED", "value": "1"},
                            {"name": "HOME", "value": "/tmp/agent"},
                        ],
                        # Limits straight from the contract (OQ-15).
                        "resources": {
                            "limits": {
                                "cpu": str(limits.cpu_cores),
                                "memory": f"{limits.memory_mb}Mi",
                                "ephemeral-storage": "128Mi",
                            },
                            "requests": {
                                "cpu": str(min(limits.cpu_cores, 0.5)),
                                "memory": f"{min(limits.memory_mb, 128)}Mi",
                            },
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [{"name": "scratch", "mountPath": "/tmp"}],
                    }
                ],
                "volumes": [
                    {"name": "scratch", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}}
                ],
            },
        }

    # -- execution --------------------------------------------------------

    def run(self, request: SandboxRequest) -> ExecutionTrace:
        if shutil.which("kubectl") is None:
            raise SandboxUnavailableError("kubectl not found on PATH")

        limits = request.spec.contract.resource_limits
        name = f"af-l3-{uuid.uuid4().hex[:12]}"
        manifest = yaml.safe_dump(self._manifest(request, name))

        started = time.perf_counter()
        timed_out = False
        stdout = stderr = ""
        exit_code: int | None = None

        # kubectl apply reads the manifest from stdin, so this cannot go
        # through the _kubectl helper.
        apply_cmd = ["kubectl"]
        if self.kubeconfig:
            apply_cmd += ["--kubeconfig", self.kubeconfig]
        apply_cmd += ["-n", self.namespace, "apply", "-f", "-"]
        applied = subprocess.run(
            apply_cmd, input=manifest, capture_output=True, text=True, timeout=120, check=False
        )
        if applied.returncode != 0:
            raise SandboxUnavailableError(f"pod apply failed: {applied.stderr.strip()}")

        try:
            deadline = time.monotonic() + limits.max_duration_sec + 120
            phase = "Pending"
            while time.monotonic() < deadline:
                got = self._kubectl("get", "pod", name, "-o", "json", timeout=60)
                if got.returncode != 0:
                    time.sleep(1)
                    continue
                try:
                    pod = json.loads(got.stdout)
                except json.JSONDecodeError:
                    time.sleep(1)
                    continue
                phase = pod.get("status", {}).get("phase", "Pending")
                if phase in {"Succeeded", "Failed"}:
                    statuses = pod.get("status", {}).get("containerStatuses") or []
                    if statuses:
                        terminated = statuses[0].get("state", {}).get("terminated") or {}
                        exit_code = terminated.get("exitCode")
                    break
                time.sleep(1)
            else:
                timed_out = True

            logs = self._kubectl("logs", name, "--tail", "-1", timeout=120)
            stdout = logs.stdout
            stderr = logs.stderr
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._kubectl("delete", "pod", name, "--ignore-not-found", "--now", timeout=120)

        return parse_harness_result(
            stdout,
            level=self.level,
            exit_code=exit_code,
            wall_time_ms=elapsed_ms,
            timed_out=timed_out,
            stderr=stderr,
            expected_output=request.expected_output,
        )
