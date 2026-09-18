"""afctl — resource control panel for the Agent Factory environment.

On a metered subscription the expensive resources are the ones that bill while
idle: the orchestrator VM and the AKS node pools. Leaving them up over a weekend
costs more than the whole planned model budget for the study. So this exists to
make "turn it all off" a single command that is easy enough to actually run.

    afctl preflight   register the providers a new subscription needs
    afctl status      what is running, and what it costs per hour
    afctl down        deallocate the VM, stop the AKS cluster and PostgreSQL
    afctl up          bring it back
    afctl cost        actual spend this month, from Cost Management
    afctl deploy      apply the Bicep templates
    afctl nuke        delete the resource group (asks twice)

Everything goes through the az CLI rather than the management SDKs: the same
commands can be run by hand when something goes wrong, and there is no second
authentication path to keep working.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import typer
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    print("afctl needs typer and rich: pip install -e '.[dev,dashboard]'", file=sys.stderr)
    raise

REPO_ROOT = Path(__file__).resolve().parents[1]

app = typer.Typer(
    add_completion=False,
    help="Control panel for the Agent Factory Azure environment.",
    no_args_is_help=True,
)
console = Console()

DEFAULT_RESOURCE_GROUP = os.environ.get("AZURE_RESOURCE_GROUP", "rg-agentfactory-dev")
DEFAULT_LOCATION = os.environ.get("AZURE_LOCATION", "centralindia")

# Approximate list prices, USD/hour, centralindia. Used only to show the idle
# burn rate; `afctl cost` reports what Azure actually billed.
HOURLY_ESTIMATES: dict[str, float] = {
    "Standard_B2s_v2": 0.083,
    "Standard_B2as_v2": 0.075,
    "Standard_D2s_v3": 0.112,
    "Standard_D4s_v3": 0.224,
    "Standard_B1ms": 0.021,
    "Standard_B2s": 0.045,
}


class AzError(RuntimeError):
    pass


def _az_executable() -> str:
    """Resolve the az entry point.

    On Windows az is a .cmd shim, which subprocess cannot launch by bare name
    without going through a shell — and going through a shell would mean
    quoting every argument correctly, which is worse.
    """
    found = shutil.which("az") or shutil.which("az.cmd")
    if not found:
        raise AzError("az CLI not found on PATH. Install it or run: az login")
    return found


def _kubectl_executable() -> str:
    found = shutil.which("kubectl") or shutil.which("kubectl.exe")
    if not found:
        raise AzError("kubectl not found on PATH")
    return found


def az(*args: str, timeout: int = 300, check: bool = True) -> Any:
    """Run an az command and parse its JSON output."""
    cmd = [_az_executable(), *args]
    if "-o" not in args and "--output" not in args:
        cmd += ["-o", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        if check:
            raise AzError(f"az {' '.join(args)} failed:\n{proc.stderr.strip()}")
        return None
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout.strip()


@dataclass
class Resource:
    kind: str
    name: str
    state: str
    detail: str = ""
    hourly_usd: float = 0.0

    @property
    def running(self) -> bool:
        return self.state.lower() in {
            "vm running", "running", "ready", "succeeded", "started",
        }


def _rg_exists(resource_group: str) -> bool:
    return bool(az("group", "exists", "-n", resource_group, check=False))


def _set_pool_size(resource_group: str, cluster: str, pool: dict[str, Any], target: int) -> None:
    """Resize an AKS node pool, autoscaler or not.

    `az aks nodepool scale` is refused outright on an autoscaler-enabled pool —
    the autoscaler owns the count, so the way to pin a pool at a size is to move
    its bounds. Getting this wrong is quiet: the scale command fails, the pool
    keeps running, and the bill keeps arriving.
    """
    name = pool["name"]
    if pool.get("count", 0) == target and not pool.get("enableAutoScaling"):
        return

    if pool.get("enableAutoScaling"):
        if pool.get("minCount") == target and pool.get("maxCount") == max(target, 1):
            return
        console.print(f"  setting {cluster}/{name} autoscaler bounds to {target}")
        az("aks", "nodepool", "update", "-g", resource_group,
           "--cluster-name", cluster, "-n", name,
           "--update-cluster-autoscaler",
           "--min-count", str(target), "--max-count", str(max(target, 1)),
           "-o", "none", check=False)
    else:
        console.print(f"  scaling {cluster}/{name} to {target}")
        az("aks", "nodepool", "scale", "-g", resource_group,
           "--cluster-name", cluster, "-n", name,
           "-c", str(target), "--no-wait", check=False)


def _collect(resource_group: str) -> list[Resource]:
    resources: list[Resource] = []

    for vm in az("vm", "list", "-g", resource_group, check=False) or []:
        name = vm["name"]
        size = vm.get("hardwareProfile", {}).get("vmSize", "?")
        # Fetch the whole instance view and filter here rather than with
        # --query. On Windows `az` is a batch shim, and cmd.exe treats `?` and
        # `[` in a JMESPath expression as syntax of its own — the call fails
        # with a parse error that looks nothing like an Azure problem.
        view = az("vm", "get-instance-view", "-g", resource_group, "-n", name, check=False) or {}
        statuses = (view.get("instanceView") or {}).get("statuses") or []
        state = next(
            (
                s.get("displayStatus", "unknown")
                for s in statuses
                if str(s.get("code", "")).startswith("PowerState")
            ),
            "unknown",
        )
        resources.append(
            Resource("VM", name, state, size,
                     HOURLY_ESTIMATES.get(size, 0.0) if "running" in state.lower() else 0.0)
        )

    for cluster in az("aks", "list", "-g", resource_group, check=False) or []:
        name = cluster["name"]
        stopped = cluster.get("powerState", {}).get("code") == "Stopped"
        for pool in cluster.get("agentPoolProfiles", []):
            # A stopped cluster still reports its configured node count, so
            # trust the power state over the count when deciding what is
            # actually billing.
            count = 0 if stopped else pool.get("count", 0)
            size = pool.get("vmSize", "?")
            if stopped:
                state = "cluster stopped"
            elif count:
                state = "running"
            else:
                state = "scaled to 0"
            resources.append(
                Resource(
                    "AKS pool",
                    f"{name}/{pool['name']}",
                    state,
                    f"{count} x {size}",
                    HOURLY_ESTIMATES.get(size, 0.0) * count,
                )
            )

    for server in az("postgres", "flexible-server", "list", "-g", resource_group, check=False) or []:
        sku = server.get("sku", {}).get("name", "?")
        state = server.get("state", "unknown")
        resources.append(
            Resource("PostgreSQL", server["name"], state, sku,
                     HOURLY_ESTIMATES.get(sku, 0.0) if state.lower() == "ready" else 0.0)
        )

    for account in az("storage", "account", "list", "-g", resource_group, check=False) or []:
        resources.append(Resource("Storage", account["name"], "always on",
                                  account.get("sku", {}).get("name", "")))

    for group in az("container", "list", "-g", resource_group, check=False) or []:
        resources.append(
            Resource("ACI", group["name"],
                     group.get("instanceView", {}).get("state", "unknown"),
                     "ephemeral sandbox")
        )

    return resources


# --------------------------------------------------------------- commands


@app.command()
def status(
    resource_group: str = typer.Option(DEFAULT_RESOURCE_GROUP, "--resource-group", "-g"),
) -> None:
    """Show what is running and the current idle burn rate."""
    if not _rg_exists(resource_group):
        console.print(f"[yellow]Resource group '{resource_group}' does not exist.[/]")
        console.print("Run [bold]afctl deploy[/] to create the environment.")
        raise typer.Exit(0)

    resources = _collect(resource_group)
    table = Table(title=f"Agent Factory — {resource_group}", header_style="bold")
    table.add_column("Kind")
    table.add_column("Name")
    table.add_column("State")
    table.add_column("Detail")
    table.add_column("USD/hour", justify="right")

    for res in sorted(resources, key=lambda r: (r.kind, r.name)):
        colour = "green" if res.running else "dim"
        table.add_row(
            res.kind, res.name,
            f"[{colour}]{res.state}[/]", res.detail,
            f"{res.hourly_usd:.3f}" if res.hourly_usd else "-",
        )

    console.print(table)
    burn = sum(r.hourly_usd for r in resources)
    if burn > 0:
        console.print(
            f"\nEstimated burn: [bold]${burn:.3f}/hour[/]  "
            f"(~${burn * 24:.2f}/day, ~${burn * 24 * 30:.0f}/month if left running)"
        )
        console.print("Run [bold]afctl down[/] to stop paying for idle compute.")
    else:
        console.print("\n[green]Nothing billable is running.[/] Storage and logs still cost a little.")


@app.command()
def down(
    resource_group: str = typer.Option(DEFAULT_RESOURCE_GROUP, "--resource-group", "-g"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Stop everything billable. All data is preserved."""
    if not _rg_exists(resource_group):
        console.print(f"[yellow]Resource group '{resource_group}' does not exist.[/]")
        raise typer.Exit(1)

    if not yes and not typer.confirm(f"Stop all compute in '{resource_group}'?", default=True):
        raise typer.Exit(0)

    for vm in az("vm", "list", "-g", resource_group, check=False) or []:
        console.print(f"  deallocating VM {vm['name']}")
        # deallocate, not stop: a stopped VM still bills for its compute
        # reservation, a deallocated one does not.
        az("vm", "deallocate", "-g", resource_group, "-n", vm["name"], "--no-wait", check=False)

    for cluster in az("aks", "list", "-g", resource_group, check=False) or []:
        # Stop the whole cluster rather than scaling pools to zero. A system
        # pool cannot go below one node, so pool scaling always leaves a node
        # billing — roughly $60/month here, which matters against a $100
        # credit. `aks stop` deallocates every node including the system pool
        # and keeps the control plane and all cluster state.
        if cluster.get("powerState", {}).get("code") != "Stopped":
            console.print(f"  stopping AKS cluster {cluster['name']}")
            az("aks", "stop", "-g", resource_group, "-n", cluster["name"],
               "--no-wait", "-o", "none", check=False)

    for server in az("postgres", "flexible-server", "list", "-g", resource_group, check=False) or []:
        if server.get("state", "").lower() == "ready":
            console.print(f"  stopping PostgreSQL {server['name']}")
            az("postgres", "flexible-server", "stop", "-g", resource_group,
               "-n", server["name"], check=False)

    for group in az("container", "list", "-g", resource_group, check=False) or []:
        console.print(f"  deleting leftover ACI group {group['name']}")
        az("container", "delete", "-g", resource_group, "-n", group["name"], "--yes", check=False)

    console.print("\n[green]Shutdown requested.[/] Some operations finish in the background.")
    console.print("Note: Azure auto-starts a stopped PostgreSQL server after 7 days.")


@app.command()
def up(
    resource_group: str = typer.Option(DEFAULT_RESOURCE_GROUP, "--resource-group", "-g"),
    sandbox_nodes: int = typer.Option(1, "--sandbox-nodes", help="Nodes in the gVisor pool."),
) -> None:
    """Bring the environment back up."""
    if not _rg_exists(resource_group):
        console.print(f"[yellow]Resource group '{resource_group}' does not exist.[/]")
        raise typer.Exit(1)

    for server in az("postgres", "flexible-server", "list", "-g", resource_group, check=False) or []:
        if server.get("state", "").lower() != "ready":
            console.print(f"  starting PostgreSQL {server['name']}")
            az("postgres", "flexible-server", "start", "-g", resource_group,
               "-n", server["name"], check=False)

    for vm in az("vm", "list", "-g", resource_group, check=False) or []:
        console.print(f"  starting VM {vm['name']}")
        az("vm", "start", "-g", resource_group, "-n", vm["name"], "--no-wait", check=False)

    for cluster in az("aks", "list", "-g", resource_group, check=False) or []:
        name = cluster["name"]
        if cluster.get("powerState", {}).get("code") == "Stopped":
            console.print(f"  starting AKS cluster {name}")
            az("aks", "start", "-g", resource_group, "-n", name, "-o", "none", check=False)
        for pool in cluster.get("agentPoolProfiles", []):
            target = sandbox_nodes if pool.get("mode") == "User" else 1
            _set_pool_size(resource_group, name, pool, target)

    console.print("\n[green]Startup requested.[/]")
    console.print(
        "A sandbox node that was scaled to zero comes back as a [bold]new[/] VM, so gVisor is "
        "not on it yet — the installer DaemonSet reinstalls it automatically, which takes a "
        "few minutes. L3 is not usable until it finishes."
    )
    console.print("\nVerify before running a batch:")
    console.print("  kubectl -n kube-system rollout status ds/gvisor-installer")
    console.print("  kubectl get runtimeclass gvisor")


@app.command()
def cost(
    resource_group: str = typer.Option(DEFAULT_RESOURCE_GROUP, "--resource-group", "-g"),
    days: int = typer.Option(30, "--days", help="Look-back window."),
) -> None:
    """Actual spend, from Cost Management."""
    account = az("account", "show", check=False) or {}
    scope_sub = account.get("id")
    if not scope_sub:
        console.print("[red]Not logged in.[/] Run: az login")
        raise typer.Exit(1)

    result = az(
        "costmanagement", "query",
        "--type", "ActualCost",
        "--scope", f"/subscriptions/{scope_sub}/resourceGroups/{resource_group}",
        "--timeframe", "MonthToDate",
        "--dataset-aggregation", '{"totalCost":{"name":"Cost","function":"Sum"}}',
        "--dataset-grouping", "name=ResourceType", "type=Dimension",
        check=False,
    )

    if not result or not result.get("rows"):
        console.print(
            "[yellow]No cost data returned.[/] Cost Management lags by up to 24 hours, "
            "and a brand-new resource group will have nothing yet."
        )
        raise typer.Exit(0)

    table = Table(title=f"Month-to-date cost — {resource_group}", header_style="bold")
    table.add_column("Resource type")
    table.add_column("USD", justify="right")
    total = 0.0
    for row in sorted(result["rows"], key=lambda r: -float(r[0])):
        amount = float(row[0])
        total += amount
        table.add_row(str(row[1]), f"{amount:.2f}")
    table.add_row("[bold]TOTAL[/]", f"[bold]{total:.2f}[/]")
    console.print(table)


# Namespaces this deployment touches. A fresh subscription has none of them
# registered, and the failure surfaces mid-deployment as
# MissingSubscriptionRegistration on whichever resource needs it first — after
# the rest of the environment already exists. Microsoft.OperationsManagement is
# the one that is easy to miss: nothing references it directly, AKS pulls it in
# through the Container Insights addon.
REQUIRED_PROVIDERS = (
    "Microsoft.Compute",
    "Microsoft.Network",
    "Microsoft.Storage",
    "Microsoft.ContainerService",
    "Microsoft.ContainerInstance",
    "Microsoft.DBforPostgreSQL",
    "Microsoft.KeyVault",
    "Microsoft.OperationalInsights",
    "Microsoft.OperationsManagement",
    "Microsoft.Insights",
    "Microsoft.ManagedIdentity",
    "Microsoft.Consumption",
)


def _registration_state(namespace: str) -> str:
    return (
        az("provider", "show", "-n", namespace, "--query", "registrationState",
           "-o", "tsv", check=False)
        or "Unknown"
    )


@app.command()
def preflight(
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Block until registration finishes."),
) -> None:
    """Register the resource providers a new subscription needs.

    Run once per subscription before the first deploy. Registration is free and
    idempotent; skipping it means discovering the missing namespace only after
    half the environment is built.
    """
    pending: list[str] = []
    for namespace in REQUIRED_PROVIDERS:
        if _registration_state(namespace) == "Registered":
            continue
        console.print(f"  registering {namespace}")
        az("provider", "register", "--namespace", namespace, "-o", "none", check=False)
        pending.append(namespace)

    if not pending:
        console.print("[green]All providers already registered.[/]")
        return
    if not wait:
        console.print("Registration requested; it continues in the background.")
        return

    console.print("Waiting for registration...")
    for _ in range(30):
        pending = [ns for ns in pending if _registration_state(ns) != "Registered"]
        if not pending:
            break
        time.sleep(10)

    if pending:
        console.print(f"[yellow]Still registering:[/] {', '.join(pending)}")
        console.print("Wait a few minutes and re-run, or deploy once they settle.")
        raise typer.Exit(1)
    console.print("[green]All providers registered.[/] Ready to deploy.")


@app.command()
def deploy(
    resource_group: str = typer.Option(DEFAULT_RESOURCE_GROUP, "--resource-group", "-g"),
    location: str = typer.Option(DEFAULT_LOCATION, "--location", "-l"),
    params: Path = typer.Option(
        REPO_ROOT / "infra" / "params" / "dev.bicepparam", "--params", "-p"
    ),
    what_if: bool = typer.Option(False, "--what-if", help="Preview changes without applying."),
) -> None:
    """Deploy the Bicep templates.

    Requires AF_PG_PASSWORD and AF_SSH_PUBLIC_KEY in the environment; the
    parameter file reads them from there so no secret is ever committed.
    """
    missing = [v for v in ("AF_PG_PASSWORD", "AF_SSH_PUBLIC_KEY") if not os.environ.get(v)]
    if missing:
        console.print(f"[red]Missing environment variables:[/] {', '.join(missing)}")
        console.print("\nSet them in your shell before deploying. For the SSH key:")
        console.print('  export AF_SSH_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)"')
        raise typer.Exit(1)

    unregistered = [ns for ns in REQUIRED_PROVIDERS if _registration_state(ns) != "Registered"]
    if unregistered:
        console.print(f"[yellow]Unregistered providers:[/] {', '.join(unregistered)}")
        console.print("Run [bold]afctl preflight[/] first, or the deployment fails partway "
                      "through with resources already created.")
        raise typer.Exit(1)

    verb = "Previewing" if what_if else "Deploying"
    console.print(f"{verb} to [bold]{resource_group}[/] in {location}...")

    cmd = [
        "deployment", "sub", "what-if" if what_if else "create",
        "--location", location,
        "--template-file", str(REPO_ROOT / "infra" / "main.bicep"),
        "--parameters", str(params),
        "--name", f"agentfactory-{datetime.now(UTC):%Y%m%d-%H%M%S}",
    ]
    result = az(*cmd, timeout=3600, check=False)
    if result is None:
        console.print("[red]Deployment failed.[/] Re-run the az command directly for full output.")
        raise typer.Exit(1)

    if not what_if:
        outputs = (result or {}).get("properties", {}).get("outputs", {})
        if outputs:
            table = Table(title="Deployment outputs", header_style="bold")
            table.add_column("Key")
            table.add_column("Value")
            for key, value in outputs.items():
                shown = str(value.get("value", ""))
                if "ConnectionString" in key:
                    shown = shown[:24] + "..."  # do not print the whole thing
                table.add_row(key, shown)
            console.print(table)
        console.print("\nNext: [bold]afctl bootstrap[/] to apply the gVisor DaemonSet.")


@app.command()
def bootstrap(
    resource_group: str = typer.Option(DEFAULT_RESOURCE_GROUP, "--resource-group", "-g"),
) -> None:
    """Fetch AKS credentials and install the gVisor RuntimeClass."""
    clusters = az("aks", "list", "-g", resource_group, check=False) or []
    if not clusters:
        console.print("[yellow]No AKS cluster in this resource group.[/]")
        raise typer.Exit(1)

    name = clusters[0]["name"]
    console.print(f"  fetching credentials for {name}")
    az("aks", "get-credentials", "-g", resource_group, "-n", name, "--overwrite-existing",
       "-o", "none", check=False)

    manifest = REPO_ROOT / "infra" / "scripts" / "install-gvisor-daemonset.yaml"
    console.print(f"  applying {manifest.name}")
    proc = subprocess.run(
        [_kubectl_executable(), "apply", "-f", str(manifest)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    console.print(proc.stdout or proc.stderr)
    if proc.returncode != 0:
        raise typer.Exit(1)

    console.print("\nWaiting for the installer DaemonSet...")
    subprocess.run(
        [_kubectl_executable(), "-n", "kube-system", "rollout", "status",
         "ds/gvisor-installer", "--timeout=300s"],
        timeout=360, check=False,
    )
    check = subprocess.run(
        [_kubectl_executable(), "get", "runtimeclass", "gvisor"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if check.returncode == 0:
        console.print("[green]RuntimeClass 'gvisor' is registered.[/]")
        console.print("Verify it actually runs before trusting L3:")
        console.print("  kubectl -n agentfactory-sandbox run probe --rm -it --restart=Never \\")
        console.print("    --image=python:3.12-slim --overrides='{\"spec\":{\"runtimeClassName\":\"gvisor\"}}' \\")
        console.print("    -- python -c \"import platform; print(platform.uname().release)\"")
        console.print("  A gVisor kernel reports a version string unlike the host's.")
    else:
        console.print("[red]RuntimeClass 'gvisor' is not registered.[/] L3 will refuse to run.")
        raise typer.Exit(1)


@app.command()
def nuke(
    resource_group: str = typer.Option(DEFAULT_RESOURCE_GROUP, "--resource-group", "-g"),
) -> None:
    """Delete the resource group and everything in it. Not reversible."""
    if not _rg_exists(resource_group):
        console.print(f"[yellow]Resource group '{resource_group}' does not exist.[/]")
        raise typer.Exit(0)

    resources = _collect(resource_group)
    console.print(f"[bold red]This deletes {len(resources)} resources in '{resource_group}'.[/]")
    console.print("Experiment logs in Blob storage and the Agent Registry go with them.")
    for res in resources:
        console.print(f"  - {res.kind}: {res.name}")

    if not typer.confirm("\nDelete all of this?", default=False):
        raise typer.Exit(0)
    typed = typer.prompt("Type the resource group name to confirm")
    if typed != resource_group:
        console.print("[yellow]Name did not match. Nothing was deleted.[/]")
        raise typer.Exit(1)

    console.print("Deleting...")
    az("group", "delete", "-n", resource_group, "--yes", "--no-wait", check=False)
    console.print("[green]Deletion started.[/] It runs in the background for a few minutes.")


if __name__ == "__main__":
    app()
