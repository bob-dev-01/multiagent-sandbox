"""The demo's HTTP API.

    uvicorn agentfactory.demo.app:app --port 8080

Read-only routes are open; anything that spends money or changes the cloud
(running a task, starting or stopping a resource, resetting the registry)
needs the demo access code in the `X-Demo-Code` header. Run events stream as
Server-Sent Events.

Environment:
  DEMO_ACCESS_CODE          required for the protected routes (no default: unset = refused)
  DEMO_ALLOWED_ORIGINS      comma-separated CORS origins (the GitHub Pages site)
  DEMO_DAILY_BUDGET_USD     model spend cap per day (default 3)
  K8S_SERVER, K8S_TOKEN, K8S_CA_B64
                            the demo service account's access to the sandbox namespace;
                            written to a kubeconfig at start-up when present
  ANTHROPIC_API_KEY         from Key Vault, through the container app's secret reference
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .infra import Infra
from .session import DemoSession, RunRefused, RunRequest, runner_factory_from_env


def _write_kubeconfig() -> None:
    """A kubeconfig for the demo service account, from environment secrets."""
    server, token, ca = (os.environ.get(k) for k in ("K8S_SERVER", "K8S_TOKEN", "K8S_CA_B64"))
    if not (server and token and ca):
        return
    context = os.environ.setdefault("AGENTFACTORY_KUBE_CONTEXT", "demo")
    config = {
        "apiVersion": "v1", "kind": "Config", "current-context": context,
        "clusters": [{"name": "aks", "cluster": {"server": server,
                                                 "certificate-authority-data": ca}}],
        "users": [{"name": "demo", "user": {"token": token}}],
        "contexts": [{"name": context, "context": {"cluster": "aks", "user": "demo",
                                                   "namespace": "agentfactory-sandbox"}}],
    }
    path = Path(tempfile.gettempdir()) / "demo-kubeconfig.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    path.chmod(0o600)
    os.environ["KUBECONFIG"] = str(path)


_write_kubeconfig()

app = FastAPI(title="Agent Factory demo", docs_url=None, redoc_url=None)
origins = [o.strip() for o in os.environ.get("DEMO_ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins or ["http://localhost:8000"],
                   allow_methods=["GET", "POST"], allow_headers=["X-Demo-Code", "Content-Type"])

session = DemoSession(runner_factory=runner_factory_from_env(),
                      daily_budget_usd=float(os.environ.get("DEMO_DAILY_BUDGET_USD", "3")))
infra = Infra()


@app.on_event("startup")
def _warm_up() -> None:
    # Load the embedding model in the background, so the first run of a cold
    # container does not spend a minute on it.
    import threading

    threading.Thread(target=lambda: session.embedder, daemon=True).start()


def _authorise(code: str | None) -> None:
    expected = os.environ.get("DEMO_ACCESS_CODE")
    if not expected or not code or not hmac.compare_digest(code, expected):
        raise HTTPException(status_code=401, detail="a valid demo access code is required")


def _touch() -> None:
    # Activity tagging is best effort; a failure must not fail the run.
    with contextlib.suppress(Exception):
        infra.touch()


class RunBody(BaseModel):
    task_id: str
    mode: str = "live"
    wording: str = "original"
    custom_text: str | None = None
    level: str = "L3"
    profile: str = "strict"
    sandbox_always: bool = False


class BatchBody(BaseModel):
    task_ids: list[str]
    mode: str = "live"
    wording: str = "original"
    level: str = "L3"
    profile: str = "strict"
    sandbox_always: bool = False
    parallel: int = 1


@app.post("/api/batches")
def start_batch(body: BatchBody, x_demo_code: str | None = Header(default=None)) -> dict[str, Any]:
    _authorise(x_demo_code)
    template = RunRequest(task_id=body.task_ids[0] if body.task_ids else "", mode=body.mode,
                          wording=body.wording, level=body.level, profile=body.profile,
                          sandbox_always=body.sandbox_always)
    try:
        batch = session.start_batch(body.task_ids, template, body.parallel)
    except RunRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _touch()
    return {"batch_id": batch.batch_id}


@app.get("/api/batches/{batch_id}")
def batch_status(batch_id: str) -> dict[str, Any]:
    batch = session.batches.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404)
    _touch_quiet()
    return batch.view()


@app.post("/api/batches/{batch_id}/stop")
def stop_batch(batch_id: str, x_demo_code: str | None = Header(default=None)) -> dict[str, Any]:
    _authorise(x_demo_code)
    batch = session.batches.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404)
    batch.cancel = True
    return {"ok": True}


_last_touch = [0.0]


def _touch_quiet() -> None:
    """Keep the auto-stop away while a batch runs, at most once a minute."""
    import time

    if time.time() - _last_touch[0] > 60:
        _last_touch[0] = time.time()
        _touch()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/session")
def describe() -> dict[str, Any]:
    return session.describe()


@app.post("/api/auth")
def check_code(x_demo_code: str | None = Header(default=None)) -> dict[str, Any]:
    _authorise(x_demo_code)
    return {"ok": True}


@app.post("/api/runs")
def start_run(body: RunBody, x_demo_code: str | None = Header(default=None)) -> dict[str, Any]:
    _authorise(x_demo_code)
    try:
        run = session.start(RunRequest(**body.model_dump()))
    except RunRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _touch()
    return {"run_id": run.run_id}


@app.get("/api/runs/{run_id}")
def run_events(run_id: str) -> dict[str, Any]:
    run = session.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404)
    return {"run_id": run_id, "done": run.done, "events": run.events}


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str) -> StreamingResponse:
    run = session.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404)

    async def events():
        seq = -1
        while True:
            batch = await asyncio.to_thread(run.wait_after, seq, 15.0)
            for event in batch:
                seq = event["seq"]
                yield f"data: {json.dumps(event, default=str)}\n\n"
            if run.done and seq >= len(run.events) - 1:
                yield "event: end\ndata: {}\n\n"
                return
            if not batch:
                yield ": keep-alive\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/reset")
def reset(x_demo_code: str | None = Header(default=None)) -> dict[str, Any]:
    _authorise(x_demo_code)
    try:
        session.reset()
    except RunRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/infra")
def infra_status() -> dict[str, Any]:
    return infra.status()


@app.post("/api/infra/{resource}/{action}")
def infra_action(resource: str, action: str,
                 x_demo_code: str | None = Header(default=None)) -> dict[str, Any]:
    _authorise(x_demo_code)
    if resource not in ("aks", "vm") or action not in ("start", "stop"):
        raise HTTPException(status_code=404)
    try:
        (infra.start if action == "start" else infra.stop)(resource)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
    return {"ok": True, "resource": resource, "action": action}


@app.get("/api/budget")
def budget() -> dict[str, Any]:
    return {"model": asdict(session.budget) | {"left_usd": round(session.budget.left_usd, 2)}}
