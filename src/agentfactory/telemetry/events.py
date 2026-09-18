"""JSONL event log with schema-on-write validation.

Every record is validated by pydantic at write time and rejected immediately if
malformed. That is deliberate: a silently corrupted evaluation dataset is far
more expensive than a crashed run, because it is discovered months later when
the numbers stop adding up.

Each record also carries the provenance fields that let any metric be traced
back to the exact code, model and configuration that produced it
(architecture.md section 11.1).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from agentfactory.contracts import SCHEMA_VERSION, IsolationLevel, ValidationLayer, Verdict


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    AGENT_GENERATED = "agent_generated"
    LAYER_COMPLETED = "layer_completed"
    VERDICT_EMITTED = "verdict_emitted"
    AGENT_REGISTERED = "agent_registered"
    AGENT_REUSED = "agent_reused"
    AGENT_DEPLOYED = "agent_deployed"
    EXECUTION_COMPLETED = "execution_completed"
    ESCALATED = "escalated"
    REGENERATION_REQUESTED = "regeneration_requested"
    RUN_FAILED = "run_failed"
    RUN_COMPLETED = "run_completed"


def _git_commit() -> str:
    """Current commit, or 'unknown' outside a checkout.

    Recorded on every event so results can be tied to the code that produced
    them without relying on anyone remembering to write it down.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            cwd=Path(__file__).resolve().parent,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


_GIT_COMMIT = _git_commit()


class Event(BaseModel):
    """One line of the JSONL log."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Provenance
    run_id: str
    git_commit: str = Field(default_factory=lambda: _GIT_COMMIT)

    # Subject
    task_id: str | None = None
    agent_id: uuid.UUID | None = None
    code_sha256: str | None = None

    # Configuration under test
    isolation_level: IsolationLevel | None = None
    layer: ValidationLayer | None = None
    model: str | None = None
    replicate: int | None = None

    # Outcome
    verdict: Verdict | None = None
    passed: bool | None = None
    score: float | None = None
    latency_ms: float | None = None

    # Anything layer-specific. Kept free-form on purpose: constraining it would
    # mean bumping the schema for every new diagnostic field.
    detail: dict[str, Any] = Field(default_factory=dict)


class EventLog:
    """Append-only JSONL writer, safe across threads.

    Use as a context manager so the handle is always closed:

        with EventLog(Path("runs/exp1"), run_id="exp1") as log:
            log.emit(EventType.RUN_STARTED)
    """

    def __init__(self, directory: Path | str, run_id: str, *, filename: str | None = None) -> None:
        self.run_id = run_id
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / (filename or f"{run_id}.jsonl")
        self._lock = threading.Lock()
        self._handle = self.path.open("a", encoding="utf-8")
        self._count = 0

    @property
    def records_written(self) -> int:
        return self._count

    def emit(self, event_type: EventType, **fields: Any) -> Event:
        """Validate and append one record. Raises on a malformed event."""
        event = Event(event_type=event_type, run_id=self.run_id, **fields)
        line = event.model_dump_json(exclude_none=True)
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._count += 1
        return event

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: Path | str, *, strict: bool = True) -> Iterator[Event]:
    """Replay a JSONL log.

    With ``strict`` the first bad line raises, which is what the metrics
    pipeline wants — a log that cannot be parsed in full is not a log you should
    compute statistics from. Set ``strict=False`` to skip damaged lines when
    triaging a crashed run.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield Event.model_validate(json.loads(raw))
            except Exception as exc:
                if strict:
                    raise ValueError(f"{path}:{lineno} is not a valid event: {exc}") from exc
                continue


def read_all(directory: Path | str, *, strict: bool = True) -> list[Event]:
    """Every event across every JSONL file in a directory, in timestamp order."""
    directory = Path(directory)
    events = [
        event
        for file in sorted(directory.glob("*.jsonl"))
        for event in read_events(file, strict=strict)
    ]
    events.sort(key=lambda e: e.timestamp)
    return events
