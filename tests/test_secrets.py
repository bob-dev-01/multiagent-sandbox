"""Key resolution: environment first, Key Vault second, never at construction."""

from __future__ import annotations

import subprocess

import pytest

from agentfactory import secrets


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    secrets.anthropic_api_key.cache_clear()
    monkeypatch.setattr(secrets, "_from_sdk", lambda vault: None)
    yield
    secrets.anthropic_api_key.cache_clear()


def test_environment_wins_and_the_vault_is_not_asked(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    monkeypatch.setattr(secrets, "_from_cli", lambda vault: pytest.fail("vault was asked"))
    assert secrets.anthropic_api_key() == "from-env"


def test_falls_back_to_the_vault(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_KEY_VAULT_NAME", "some-kv")
    seen = []
    monkeypatch.setattr(secrets, "_from_cli", lambda vault: seen.append(vault) or "from-kv")
    assert secrets.anthropic_api_key() == "from-kv"
    assert seen == ["some-kv"]


def test_a_failed_vault_read_names_the_vault_not_a_value(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(secrets.shutil, "which", lambda name: "az")
    monkeypatch.setattr(
        secrets.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "Forbidden"),
    )
    with pytest.raises(secrets.SecretUnavailableError, match="agentfac-kv.*Forbidden"):
        secrets.anthropic_api_key()


def test_constructing_the_judge_does_not_touch_the_vault(monkeypatch) -> None:
    from agentfactory.judge.correctness_judge import CorrectnessJudge

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(secrets, "_from_cli", lambda vault: pytest.fail("vault was asked"))
    CorrectnessJudge()
