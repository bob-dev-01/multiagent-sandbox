"""L2 — ephemeral container. Namespace isolation, shared kernel.

One container per validation, destroyed after the verdict. No warm pool: a
reused container carries state between agents, and state that crosses the
boundary between a malicious agent and the next one is exactly what this level
exists to prevent. The cost is cold-start latency, which is the quantity RQ2
measures (and the one the L2 budget of p50 < 3 s is most at risk from — see
OQ-4 in architecture.md).

Resource limits come from the agent's contract, not a fixed ceiling, so an agent
declaring 256 MB is held to 256 MB rather than to whatever the image default is.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tarfile
import time
import uuid
from typing import Any

from agentfactory.contracts import ExecutionTrace, IsolationLevel

from .base import (
    HARNESS_PATH,
    SandboxRequest,
    SandboxUnavailableError,
    parse_harness_result,
)

DEFAULT_IMAGE = "python:3.12-slim"
WORKDIR = "/workspace"


def _harness_tar() -> bytes:
    """The harness as a tar stream, for injection into a running container."""
    data = HARNESS_PATH.read_bytes()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name="harness.py")
        info.size = len(data)
        info.mode = 0o444
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class DockerRunner:
    """L2 via the local Docker daemon.

    Used for local development and on the orchestrator VM. The Azure-native
    variant is `AciRunner` below; both present the same interface so the
    experiment does not care which one produced a trace.
    """

    level = IsolationLevel.L2

    def __init__(self, image: str = DEFAULT_IMAGE, *, runtime: str | None = None) -> None:
        self.image = image
        # `runtime` is what makes this class reusable for L3 on a host where
        # runsc is registered as a Docker runtime.
        self.runtime = runtime

    def _client(self) -> Any:
        try:
            import docker
        except ImportError as exc:
            raise SandboxUnavailableError("the docker package is not installed") from exc
        try:
            client = docker.from_env()
            client.ping()
        except Exception as exc:  # noqa: BLE001 — docker raises a wide family here
            raise SandboxUnavailableError(f"Docker daemon is not reachable: {exc}") from exc
        return client

    def available(self) -> bool:
        try:
            self._client()
        except SandboxUnavailableError:
            return False
        return True

    def run(self, request: SandboxRequest) -> ExecutionTrace:
        client = self._client()
        limits = request.spec.contract.resource_limits
        name = f"af-l2-{uuid.uuid4().hex[:12]}"

        kwargs: dict[str, Any] = {
            "image": self.image,
            "name": name,
            "command": ["python", "-I", "-S", f"{WORKDIR}/harness.py"],
            "detach": True,
            "stdin_open": True,
            "working_dir": WORKDIR,
            # Limits from the contract (OQ-15).
            "mem_limit": f"{limits.memory_mb}m",
            "memswap_limit": f"{limits.memory_mb}m",
            "nano_cpus": int(limits.cpu_cores * 1_000_000_000),
            "pids_limit": 64,
            # Containment.
            "network_disabled": not limits.network_access,
            "read_only": True,
            "tmpfs": {"/tmp/agent": "rw,size=32m,mode=1777"},
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "user": "65534:65534",
            "environment": {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONUNBUFFERED": "1",
                "HOME": "/tmp/agent",
            },
        }
        if self.runtime:
            kwargs["runtime"] = self.runtime

        container = client.containers.create(**kwargs)
        started = time.perf_counter()
        timed_out = False
        stdout = stderr = ""
        exit_code: int | None = None

        try:
            container.put_archive(WORKDIR, _harness_tar())
            container.start()

            socket = container.attach_socket(params={"stdin": 1, "stream": 1})
            socket._sock.sendall(request.payload().encode("utf-8"))  # noqa: SLF001
            socket._sock.shutdown(1)  # noqa: SLF001 — signal EOF to the harness

            try:
                result = container.wait(timeout=limits.max_duration_sec + 5)
                exit_code = int(result.get("StatusCode", -1))
            except Exception:  # noqa: BLE001 — docker surfaces timeouts variously
                timed_out = True
                container.kill()

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            # Best effort: a leaked container costs money but must not mask
            # the result we already have.
            with contextlib.suppress(Exception):
                container.remove(force=True)

        return parse_harness_result(
            stdout,
            level=self.level,
            exit_code=exit_code,
            wall_time_ms=elapsed_ms,
            timed_out=timed_out,
            stderr=stderr,
            expected_output=request.expected_output,
        )


class AciRunner:
    """L2 via Azure Container Instances, one ephemeral container group per run.

    Driven through the az CLI rather than the management SDK so that the
    container group definition stays legible and matches what the Bicep
    templates create. Provisioning dominates the latency here; measure it before
    trusting the L2 budget.
    """

    level = IsolationLevel.L2

    def __init__(
        self,
        *,
        resource_group: str,
        subnet_id: str | None = None,
        image: str = DEFAULT_IMAGE,
        location: str = "centralindia",
    ) -> None:
        self.resource_group = resource_group
        self.subnet_id = subnet_id
        self.image = image
        self.location = location

    def available(self) -> bool:
        return shutil.which("az") is not None

    def run(self, request: SandboxRequest) -> ExecutionTrace:
        if not self.available():
            raise SandboxUnavailableError("az CLI not found on PATH")

        limits = request.spec.contract.resource_limits
        name = f"af-l2-{uuid.uuid4().hex[:12]}"
        # The harness and the job both travel as base64 blobs inside the
        # command, so the container needs no volume mount and no custom image
        # in a registry. The harness reads the job from the file rather than
        # stdin, because ACI gives no way to write to a started container.
        import base64

        harness_b64 = base64.b64encode(HARNESS_PATH.read_bytes()).decode()
        job_b64 = base64.b64encode(request.payload().encode("utf-8")).decode()
        bootstrap = (
            "import base64,os,sys;"
            f"open('/tmp/h.py','wb').write(base64.b64decode('{harness_b64}'));"
            f"open('/tmp/job.json','wb').write(base64.b64decode('{job_b64}'));"
            "os.makedirs('/tmp/agent',exist_ok=True);"
            "os.environ['AGENTFACTORY_JOB_FILE']='/tmp/job.json';"
            "os.execve(sys.executable,[sys.executable,'-I','-S','/tmp/h.py'],os.environ)"
        )

        create = [
            "az", "container", "create",
            "--resource-group", self.resource_group,
            "--name", name,
            "--image", self.image,
            "--location", self.location,
            "--cpu", str(max(1, int(limits.cpu_cores))),
            "--memory", str(max(0.5, limits.memory_mb / 1024)),
            "--restart-policy", "Never",
            "--os-type", "Linux",
            "--command-line", f"python -c \"{bootstrap}\"",
            "--output", "json",
        ]
        if self.subnet_id:
            create += ["--subnet", self.subnet_id]
        else:
            create += ["--ip-address", "None"]

        started = time.perf_counter()
        timed_out = False
        stdout = stderr = ""
        exit_code: int | None = None

        try:
            subprocess.run(
                create, capture_output=True, text=True, timeout=300, check=True
            )
            deadline = time.monotonic() + limits.max_duration_sec + 120
            while time.monotonic() < deadline:
                state = subprocess.run(
                    ["az", "container", "show", "-g", self.resource_group, "-n", name,
                     "--query", "containers[0].instanceView.currentState", "-o", "json"],
                    capture_output=True, text=True, timeout=60, check=False,
                )
                try:
                    current = json.loads(state.stdout or "{}")
                except json.JSONDecodeError:
                    current = {}
                if current.get("state") == "Terminated":
                    exit_code = current.get("exitCode")
                    break
                time.sleep(2)
            else:
                timed_out = True

            logs = subprocess.run(
                ["az", "container", "logs", "-g", self.resource_group, "-n", name],
                capture_output=True, text=True, timeout=120, check=False,
            )
            stdout, stderr = logs.stdout, logs.stderr
        except subprocess.CalledProcessError as exc:
            raise SandboxUnavailableError(f"ACI create failed: {exc.stderr}") from exc
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            subprocess.run(
                ["az", "container", "delete", "-g", self.resource_group, "-n", name, "--yes"],
                capture_output=True, text=True, timeout=180, check=False,
            )

        return parse_harness_result(
            stdout,
            level=self.level,
            exit_code=exit_code,
            wall_time_ms=elapsed_ms,
            timed_out=timed_out,
            stderr=stderr,
            expected_output=request.expected_output,
        )
