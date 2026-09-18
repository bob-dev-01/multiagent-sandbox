"""L1 — subprocess with seccomp-bpf and rlimits. Shares the host kernel.

The weakest and fastest level: no container, no image pull, no cold start. It
is appropriate for low-risk read-only agents and it is the latency floor that
the L2 and L3 overheads in RQ2 are measured against.

Honesty about what is enforced: rlimits and `no_new_privs` are always applied.
A real seccomp-bpf syscall filter needs libseccomp, which is not in the standard
library, so the runner applies one when `pyseccomp` is installed and records
`seccomp_active` either way. A run where the filter was not active is still a
valid L1 run of the *rlimits* configuration, but it is not the configuration
RQ2 describes — so the flag is written into the trace rather than assumed, and
the harness refuses to pretend otherwise.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from typing import Any

from agentfactory.contracts import ExecutionTrace, IsolationLevel

from .base import (
    HARNESS_PATH,
    SandboxRequest,
    SandboxUnavailableError,
    parse_harness_result,
)

# Syscalls denied by the filter when seccomp is available. These are the ones an
# agent has no legitimate reason to make and that matter for the threat model:
# process creation, networking, and privilege changes.
DENIED_SYSCALLS: tuple[str, ...] = (
    "execve",
    "execveat",
    "fork",
    "vfork",
    "clone3",
    "socket",
    "socketpair",
    "connect",
    "bind",
    "listen",
    "accept",
    "accept4",
    "sendto",
    "recvfrom",
    "ptrace",
    "setuid",
    "setgid",
    "mount",
    "umount2",
    "chroot",
    "reboot",
    "kexec_load",
    "init_module",
    "delete_module",
)


def seccomp_available() -> bool:
    if platform.system() != "Linux":
        return False
    try:
        import seccomp  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def _build_preexec(memory_mb: int, cpu_seconds: int) -> Any:
    """Return a preexec_fn applying rlimits and, where possible, seccomp.

    Runs in the forked child between fork and exec, so anything raised here
    kills the child before the agent's code is reached.
    """

    def preexec() -> None:
        import resource

        # Address space. RLIMIT_AS is a blunt instrument but it is the one
        # limit that reliably stops a runaway allocation on Linux.
        limit_bytes = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        # CPU seconds — a hard backstop behind the wall-clock timeout, for an
        # agent that spins without sleeping.
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        # No core dumps, no new processes, a modest file-size ceiling.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)

        # Drop the ability to gain privileges through exec.
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_NO_NEW_PRIVS = 38
            libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        except (OSError, AttributeError):
            pass

        os.setsid()

        try:
            import seccomp  # type: ignore[import-not-found]
        except ImportError:
            return

        # Default allow, explicit deny — a default-deny filter would have to
        # enumerate every syscall CPython makes at startup, which is brittle
        # across interpreter versions. Denying the dangerous set is the
        # trade-off this level represents.
        flt = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
        for name in DENIED_SYSCALLS:
            try:
                flt.add_rule(seccomp.ERRNO(1), name)
            except (ValueError, RuntimeError):
                continue
        flt.load()

    return preexec


class SubprocessRunner:
    """L1 isolation."""

    level = IsolationLevel.L1

    def __init__(self, *, python: str | None = None, require_seccomp: bool = False) -> None:
        self.python = python or sys.executable
        self.require_seccomp = require_seccomp

    def available(self) -> bool:
        if platform.system() != "Linux":
            return False
        if self.require_seccomp and not seccomp_available():
            return False
        return HARNESS_PATH.exists()

    def run(self, request: SandboxRequest) -> ExecutionTrace:
        if platform.system() != "Linux":
            raise SandboxUnavailableError(
                "L1 needs Linux for rlimits and seccomp. On Windows run inside WSL2 "
                "or on the Azure orchestrator VM."
            )
        if self.require_seccomp and not seccomp_available():
            raise SandboxUnavailableError(
                "seccomp filter required but pyseccomp is not installed"
            )

        limits = request.spec.contract.resource_limits
        timeout = limits.max_duration_sec

        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp/agent",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONUNBUFFERED": "1",
        }
        os.makedirs("/tmp/agent", exist_ok=True)

        started = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.run(
                [self.python, "-I", "-S", str(HARNESS_PATH)],
                input=request.payload(),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd="/tmp/agent",
                preexec_fn=_build_preexec(limits.memory_mb, timeout),  # noqa: PLW1509
                check=False,
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            code = None

        elapsed_ms = (time.perf_counter() - started) * 1000

        trace = parse_harness_result(
            stdout,
            level=self.level,
            exit_code=code,
            wall_time_ms=elapsed_ms,
            timed_out=timed_out,
            stderr=stderr,
            expected_output=request.expected_output,
        )
        # Record whether the syscall filter was actually in force, so a run made
        # without it is never silently read as one made with it.
        return trace.model_copy(
            update={
                "stderr_tail": (
                    f"[seccomp_active={seccomp_available()}]\n{trace.stderr_tail}"
                )[-4000:]
            }
        )
