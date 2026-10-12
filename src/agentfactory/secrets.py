"""Where the Anthropic API key comes from.

In order: the environment (`ANTHROPIC_API_KEY`, possibly from .env), then the
project Key Vault. Key Vault is the deployed design — the orchestrator VM reads
it with its managed identity (architecture.md section 10.2) — and it works from
a workstation too, through the signed-in `az` session, so the key never has to
sit in a file on the laptop.

The key is fetched lazily, at the first model call, and only into this
process's memory. It is never logged, printed or written anywhere. Nothing here
runs in the sandbox, which has no identity and no route to the vault.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache

ENV_VAR = "ANTHROPIC_API_KEY"
SECRET_NAME = "anthropic-api-key"
DEFAULT_VAULT = "agentfac-kv"


class SecretUnavailableError(RuntimeError):
    pass


def vault_name() -> str:
    return os.environ.get("AZURE_KEY_VAULT_NAME") or DEFAULT_VAULT


def _from_sdk(vault: str) -> str | None:
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError:
        return None
    client = SecretClient(f"https://{vault}.vault.azure.net", DefaultAzureCredential())
    return client.get_secret(SECRET_NAME).value


def _from_cli(vault: str) -> str | None:
    az = shutil.which("az") or shutil.which("az.cmd")
    if az is None:
        return None
    # The data-plane call needs no subscription, so this works whichever
    # subscription the CLI currently points at.
    proc = subprocess.run(
        [az, "keyvault", "secret", "show", "--vault-name", vault, "--name", SECRET_NAME,
         "--query", "value", "-o", "tsv"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        # stderr names the vault and the error, never the value.
        raise SecretUnavailableError(
            f"could not read '{SECRET_NAME}' from Key Vault '{vault}': "
            f"{proc.stderr.strip()[:300]}"
        )
    return proc.stdout.strip() or None


@lru_cache(maxsize=1)
def anthropic_api_key() -> str:
    """The key, from the environment or the vault. Raises if neither has it."""
    key = os.environ.get(ENV_VAR)
    if key:
        return key
    vault = vault_name()
    key = _from_sdk(vault) or _from_cli(vault)
    if not key:
        raise SecretUnavailableError(
            f"no {ENV_VAR} in the environment and no '{SECRET_NAME}' readable from "
            f"Key Vault '{vault}'"
        )
    return key
