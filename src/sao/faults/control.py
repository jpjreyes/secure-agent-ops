"""Inject and revert faults safely.

Before a fault touches anything, the objects it will change are saved to runs/faults/active.json (written
atomically). Revert restores them from that file, so a killed process, a usage stop or a crash never leaves the
cluster broken: `revert_all()` runs at the start of every live episode and from the CLI.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sao import kube
from sao.faults.catalog import FAULTS, Fault

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / "runs" / "faults" / "active.json"


def _load() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE)


def active() -> dict:
    return _load()


def inject(fault_id: str, wait: bool = True) -> dict:
    """Inject one fault (only one at a time). Returns its record (id, time injected, saved objects)."""
    f: Fault = FAULTS[fault_id]
    state = _load()
    if state:
        raise RuntimeError(f"a fault is already active: {list(state)}; revert it first")
    saved = [kube.get(kind, name) for kind, name in f.touches]
    rec = {"id": f.id, "saved": saved, "creates": f.creates, "restart": f.restart,
           "restart_after_revert": f.restart_after_revert, "injected_at": None}
    _save({f.id: rec})  # saved BEFORE the change: a crash during inject is still revertible
    f.inject()
    for d in f.restart:
        kube.rollout_restart(d)
    rec["injected_at"] = time.time()
    _save({f.id: rec})
    if wait:
        for d in f.restart:
            kube.rollout_wait(d, 120)
    return rec


def revert_all(wait: bool = True) -> list[str]:
    """Restore every saved object, delete created ones, restart what the fault restarted. Idempotent."""
    state = _load()
    done = []
    for fid, rec in state.items():
        for obj in rec["saved"]:
            kube.replace(obj)
        for kind, name in rec["creates"]:
            kube.delete(kind, name)
        for d in rec["restart"] + rec.get("restart_after_revert", []):
            kube.rollout_restart(d)
        if wait:
            deps = set(rec["restart"]) | set(rec.get("restart_after_revert", [])) | {
                o["metadata"]["name"] for o in rec["saved"] if o["kind"] == "Deployment"}
            for d in deps:
                kube.rollout_wait(d, 180)
        done.append(fid)
    if STATE.exists():
        STATE.unlink()
    return done


def healthy() -> tuple[bool, list[str]]:
    """All otel-demo pods Running and ready (the precondition for a clean episode)."""
    pods = kube.get("pods")["items"]
    bad = []
    for p in pods:
        cs = p.get("status", {}).get("containerStatuses") or []
        if p["status"].get("phase") != "Running" or not cs or not all(c.get("ready") for c in cs):
            bad.append(p["metadata"]["name"])
    return (not bad, bad)
