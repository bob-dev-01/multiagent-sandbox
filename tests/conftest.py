"""Shared fixtures.

`LocalHarnessRunner` deserves a word. It runs the harness as a plain subprocess
with no rlimits, no seccomp and no container — so it is *not* an isolation level
and must never appear in an experiment. It exists so the harness's own logic
(tool authorization, network recording, filesystem guard, report format) can be
tested on any OS including Windows, where L1 cannot run at all. It reports its
traces as L1 because `ExecutionTrace` requires a level; the guard below is what
keeps it out of real runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

import pytest

from agentfactory.contracts import (
    AgentContract,
    AgentSpec,
    ExecutionTrace,
    IsolationLevel,
    ResourceLimits,
)
from agentfactory.validation.sandbox.base import (
    HARNESS_PATH,
    SandboxRequest,
    parse_harness_result,
)


class LocalHarnessRunner:
    """Test-only runner. No isolation whatsoever."""

    level = IsolationLevel.L1

    def __init__(self) -> None:
        if os.environ.get("AGENTFACTORY_ENV") not in (None, "", "local", "test"):
            raise RuntimeError(
                "LocalHarnessRunner provides no isolation and must not run outside tests"
            )

    def available(self) -> bool:
        return HARNESS_PATH.exists()

    def run(self, request: SandboxRequest) -> ExecutionTrace:
        started = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.run(
                [sys.executable, str(HARNESS_PATH)],
                input=request.payload(),
                capture_output=True,
                text=True,
                timeout=request.spec.contract.resource_limits.max_duration_sec,
                check=False,
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout, stderr, code = "", "", None

        return parse_harness_result(
            stdout,
            level=self.level,
            exit_code=code,
            wall_time_ms=(time.perf_counter() - started) * 1000,
            timed_out=timed_out,
            stderr=stderr,
            expected_output=request.expected_output,
        )


@pytest.fixture
def local_runner() -> LocalHarnessRunner:
    return LocalHarnessRunner()


@pytest.fixture
def safe_contract() -> AgentContract:
    return AgentContract(
        task_spec="Count ERROR lines per service in a log file.",
        authorized_tools=("read_log_file",),
        resource_limits=ResourceLimits(cpu_cores=1, memory_mb=256, max_duration_sec=30),
    )


def make_spec(source: str, contract: AgentContract, task_id: str = "T001") -> AgentSpec:
    return AgentSpec.from_source(
        task_id=task_id, source=source, contract=contract, generator_model="test"
    )


SAFE_AGENT = '''
def run(task_input, tools):
    data = tools.read_log_file(path="app.log")
    counts = {}
    for line in data["lines"]:
        if "ERROR" in line:
            parts = line.split()
            service = parts[3] if len(parts) > 3 else "unknown"
            counts[service] = counts.get(service, 0) + 1
    return {"error_counts": counts, "total": sum(counts.values())}
'''

UNSAFE_IMPORT_AGENT = '''
import subprocess

def run(task_input, tools):
    subprocess.Popen(["/bin/sh", "-c", "cat /etc/passwd"])
    return {"ok": True}
'''

UNSAFE_OBFUSCATED_AGENT = '''
import os

def run(task_input, tools):
    fn = getattr(os, "system")
    fn("cat /etc/shadow")
    return {"ok": True}
'''

UNSAFE_NETWORK_AGENT = '''
import socket

def run(task_input, tools):
    s = socket.socket()
    s.connect(("attacker.example", 443))
    return {"ok": True}
'''

INCORRECT_AGENT = '''
def run(task_input, tools):
    tools.read_log_file(path="app.log")
    return {"error_counts": {}, "total": 999}
'''

UNDECLARED_TOOL_AGENT = '''
def run(task_input, tools):
    return tools.create_incident_ticket(title="pwned")
'''


@pytest.fixture
def agents(safe_contract: AgentContract) -> dict[str, Any]:
    return {
        "safe": make_spec(SAFE_AGENT, safe_contract),
        "unsafe_import": make_spec(UNSAFE_IMPORT_AGENT, safe_contract),
        "unsafe_obfuscated": make_spec(UNSAFE_OBFUSCATED_AGENT, safe_contract),
        "unsafe_network": make_spec(UNSAFE_NETWORK_AGENT, safe_contract),
        "incorrect": make_spec(INCORRECT_AGENT, safe_contract),
        "undeclared_tool": make_spec(UNDECLARED_TOOL_AGENT, safe_contract),
    }
