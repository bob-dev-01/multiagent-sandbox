"""Start, stop and watch the demo's cloud resources, and what they have cost.

Talks to Azure Resource Manager directly with a bearer token: in Container
Apps the token comes from the app's managed identity, whose custom role allows
only reading, starting and stopping the AKS cluster and the VM, tagging the
resource group, and reading its cost; locally it comes from `az login`.

The resource group carries two tags the auto-stop job reads:
`demo-last-activity` (an ISO timestamp, refreshed by every demo action) and
`demo-autostop` ("on" once the demo has started the cluster).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx

ARM = "https://management.azure.com"
AKS_API = "2024-05-01"
VM_API = "2024-07-01"
TAGS_API = "2021-04-01"
COST_API = "2023-11-01"


class Infra:
    def __init__(self) -> None:
        self.subscription = os.environ.get("AZURE_SUBSCRIPTION_ID",
                                           "1c063792-23ea-4e59-ac80-7fed220d9711")
        self.group = os.environ.get("AZURE_RESOURCE_GROUP", "rg-agentfactory-dev")
        self.aks = os.environ.get("AZURE_AKS_NAME", "agentfac-aks")
        self.vm = os.environ.get("AZURE_VM_NAME", "agentfac-vm")
        self._credential: Any = None
        self._cost: tuple[float, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ http

    def _token(self) -> str:
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return self._credential.get_token(f"{ARM}/.default").token

    def _request(self, method: str, path: str, api: str, **kwargs: Any) -> httpx.Response:
        url = f"{ARM}{path}?api-version={api}"
        headers = {"Authorization": f"Bearer {self._token()}"}
        response = httpx.request(method, url, headers=headers, timeout=30, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:300]}")
        return response

    @property
    def _rg(self) -> str:
        return f"/subscriptions/{self.subscription}/resourceGroups/{self.group}"

    # ---------------------------------------------------------------- status

    def aks_status(self) -> dict[str, Any]:
        body = self._request("GET", f"{self._rg}/providers/Microsoft.ContainerService/"
                                    f"managedClusters/{self.aks}", AKS_API).json()
        props = body.get("properties", {})
        return {"power": props.get("powerState", {}).get("code"),
                "provisioning": props.get("provisioningState")}

    def vm_status(self) -> dict[str, Any]:
        body = self._request("GET", f"{self._rg}/providers/Microsoft.Compute/virtualMachines/"
                                    f"{self.vm}/instanceView", VM_API).json()
        power = next((s.get("displayStatus") for s in body.get("statuses", [])
                      if s.get("code", "").startswith("PowerState/")), None)
        return {"power": power}

    def tags(self) -> dict[str, str]:
        body = self._request("GET", f"{self._rg}/providers/Microsoft.Resources/tags/default",
                             TAGS_API).json()
        return body.get("properties", {}).get("tags", {}) or {}

    def cost(self) -> dict[str, Any]:
        """Month-to-date cost of the resource group; cached for 30 minutes."""
        if self._cost and time.time() - self._cost[0] < 1800:
            return self._cost[1]
        body = self._request(
            "POST", f"{self._rg}/providers/Microsoft.CostManagement/query", COST_API,
            json={"type": "ActualCost", "timeframe": "MonthToDate",
                  "dataset": {"granularity": "None",
                              "aggregation": {"total": {"name": "Cost", "function": "Sum"}}}},
        ).json()
        rows = body.get("properties", {}).get("rows") or [[0.0, "USD"]]
        value = {"month_to_date": round(float(rows[0][0]), 2),
                 "currency": rows[0][-1] if len(rows[0]) > 1 else "USD",
                 "as_of": datetime.now(UTC).isoformat(timespec="minutes")}
        self._cost = (time.time(), value)
        return value

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, call in (("aks", self.aks_status), ("vm", self.vm_status),
                           ("tags", self.tags), ("cost", self.cost)):
            try:
                out[name] = call()
            except Exception as exc:  # noqa: BLE001 — one failing call must not hide the rest
                out[name] = {"error": str(exc)[:200]}
        return out

    # --------------------------------------------------------------- actions

    def set_tags(self, **tags: str) -> None:
        self._request("PATCH", f"{self._rg}/providers/Microsoft.Resources/tags/default",
                      TAGS_API, json={"operation": "Merge", "properties": {"tags": tags}})

    def touch(self) -> None:
        """Record demo activity, which keeps the auto-stop job away."""
        self.set_tags(**{"demo-last-activity": datetime.now(UTC).isoformat(timespec="seconds")})

    def start(self, resource: str) -> None:
        if resource == "aks":
            self.touch()
            self.set_tags(**{"demo-autostop": "on"})
            self._request("POST", f"{self._rg}/providers/Microsoft.ContainerService/"
                                  f"managedClusters/{self.aks}/start", AKS_API)
        elif resource == "vm":
            self._request("POST", f"{self._rg}/providers/Microsoft.Compute/virtualMachines/"
                                  f"{self.vm}/start", VM_API)
        else:
            raise ValueError(resource)

    def stop(self, resource: str) -> None:
        if resource == "aks":
            self._request("POST", f"{self._rg}/providers/Microsoft.ContainerService/"
                                  f"managedClusters/{self.aks}/stop", AKS_API)
            self.set_tags(**{"demo-autostop": "off"})
        elif resource == "vm":
            # Deallocate, not power off: a stopped-but-allocated VM is still billed.
            self._request("POST", f"{self._rg}/providers/Microsoft.Compute/virtualMachines/"
                                  f"{self.vm}/deallocate", VM_API)
        else:
            raise ValueError(resource)
