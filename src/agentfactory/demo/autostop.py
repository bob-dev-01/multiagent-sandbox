"""Stop the cluster when the demo has gone quiet.

    python -m agentfactory.demo.autostop

Run on a schedule as a Container Apps job. Stops AKS when the demo started it
(`demo-autostop` = on) and nothing has happened for `DEMO_IDLE_MINUTES`
(default 40). A cluster started by hand, without the tag, is left alone.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from .infra import Infra


def main() -> int:
    idle_limit = float(os.environ.get("DEMO_IDLE_MINUTES", "40"))
    infra = Infra()
    tags = infra.tags()
    aks = infra.aks_status()
    print(f"aks={aks} tags={tags}")
    if tags.get("demo-autostop") != "on" or aks.get("power") != "Running":
        return 0
    last = tags.get("demo-last-activity")
    idle = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() / 60 if last \
        else float("inf")
    if idle < idle_limit:
        print(f"active {idle:.0f} min ago; leaving it running")
        return 0
    print(f"idle for {idle:.0f} min; stopping the cluster")
    infra.stop("aks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
