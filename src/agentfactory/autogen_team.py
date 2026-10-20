"""The Agent Factory as a team of AutoGen agents.

The research plan places the Orchestrator and the Task Executor inside a
Microsoft AutoGen multi-agent system (architecture.md section 3). Here they are
two AutoGen Core agents in one runtime, and every hand-over between them is an
AutoGen message:

    caller --TaskRequest--> orchestrator --DeployRequest--> executor
           <--TaskResult---              <--ExecutionRecord--

What each agent decides is unchanged: the orchestrator runs the same
reuse-or-generate loop and verdict routing as `AgentFactory`, and the executor
the same fail-closed gate and sandboxed run as `TaskExecutor`. Both are
deterministic code, not LLM conversation — routing a safety verdict is not a
judgement a language model should make. The model calls (generator, judge)
stay inside the orchestrator, on the orchestrator's side of every sandbox.

Generated micro-agents are not members of the runtime. They have no network
and no model access (section 10.1), so they cannot exchange messages; the
executor runs them as tools, one sandboxed call at a time. Data passes between
micro-agents only through the orchestrator.

`AutoGenAgentFactory` keeps the synchronous `handle_task` interface, so the
integration harness drives the team exactly as it drives a plain factory.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any

from autogen_core import (
    JSON_DATA_CONTENT_TYPE,
    AgentId,
    DefaultInterventionHandler,
    MessageContext,
    RoutedAgent,
    SingleThreadedAgentRuntime,
    message_handler,
)

from agentfactory.contracts import AgentSpec, Verdict
from agentfactory.executor import ExecutionRecord, TaskExecutor
from agentfactory.orchestrator.factory import AgentFactory, TaskResult, TaskSpecification

ORCHESTRATOR = "orchestrator"
EXECUTOR = "task_executor"


@dataclass(frozen=True)
class TaskRequest:
    """A task submitted to the factory."""

    task: TaskSpecification


@dataclass(frozen=True)
class DeployRequest:
    """The orchestrator asks the executor to run an approved agent."""

    spec: AgentSpec
    task: TaskSpecification
    approved_sha256: str
    verdict: Verdict
    reused: bool
    run_id: str | None


class _SummarySerializer:
    """What AutoGen's event log records for a message: identifiers, not payloads.

    The runtime serialises every message it delivers for its log. Ours carry
    agent source code and pydantic models, which the default dataclass
    serialiser cannot encode; the log needs only enough to follow the
    conversation. The runtime here is in-process, so nothing is ever
    deserialised.
    """

    data_content_type = JSON_DATA_CONTENT_TYPE

    def __init__(self, cls: type, summarise: Any) -> None:
        self.type_name = cls.__name__
        self._summarise = summarise

    def serialize(self, message: Any) -> bytes:
        return json.dumps(self._summarise(message), default=str).encode("utf-8")

    def deserialize(self, payload: bytes) -> Any:
        raise NotImplementedError("messages are summarised for the log, not transported")


SERIALIZERS = (
    _SummarySerializer(TaskRequest, lambda m: {
        "task_id": m.task.task_id, "risk_tier": m.task.risk_tier,
    }),
    _SummarySerializer(DeployRequest, lambda m: {
        "task_id": m.task.task_id, "agent_id": str(m.spec.agent_id),
        "approved_sha256": m.approved_sha256, "verdict": m.verdict.value, "reused": m.reused,
    }),
)


class ExecutorAgent(RoutedAgent):
    """The Task Executor. Gate, sandboxed run, record — `TaskExecutor.execute`.

    A refusal or an infrastructure error is raised, and AutoGen delivers it to
    the sender, where the orchestrator routes it as before.
    """

    def __init__(self, executor: TaskExecutor) -> None:
        super().__init__("Deploys approved micro-agents into a sandbox and records the run.")
        self.executor = executor

    @message_handler
    async def on_deploy(self, message: DeployRequest, ctx: MessageContext) -> ExecutionRecord:
        return await asyncio.to_thread(
            self.executor.execute,
            message.spec,
            message.task,
            approved_sha256=message.approved_sha256,
            verdict=message.verdict,
            reused=message.reused,
            run_id=message.run_id,
        )


class _ExecutorByMessage:
    """Stands in for the factory's executor: each call becomes a DeployRequest.

    The factory loop is synchronous and runs in a worker thread; the message
    is sent on the runtime's event loop and the thread waits for the reply.
    """

    def __init__(self, sender: RoutedAgent, loop: asyncio.AbstractEventLoop) -> None:
        self._sender = sender
        self._loop = loop

    def execute(
        self,
        spec: AgentSpec,
        task: TaskSpecification,
        *,
        approved_sha256: str,
        verdict: Verdict,
        reused: bool = False,
        run_id: str | None = None,
    ) -> ExecutionRecord:
        request = DeployRequest(spec, task, approved_sha256, verdict, reused, run_id)
        future = asyncio.run_coroutine_threadsafe(
            self._sender.send_message(request, AgentId(EXECUTOR, "default")), self._loop
        )
        return future.result()


class OrchestratorAgent(RoutedAgent):
    """The Orchestrator. Reuse or generate, validate, route — `AgentFactory`."""

    def __init__(self, factory: AgentFactory) -> None:
        super().__init__("Decides reuse or generation, validates, and routes each verdict.")
        self.factory = factory

    @message_handler
    async def on_task(self, message: TaskRequest, ctx: MessageContext) -> TaskResult:
        # Bound here, on the runtime's loop, so the executor hand-over is a
        # message to the executor agent rather than a direct call.
        self.factory.executor = _ExecutorByMessage(self, asyncio.get_running_loop())  # type: ignore[assignment]
        return await asyncio.to_thread(self.factory.handle_task, message.task)


class _MessageCounter(DefaultInterventionHandler):
    """Counts every message the runtime delivers, by type and recipient."""

    def __init__(self, on_message: Any = None) -> None:
        self.counts: Counter[str] = Counter()
        # Optional observer, e.g. the demo's live console: (message type, recipient).
        self.on_message = on_message

    async def on_send(self, message: Any, *, message_context: MessageContext,
                      recipient: AgentId) -> Any:
        self.counts[f"{type(message).__name__}->{recipient.type}"] += 1
        if self.on_message is not None:
            self.on_message(type(message).__name__, recipient.type)
        return message


class AutoGenAgentFactory:
    """A running AutoGen runtime with the two agents, behind `handle_task`.

    Takes a fully built `AgentFactory` — generator, pipeline, registry,
    embedder, judge, event log and a real `TaskExecutor` — and splits it across
    the two agents. One instance per seed, like the factory it wraps.
    """

    def __init__(self, factory: AgentFactory, *, on_message: Any = None) -> None:
        if factory.executor is None:
            raise ValueError("the AutoGen team needs a factory with a Task Executor")
        self._task_executor = factory.executor
        self._factory = factory
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                        name="autogen-runtime")
        self._thread.start()
        self._counter = _MessageCounter(on_message)
        self._runtime = SingleThreadedAgentRuntime(intervention_handlers=[self._counter])
        self._call(self._start())

    # The harness checks this before running anything.
    @property
    def executor(self) -> TaskExecutor:
        return self._task_executor

    @property
    def generator(self) -> Any:
        return self._factory.generator

    @property
    def message_counts(self) -> dict[str, int]:
        """Messages delivered so far, e.g. {"DeployRequest->task_executor": 12}."""
        return dict(self._counter.counts)

    def _call(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _start(self) -> None:
        await ExecutorAgent.register(self._runtime, EXECUTOR,
                                     lambda: ExecutorAgent(self._task_executor))
        await OrchestratorAgent.register(self._runtime, ORCHESTRATOR,
                                         lambda: OrchestratorAgent(self._factory))
        # After registration, which installs the default serialisers.
        self._runtime.add_message_serializer(list(SERIALIZERS))
        self._runtime.start()

    def handle_task(self, task: TaskSpecification) -> TaskResult:
        return self._call(
            self._runtime.send_message(TaskRequest(task), AgentId(ORCHESTRATOR, "default"))
        )

    def close(self) -> None:
        if not self._loop.is_running():
            return
        self._call(self._runtime.stop_when_idle())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        # Hand the real executor back, so the wrapped factory is usable alone.
        self._factory.executor = self._task_executor

    def __enter__(self) -> AutoGenAgentFactory:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
