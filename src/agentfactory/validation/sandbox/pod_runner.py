"""L2 and L3 as Kubernetes pods on the same node pool, differing only in runtime.

Why one runner for two levels. The pilot measured L2 on ephemeral ACI at a p50
of 43.5 s and L3 as a gVisor pod at 7.5 s — the stronger isolation level was
six times faster than the weaker one. That was not a finding about isolation.
ACI builds a container group from nothing on every validation, while an AKS
node is already warm and only has to schedule a pod, so the comparison was
measuring two provisioning models and attributing the difference to gVisor
(OQ-4 in architecture.md).

The fix is to hold everything constant except the thing RQ2 asks about. Both
levels here run on the same sandbox node pool, from the same image, with the
same resource limits, security context, tolerations and node selector. The
single difference is `runtimeClassName`:

    L2  — unset, so the node's default runtime (runc): namespaces and cgroups,
          host kernel shared.
    L3  — "gvisor", so runsc: the Sentry intercepts syscalls in userspace and
          the host kernel is unreachable.

Each run also proves which kernel it executed under. The harness reports
`platform.release()`, and a gVisor sandbox reports a synthetic release string
containing "gvisor" while runc reports the host's. A run whose kernel does not
match the level it was scheduled as is rejected as an infrastructure failure —
never recorded — so a RuntimeClass that silently stopped resolving cannot turn
L3 runs into mislabelled L2 runs.

Deployment note for L3. AKS does not ship gVisor; Azure's supported sandboxing
path is Kata. `infra/scripts/install-gvisor-daemonset.yaml` installs runsc with
a privileged DaemonSet, which is node customization on a managed control plane
and is reverted whenever a node is replaced. The DaemonSet reinstalls on every
new node automatically; `preflight()` and the per-run kernel check are what keep
a gap in that coverage from becoming bad data.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any, Protocol

import yaml

from agentfactory.contracts import ExecutionTrace, IsolationLevel

from .base import (
    HARNESS_PATH,
    PLATFORM_START_FAILURES,
    SandboxExecutionError,
    SandboxRequest,
    SandboxUnavailableError,
    parse_harness_result,
    require_attributable,
    resolve_executable,
)

# Pulled from MCR, not Docker Hub: the sandbox subnet's only egress allowance is
# Microsoft's registry service tags.
DEFAULT_IMAGE = "mcr.microsoft.com/azurelinux/base/python:3.12"
DEFAULT_NAMESPACE = "agentfactory-sandbox"
GVISOR_RUNTIME_CLASS = "gvisor"

# The sandbox pool. The label value predates L2 moving onto this pool; it names
# the pool where runsc is installed, which remains true. Both levels schedule
# here so that host hardware is not a second varying factor.
SANDBOX_NODE_SELECTOR = {"agentfactory.io/sandbox": "gvisor"}
SANDBOX_TOLERATIONS = [
    {
        "key": "agentfactory.io/sandbox",
        "operator": "Equal",
        "value": "gvisor",
        "effect": "NoSchedule",
    }
]


def _default_context() -> str | None:
    # Pinned rather than ambient for the same reason afctl pins the Azure
    # subscription: kubectl's current-context moves, and a validation batch
    # must not quietly run against whichever cluster it moved to.
    return os.environ.get("AGENTFACTORY_KUBE_CONTEXT", "agentfac-aks") or None


class PodRunner:
    """A validation sandbox as a single-use Kubernetes pod."""

    def __init__(
        self,
        *,
        level: IsolationLevel,
        runtime_class: str | None,
        namespace: str = DEFAULT_NAMESPACE,
        image: str = DEFAULT_IMAGE,
        kube_context: str | None = None,
        kubeconfig: str | None = None,
    ) -> None:
        self.level = level
        self.runtime_class = runtime_class
        self.namespace = namespace
        self.image = image
        self.kube_context = kube_context if kube_context is not None else _default_context()
        self.kubeconfig = kubeconfig

    @property
    def expects_gvisor(self) -> bool:
        return self.runtime_class == GVISOR_RUNTIME_CLASS

    # -- plumbing ---------------------------------------------------------

    def _base_cmd(self) -> list[str]:
        cmd = [resolve_executable("kubectl")]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.kube_context:
            cmd += ["--context", self.kube_context]
        return cmd

    def _kubectl(
        self, *args: str, timeout: int = 120, namespaced: bool = True, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd = self._base_cmd()
        if namespaced:
            cmd += ["-n", self.namespace]
        cmd += list(args)
        return subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
        )

    def available(self) -> bool:
        if shutil.which("kubectl") is None and shutil.which("kubectl.exe") is None:
            return False
        return self._kubectl("get", "ns", self.namespace, "-o", "name", timeout=30).returncode == 0

    def preflight(self) -> None:
        """Fail loudly before a batch if this level cannot be what it claims."""
        if shutil.which("kubectl") is None and shutil.which("kubectl.exe") is None:
            raise SandboxUnavailableError("kubectl not found on PATH")

        if self.runtime_class:
            rc = self._kubectl(
                "get", "runtimeclass", self.runtime_class, "-o", "name",
                timeout=60, namespaced=False,
            )
            if rc.returncode != 0:
                raise SandboxUnavailableError(
                    f"RuntimeClass '{self.runtime_class}' is not registered on the cluster. "
                    "Apply infra/scripts/install-gvisor-daemonset.yaml, or switch to the "
                    "Kata fallback and record that change in the delimitations."
                )

        ns = self._kubectl("get", "ns", self.namespace, "-o", "name", timeout=30)
        if ns.returncode != 0:
            raise SandboxUnavailableError(
                f"namespace '{self.namespace}' does not exist "
                f"(kube context: {self.kube_context or 'current'})"
            )

    # -- manifest ---------------------------------------------------------

    def manifest(self, request: SandboxRequest, name: str) -> dict[str, Any]:
        """The pod spec. Identical across levels apart from runtimeClassName."""
        limits = request.spec.contract.resource_limits
        harness_b64 = base64.b64encode(HARNESS_PATH.read_bytes()).decode()
        job_b64 = base64.b64encode(request.payload().encode("utf-8")).decode()

        bootstrap = (
            # Stamped first, so the harness can report how long the sandbox
            # took to get from here to running it (sandbox_init_ms).
            "import time,os;os.environ['AGENTFACTORY_BOOT_EPOCH']=repr(time.time());"
            "import base64,sys;"
            f"open('/tmp/h.py','wb').write(base64.b64decode('{harness_b64}'));"
            f"open('/tmp/job.json','wb').write(base64.b64decode('{job_b64}'));"
            "os.makedirs('/tmp/agent',exist_ok=True);"
            "os.execve(sys.executable,[sys.executable,'-I','-S','/tmp/h.py'],os.environ)"
        )

        spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "nodeSelector": dict(SANDBOX_NODE_SELECTOR),
            "tolerations": [dict(t) for t in SANDBOX_TOLERATIONS],
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
                    # python3, not python: the MCR azurelinux image ships only
                    # python3. With "python" every pod failed at StartError
                    # before the harness ran — which is how two rounds of L3
                    # "latency" ended up measuring container start failures.
                    "command": ["python3", "-c", bootstrap],
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
        }
        if self.runtime_class:
            spec["runtimeClassName"] = self.runtime_class

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "labels": {
                    "app": "agentfactory-sandbox",
                    "level": self.level.value.lower(),
                },
            },
            "spec": spec,
        }

    # -- execution --------------------------------------------------------

    def _verify_kernel(self, trace: ExecutionTrace) -> None:
        """Reject a run that did not execute under the runtime its level claims."""
        release = trace.kernel_release
        if release is None:
            # No harness report at all — the pod never ran the agent. The
            # caller's exit-code and timeout handling covers that case, and
            # there is nothing to verify.
            return
        on_gvisor = "gvisor" in release.lower()
        if self.expects_gvisor and not on_gvisor:
            raise SandboxExecutionError(
                f"{self.level.value} was scheduled under RuntimeClass "
                f"'{self.runtime_class}' but ran on kernel {release!r}. The gVisor "
                "runtime is not in force on this node; refusing to record the run."
            )
        if not self.expects_gvisor and on_gvisor:
            raise SandboxExecutionError(
                f"{self.level.value} expected the default runtime but ran under gVisor "
                f"({release!r}); the node's default runtime has been changed."
            )

    def run(self, request: SandboxRequest) -> ExecutionTrace:
        limits = request.spec.contract.resource_limits
        name = f"af-{self.level.value.lower()}-{uuid.uuid4().hex[:12]}"
        manifest = yaml.safe_dump(self.manifest(request, name))

        started = time.perf_counter()
        timed_out = False
        stdout = stderr = ""
        exit_code: int | None = None
        reason: str | None = None
        message = ""
        clock = _Milestones(started)

        applied = self._kubectl("apply", "-f", "-", stdin=manifest, timeout=120)
        if applied.returncode != 0:
            raise SandboxExecutionError(f"pod apply failed: {applied.stderr.strip()[:500]}")
        clock.mark("applied", time.perf_counter(), exact=True)

        try:
            deadline = time.monotonic() + limits.max_duration_sec + 120
            for seen_at, pod, initial in self._pod_states(name, deadline):
                status = pod.get("status") or {}
                for milestone, reached in _progress(status).items():
                    if reached:
                        clock.mark(milestone, seen_at, exact=not initial)

                if status.get("phase") in {"Succeeded", "Failed"}:
                    statuses = status.get("containerStatuses") or []
                    if statuses:
                        terminated = (statuses[0].get("state") or {}).get("terminated") or {}
                        exit_code = terminated.get("exitCode")
                        reason = terminated.get("reason")
                        message = terminated.get("message") or ""
                    # activeDeadlineSeconds is enforced at pod level.
                    if status.get("reason") == "DeadlineExceeded":
                        timed_out = True
                    break
                # A container that cannot be created may sit in Waiting with a
                # reason instead of reaching Failed. Catch it here rather than
                # waiting out the whole deadline.
                for cs in status.get("containerStatuses") or []:
                    waiting = (cs.get("state") or {}).get("waiting") or {}
                    if waiting.get("reason") in PLATFORM_START_FAILURES:
                        reason = waiting.get("reason")
                        message = waiting.get("message") or ""
                if reason in PLATFORM_START_FAILURES:
                    break
            else:
                # The watch ran out the clock. Confirm against the API server
                # before blaming the agent: a watch that stalled while the pod
                # finished would otherwise record an infrastructure fault as
                # the agent exceeding its time.
                got = self._kubectl("get", "pod", name, "-o", "json", timeout=60)
                try:
                    phase = (json.loads(got.stdout).get("status") or {}).get("phase")
                except json.JSONDecodeError:
                    phase = None
                if got.returncode != 0 or phase is None:
                    raise SandboxExecutionError(
                        f"pod {name} state unknown after the deadline: {got.stderr.strip()[:300]}"
                    )
                if phase in {"Succeeded", "Failed"}:
                    raise SandboxExecutionError(
                        f"pod {name} finished ({phase}) but the watch never reported it"
                    )
                timed_out = True

            logs = self._kubectl("logs", name, "--tail", "-1", timeout=120)
            stdout, stderr = logs.stdout, logs.stderr
            clock.mark("logs_fetched", time.perf_counter(), exact=True)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._kubectl("delete", "pod", name, "--ignore-not-found", "--now", timeout=120)

        if reason in PLATFORM_START_FAILURES:
            raise SandboxExecutionError(
                f"{self.level.value} container never started ({reason}): {message[:300]}"
            )

        trace = parse_harness_result(
            stdout,
            level=self.level,
            exit_code=exit_code,
            wall_time_ms=elapsed_ms,
            timed_out=timed_out,
            stderr=stderr,
            expected_output=request.expected_output,
            terminated_reason=reason,
            milestones_ms=clock.ms,
            milestones_inexact=clock.inexact,
        )
        trace = require_attributable(trace, agent_caused=reason == "OOMKilled")
        self._verify_kernel(trace)
        return trace

    def _open_watch(self, name: str) -> PodWatch:
        cmd = self._base_cmd() + [
            "-n", self.namespace, "get", "pod", name, "--watch", "-o", "json",
        ]
        return KubectlPodWatch(cmd)

    def _pod_states(
        self, name: str, deadline: float
    ) -> Iterator[tuple[float, dict[str, Any], bool]]:
        """Every state the pod passes through, stamped when it arrived.

        Yields (perf_counter at arrival, pod object, whether it was the watch's
        initial state). An initial state shows where the pod already was, not
        when it got there, so milestones first seen in one are marked inexact.
        The API server closes long watches and connections drop, so the watch
        is reopened a bounded number of times; the first object after a reopen
        is again an initial state. The generator ends without a terminal state
        once the deadline passes, which the caller reads as a timeout.
        """
        reopened = 0
        watch = self._open_watch(name)
        try:
            initial = True
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    item = watch.next(timeout=min(remaining, 5.0))
                except queue.Empty:
                    continue
                if item is None:
                    reopened += 1
                    if reopened > MAX_WATCH_REOPENS:
                        raise SandboxExecutionError(
                            f"pod watch for {name} ended {reopened} times: "
                            f"{watch.error()[:300]}"
                        )
                    watch.close()
                    watch = self._open_watch(name)
                    initial = True
                    continue
                seen_at, pod = item
                yield seen_at, pod, initial
                initial = False
        finally:
            watch.close()


# A watch that keeps ending is a cluster or credential problem, not a slow pod.
MAX_WATCH_REOPENS = 5


def _progress(status: dict[str, Any]) -> dict[str, bool]:
    """Which lifecycle milestones a pod status shows as reached."""
    conditions = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
    states = [cs.get("state") or {} for cs in status.get("containerStatuses") or []]
    terminated = status.get("phase") in {"Succeeded", "Failed"} or any(
        "terminated" in st for st in states
    )
    running = terminated or any("running" in st for st in states)
    return {
        "scheduled": running or conditions.get("PodScheduled") == "True",
        "running": running,
        "terminated": terminated,
    }


class _Milestones:
    """First-observation times, in ms from the runner's start."""

    def __init__(self, origin: float) -> None:
        self.origin = origin
        self.ms: dict[str, float] = {}
        self._first_seen_initial: set[str] = set()

    def mark(self, name: str, at: float, *, exact: bool) -> None:
        if name in self.ms:
            return
        self.ms[name] = round((at - self.origin) * 1000, 3)
        if not exact:
            self._first_seen_initial.add(name)

    @property
    def inexact(self) -> tuple[str, ...]:
        # A milestone first seen in the same update as the next one was never
        # observed on its own — kubelet reported running and terminated
        # together, say — so its time is really the later milestone's.
        collapsed = {
            m for m, later in (("scheduled", "running"), ("running", "terminated"))
            if m in self.ms and self.ms[m] == self.ms.get(later)
        }
        return tuple(sorted(self._first_seen_initial | collapsed))


class PodWatch(Protocol):
    def next(self, timeout: float) -> tuple[float, dict[str, Any]] | None: ...
    def error(self) -> str: ...
    def close(self) -> None: ...


class KubectlPodWatch:
    """`kubectl get pod --watch -o json`, each object stamped on arrival.

    kubectl prints one pretty-printed JSON object per change with nothing
    between them, so the stream is split by decoding rather than by line. A
    reader thread decodes and stamps each object with perf_counter the moment
    it is complete; that stamp is what the milestones are built from. `next`
    raises queue.Empty on timeout and returns None once the stream has ended.
    """

    def __init__(self, cmd: list[str]) -> None:
        # Owned for the watch's lifetime and closed in close().
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self._stderr
        )
        self._items: queue.Queue[tuple[float, dict[str, Any]] | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stream = self._proc.stdout
        assert stream is not None
        decoder = JsonStreamDecoder()
        try:
            while chunk := stream.read1(65536):  # type: ignore[attr-defined]
                for obj in decoder.feed(chunk):
                    self._items.put((time.perf_counter(), obj))
        finally:
            self._items.put(None)

    def next(self, timeout: float) -> tuple[float, dict[str, Any]] | None:
        return self._items.get(timeout=timeout)

    def error(self) -> str:
        try:
            self._stderr.seek(0)
            return self._stderr.read().decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            return ""

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._proc.wait(timeout=10)
        self._stderr.close()


class JsonStreamDecoder:
    """Split a byte stream of concatenated JSON objects into objects."""

    def __init__(self) -> None:
        self._pending = b""
        self._text = ""
        self._decoder = json.JSONDecoder()

    def feed(self, chunk: bytes) -> list[Any]:
        data = self._pending + chunk
        # Hold back a UTF-8 sequence split across the chunk boundary.
        try:
            self._text += data.decode("utf-8")
            self._pending = b""
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data":
                raise
            self._text += data[: exc.start].decode("utf-8")
            self._pending = data[exc.start :]

        out: list[Any] = []
        while text := self._text.lstrip():
            try:
                obj, end = self._decoder.raw_decode(text)
            except json.JSONDecodeError:
                self._text = text  # incomplete; wait for the rest
                return out
            out.append(obj)
            self._text = text[end:]
        self._text = ""
        return out


def runc_pod_runner(**kwargs: Any) -> PodRunner:
    """L2: namespace and cgroup isolation, host kernel shared."""
    return PodRunner(level=IsolationLevel.L2, runtime_class=None, **kwargs)


def gvisor_pod_runner(**kwargs: Any) -> PodRunner:
    """L3: gVisor Sentry between the agent and the host kernel."""
    return PodRunner(level=IsolationLevel.L3, runtime_class=GVISOR_RUNTIME_CLASS, **kwargs)
