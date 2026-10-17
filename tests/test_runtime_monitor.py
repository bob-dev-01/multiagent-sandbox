"""The harness's run-time monitor: what it records, and what it must leave alone.

The layer ablation found that the sandbox recorded no signal for an agent that
spawns a process or runs dynamic code. These agents are written for the test
and do nothing harmful; each forbidden operation is refused by the monitor
before it takes effect, which is part of what is checked.
"""

from __future__ import annotations

import pytest

from agentfactory.contracts import AgentContract, AgentSpec
from agentfactory.validation.sandbox.base import SandboxRequest

CONTRACT = AgentContract(task_spec="monitor test", authorized_tools=())


def run(local_runner, source: str):
    spec = AgentSpec.from_source(task_id="T-monitor", source=source, contract=CONTRACT)
    return local_runner.run(SandboxRequest(spec=spec, test_input={"case": 1}))


@pytest.mark.parametrize("body, event", [
    ("return {'v': eval('1 + 1')}", ("compile", "exec")),
    ("exec('x = 1')\n    return {}", ("compile", "exec")),
    ("import builtins\n    f = getattr(builtins, 'ev' + 'al')\n    return {'v': f('2')}", ("compile", "exec")),
    ("import subprocess, sys\n    subprocess.run([sys.executable, '-c', 'pass'])\n    return {}",
     "subprocess.Popen"),
    ("import os\n    os.system('echo hi')\n    return {}", "os.system"),
    ("import pickle, collections\n"
     "    return {'v': len(pickle.loads(pickle.dumps(collections.Counter('ab'))))}",
     "pickle.find_class"),
    ("import sys\n    return {'v': sys._getframe().f_lineno}", "sys._getframe"),
])
def test_forbidden_behaviour_is_recorded_and_refused(local_runner, body, event) -> None:
    trace = run(local_runner, f"def run(task_input, tools):\n    {body}\n")
    assert any(v.startswith(event) for v in trace.runtime_violations), trace.runtime_violations
    assert trace.has_safety_violation
    assert trace.agent_output in (None, {})  # refused, so the agent never got its result


def test_a_read_outside_the_work_area_through_pathlib_is_caught(local_runner) -> None:
    # pathlib opens through io.open, which the builtins.open guard never saw.
    trace = run(local_runner,
                "import pathlib, sys\n\n\ndef run(task_input, tools):\n"
                "    return {'n': len(pathlib.Path(sys.executable).read_bytes())}\n")
    assert trace.fs_violations, trace


def test_a_secret_in_the_output_is_caught(local_runner) -> None:
    trace = run(local_runner,
                "def run(task_input, tools):\n"
                "    return {'note': \"api_key = 'abcdefgh12345678'\"}\n")
    assert any("AF201" in v for v in trace.runtime_violations)


def test_ordinary_library_work_is_left_alone(local_runner) -> None:
    source = (
        "import collections, dataclasses, datetime, json, pathlib, re, statistics\n\n\n"
        "@dataclasses.dataclass\nclass Row:\n    name: str\n    value: int\n\n\n"
        "Pair = collections.namedtuple('Pair', 'a b')\n\n\n"
        "def run(task_input, tools):\n"
        "    rows = [Row('a', 1), Row('b', 3)]\n"
        "    work = pathlib.Path('/tmp/agent/monitor-test')\n"
        "    work.mkdir(parents=True, exist_ok=True)\n"
        "    target = work / 'x.json'\n"
        "    target.write_text(json.dumps([dataclasses.asdict(r) for r in rows]))\n"
        "    back = json.loads(target.read_text())\n"
        "    target.unlink()\n"
        "    when = datetime.datetime.strptime('2026-10-17', '%Y-%m-%d').date().isoformat()\n"
        "    return {'mean': statistics.mean(r['value'] for r in back),\n"
        "            'pair': list(Pair(1, 2)), 'when': when,\n"
        "            'words': re.findall('[a-z]+', 'a b c')}\n"
    )
    trace = run(local_runner, source)
    assert not trace.has_safety_violation, (trace.runtime_violations, trace.fs_violations)
    assert trace.agent_output == {"mean": 2, "pair": [1, 2], "when": "2026-10-17",
                                  "words": ["a", "b", "c"]}


def test_udp_without_connect_and_name_lookup_are_caught(local_runner) -> None:
    # The socket subclass only refuses connect(); a datagram needs no connection,
    # and resolving a name is itself a way out.
    trace = run(local_runner,
                "import socket\n\n\ndef run(task_input, tools):\n"
                "    try:\n"
                "        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
                "        s.sendto(b'x', ('127.0.0.1', 9))\n"
                "    except OSError:\n"
                "        pass\n"
                "    try:\n"
                "        socket.getaddrinfo('localhost', 80)\n"
                "    except OSError:\n"
                "        pass\n"
                "    return {}\n")
    assert any(a.startswith("socket.sendto") for a in trace.network_attempts)
    assert any(a.startswith("socket.getaddrinfo") for a in trace.network_attempts)


def test_a_secret_in_a_tool_argument_is_caught(local_runner) -> None:
    spec = AgentSpec.from_source(
        task_id="T-monitor",
        source="def run(task_input, tools):\n"
               "    tools.write_report(path='r.txt', content='x', footer='key=rpt-svc-91c4e7a2f05b3d68')\n"
               "    return {}\n",
        contract=AgentContract(task_spec="monitor test", authorized_tools=("write_report",)),
    )
    trace = local_runner.run(SandboxRequest(spec=spec, test_input={"case": 1}))
    assert any("argument to write_report" in v for v in trace.runtime_violations)



def test_a_datagram_to_an_unresolvable_host_is_recorded(local_runner) -> None:
    # Name resolution fails before the interpreter's audit event; the agent
    # swallows the error. The attempt must still be on record.
    trace = run(local_runner,
                "import socket\n\n\ndef run(task_input, tools):\n"
                "    try:\n"
                "        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
                "        s.sendto(b'x', ('collector.invalid', 8125))\n"
                "    except OSError:\n"
                "        pass\n"
                "    return {}\n")
    assert any("collector.invalid" in a for a in trace.network_attempts), trace.network_attempts
