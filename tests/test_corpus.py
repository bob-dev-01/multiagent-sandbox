"""Corpus integrity.

These tests guard the instrument. Every classification metric in the study is
computed against these labels, so a corpus that silently drifts produces numbers
that look fine and mean nothing.
"""

from __future__ import annotations

import pytest
import yaml

from agentfactory.corpus import (
    EXPECTED_DISTRIBUTION,
    CorpusError,
    load_corpus,
    load_task,
    manifest,
)
from agentfactory.metrics.core import GroundTruth


def test_corpus_loads() -> None:
    tasks = load_corpus()
    assert tasks
    assert all(len(t.agents) == 10 for t in tasks)


def test_every_task_has_the_pre_registered_distribution() -> None:
    from collections import Counter

    for task in load_corpus():
        counts = Counter(a.label for a in task.agents)
        assert counts == Counter(EXPECTED_DISTRIBUTION), task.task_id


def test_task_ids_are_unique() -> None:
    ids = [t.task_id for t in load_corpus()]
    assert len(ids) == len(set(ids))


def test_unsafe_variants_name_their_failure_mode() -> None:
    for task in load_corpus():
        for agent in task.agents:
            if agent.label is GroundTruth.SAFE_CORRECT:
                assert agent.failure_mode is None, agent.agent_key
            else:
                assert agent.failure_mode, agent.agent_key


def test_every_agent_compiles() -> None:
    """A corpus agent that does not parse would be a Layer 1 finding about the
    corpus, not about the agent under test."""
    import ast

    for task in load_corpus():
        for agent in task.agents:
            ast.parse(agent.source)


def test_specs_are_content_addressed() -> None:
    for task in load_corpus():
        for agent, spec in task.specs():
            assert spec.code_sha256 == agent.source_sha256
            assert spec.task_id == task.task_id


def test_manifest_is_stable_across_loads() -> None:
    assert manifest(load_corpus())["corpus_sha256"] == manifest(load_corpus())["corpus_sha256"]


def test_manifest_changes_when_an_agent_changes(tmp_path) -> None:
    tasks = load_corpus()
    before = manifest(tasks)["corpus_sha256"]
    edited = [
        t.__class__(**{**t.__dict__, "agents": tuple(
            a.__class__(**{**a.__dict__, "source": a.source + "\n# edit\n"}) if i == 0 else a
            for i, a in enumerate(t.agents)
        )}) if t is tasks[0] else t
        for t in tasks
    ]
    assert manifest(edited)["corpus_sha256"] != before


def test_bad_distribution_is_rejected(tmp_path) -> None:
    src = "def run(task_input, tools):\n    return {}\n"
    bad = {
        "task_id": "TX",
        "domain": "incident_response",
        "description": "x",
        "available_tools": ["read_log_file"],
        "agents": [
            {"variant": f"v{i}", "label": "safe_correct", "failure_mode": None, "source": src}
            for i in range(10)
        ],
    }
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(CorpusError, match="distribution"):
        load_task(path)


def test_unknown_domain_is_rejected(tmp_path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump({
            "task_id": "TX", "domain": "made_up", "description": "x",
            "available_tools": [], "agents": [],
        }),
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="domain"):
        load_task(path)


def test_agent_without_entry_point_is_rejected(tmp_path) -> None:
    agents = [
        {"variant": f"v{i}", "label": label, "failure_mode": fm, "source": "x = 1\n"}
        for i, (label, fm) in enumerate(
            [("safe_correct", None)] * 5
            + [("unsafe_only", "m")] * 2
            + [("incorrect_only", "m")] * 2
            + [("both", "m")]
        )
    ]
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump({
            "task_id": "TX", "domain": "incident_response", "description": "x",
            "available_tools": [], "agents": agents,
        }),
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="run"):
        load_task(path)


def test_task_without_expected_output_is_rejected(tmp_path) -> None:
    agents = [
        {"variant": f"v{i}", "label": label, "failure_mode": fm,
         "source": "def run(task_input, tools):\n    return {}\n"}
        for i, (label, fm) in enumerate(
            [("safe_correct", None)] * 5
            + [("unsafe_only", "m")] * 2
            + [("incorrect_only", "m")] * 2
            + [("both", "m")]
        )
    ]
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump({
            "task_id": "TX", "domain": "incident_response", "description": "x",
            "available_tools": ["read_log_file"], "agents": agents,
        }),
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="expected_output"):
        load_task(path)


@pytest.mark.parametrize("task", load_corpus(), ids=lambda t: t.task_id)
def test_every_task_passes_the_corpus_check(task) -> None:
    """Labels, expected output and tool data agree; see agentfactory.corpus_check.

    The first version of this corpus had expected_output carrying only a count.
    Three T001 variants labelled correct keyed their counts by the wrong log
    field, and a day/month swap and a fabricated mapping both passed — the
    automatic check could not see what the labels claimed. Unsafe-labelled
    agents are examined statically and never executed here.
    """
    from agentfactory.corpus_check import check_task

    report = check_task(task)
    assert report.ok, report.problems


def test_no_two_agents_share_code() -> None:
    from collections import Counter

    shas = Counter(a.source_sha256 for t in load_corpus() for a in t.agents)
    assert max(shas.values()) == 1


def test_fixtures_for_tools_the_task_lacks_are_rejected(tmp_path) -> None:
    agents = [
        {"variant": f"v{i}", "label": label, "failure_mode": fm,
         "source": "def run(task_input, tools):\n    return {}\n"}
        for i, (label, fm) in enumerate(
            [("safe_correct", None)] * 5
            + [("unsafe_only", "m")] * 2
            + [("incorrect_only", "m")] * 2
            + [("both", "m")]
        )
    ]
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump({
            "task_id": "TX", "domain": "incident_response", "description": "x",
            "available_tools": ["read_log_file"], "expected_output": {"a": 1, "b": 2},
            "tool_fixtures": {"read_csv": {"rows": []}}, "agents": agents,
        }),
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="read_csv"):
        load_task(path)


def test_manifest_changes_when_a_label_or_expected_output_changes() -> None:
    import dataclasses

    tasks = load_corpus()
    before = manifest(tasks)["corpus_sha256"]
    t0 = tasks[0]
    relabelled = dataclasses.replace(
        t0, agents=(dataclasses.replace(t0.agents[0], label=GroundTruth.BOTH), *t0.agents[1:])
    )
    assert manifest([relabelled, *tasks[1:]])["corpus_sha256"] != before
    changed = dataclasses.replace(t0, expected_output={**t0.expected_output, "extra": 1})
    assert manifest([changed, *tasks[1:]])["corpus_sha256"] != before
