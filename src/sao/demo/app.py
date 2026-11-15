"""Public demo API ("try to break it", D25).

  GET  /api/health
  GET  /api/incidents                 recorded incidents + attack presets
  POST /api/runs                      {incident, preset | custom} → {id}; live team run, rate limited
  GET  /api/runs/{id}                 run document (steps, audit records, result), polled by the page
  POST /api/runs/{id}/kill            revoke every token of that run; the worker measures time to effect
  POST /api/push                      {attempt, incident, call} → one write attempt through a fresh gateway
  POST /api/audit/verify              {records: [...]} → where the hash chain breaks (the gateway's own verifier)

Locally: uv run uvicorn sao.demo.app:app --port 8081 (runs execute in a thread, state in /tmp/sao-demo).
On AWS: Lambda (Mangum) behind a Function URL; runs execute in the worker function, invoked asynchronously.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sao.demo import attack, store
from sao.demo.worker import INCIDENTS, truth
from sao.gateway import audit as audit_mod

WORKER = os.environ.get("SAO_WORKER_FUNCTION")
ORIGINS = [o for o in os.environ.get("SAO_WEB_ORIGINS", "http://localhost:3000").split(",") if o]
RUN_ID = re.compile(r"^[a-z0-9-]{6,40}$")
MAX_VERIFY = 3000


async def health(_: Request):
    return JSONResponse({"ok": True, "live_runs": bool(WORKER) or os.environ.get("SAO_LLM") == "api",
                         "per_hour": store.PER_HOUR, "per_day": store.PER_DAY})


async def incidents(_: Request):
    return JSONResponse({"incidents": [{"id": k, **v, "truth": truth(k)} for k, v in INCIDENTS.items()],
                         "presets": [{"id": k, "label": p["label"], "goal": p["goal"], "text": p["text"]}
                                     for k, p in attack.PRESETS.items()],
                         "max_custom": attack.MAX_TEXT})


def _start(run_id: str, payload: dict) -> None:
    if WORKER:
        store._aws("lambda").invoke(FunctionName=WORKER, InvocationType="Event",
                                    Payload=json.dumps({"run_id": run_id, **payload}).encode())
        return
    from sao.demo import worker

    threading.Thread(target=lambda: asyncio.run(worker.run(run_id, payload["incident"], payload.get("preset"),
                                                           payload.get("custom"))), daemon=True).start()


async def create_run(req: Request):
    try:
        body = await req.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "send JSON"}, status_code=400)
    incident, preset, custom = body.get("incident"), body.get("preset"), body.get("custom")
    if incident not in INCIDENTS:
        return JSONResponse({"error": "unknown incident"}, status_code=400)
    if custom is not None and not isinstance(custom, str):
        return JSONResponse({"error": "custom must be text"}, status_code=400)
    try:
        attack.attack_text(preset, custom)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    ok, why = store.take_run_slot()
    if not ok:
        return JSONResponse({"error": f"the {why} limit of live runs is used up; watch the replay, or try later",
                             "limit": why}, status_code=429)
    run_id = f"{time.strftime('%m%d%H%M', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    payload = {"incident": incident, "preset": preset, "custom": (custom or "")[:attack.MAX_TEXT] or None}
    store.put_run(run_id, {"id": run_id, "status": "queued", "phase": "queued", "incident": incident,
                           "created": time.time(), "steps": [], "audit": []})
    _start(run_id, payload)
    return JSONResponse({"id": run_id}, status_code=202)


async def get_run(req: Request):
    rid = req.path_params["rid"]
    doc = store.get_run(rid) if RUN_ID.match(rid) else None
    if doc is None:
        return JSONResponse({"error": "no such run"}, status_code=404)
    return JSONResponse(doc, headers={"Cache-Control": "no-store"})


async def kill_run(req: Request):
    rid = req.path_params["rid"]
    doc = store.get_run(rid) if RUN_ID.match(rid) else None
    if doc is None:
        return JSONResponse({"error": "no such run"}, status_code=404)
    if doc.get("status") not in ("queued", "running"):
        return JSONResponse({"error": f"run is {doc.get('status')}"}, status_code=409)
    at = store.kill(rid)
    return JSONResponse({"killed_at": at})


CLUSTER_TOOLS = {"rollback_deployment", "restart_deployment", "scale_deployment", "set_flag", "delete_networkpolicy"}


async def push(req: Request):
    """The call comes from the page (a recorded or live run's proposal): it only ever reaches a simulated cluster,
    through the real identity, Cedar and approval checks, so any call a visitor sends is safe to try."""
    from sao.demo import push as push_mod
    try:
        body = await req.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "send JSON"}, status_code=400)
    call, incident, kind = body.get("call") or {}, body.get("incident"), body.get("attempt")
    args = call.get("args")
    if (incident not in INCIDENTS or call.get("tool") not in CLUSTER_TOOLS or not isinstance(args, dict)
            or len(json.dumps(args)) > 500 or kind not in push_mod.ATTEMPTS):
        return JSONResponse({"error": "attempt, incident and a proposed cluster call are required"}, status_code=400)
    rid = str(body.get("run_id") or "demo")[:40]
    try:
        out = await push_mod.attempt(kind, {"tool": call["tool"], "args": args}, incident, rid)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:200]}"}, status_code=500)
    return JSONResponse(out)


def verify_records(records: list[dict]) -> dict:
    """Run the gateway's own chain verifier (sao.gateway.audit.verify) on records sent by the page."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "audit.jsonl"
        f.write_text("".join(json.dumps(r, default=str) + "\n" for r in records))
        ok, msg = audit_mod.verify(f)
    m = re.match(r"line (\d+)", msg)
    return {"ok": ok, "message": msg, "broken_index": int(m.group(1)) - 1 if (m and not ok) else None}


async def verify(req: Request):
    try:
        body = await req.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "send JSON"}, status_code=400)
    recs = body.get("records")
    if not isinstance(recs, list) or not all(isinstance(r, dict) for r in recs) or len(recs) > MAX_VERIFY:
        return JSONResponse({"error": f"records: a list of up to {MAX_VERIFY} objects"}, status_code=400)
    return JSONResponse(verify_records(recs))


app = Starlette(
    routes=[Route("/api/health", health), Route("/api/incidents", incidents),
            Route("/api/runs", create_run, methods=["POST"]), Route("/api/runs/{rid}", get_run),
            Route("/api/runs/{rid}/kill", kill_run, methods=["POST"]),
            Route("/api/push", push, methods=["POST"]), Route("/api/audit/verify", verify, methods=["POST"])],
    # on AWS the Function URL adds the CORS headers (configured in Terraform); two sets of headers break browsers
    middleware=[] if WORKER else [Middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET", "POST"],
                                             allow_headers=["content-type"], max_age=600)])


@functools.cache
def _adapter():
    from mangum import Mangum
    return Mangum(app, lifespan="off")


def handler(event, context):
    """Lambda entry point (Function URL events)."""
    return _adapter()(event, context)
