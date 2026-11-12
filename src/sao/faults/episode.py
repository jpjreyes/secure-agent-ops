"""One live incident, recorded: baseline → inject a fault → symptoms → (agents investigate) → revert.

  uv run python -m sao.faults.episode flag-payment-failure            # record, then revert
  uv run python -m sao.faults.episode cfg-cart-scaled-to-zero --hold  # keep the fault until Enter (live agents)

Writes runs/live/<episode>/:
  snapshot/   the recording (same layout as an ITBench-AA snapshot; the only folder agents' tools can read)
  truth.yaml  ground truth in ITBench-AA format (outside snapshot/, so no tool can read it)
  episode.json fault, timings, which expected alerts fired, cleanup status

Safety: any active fault is reverted first; the fault is reverted in `finally` (Ctrl-C, crash, usage stop), and its
saved objects stay in runs/faults/active.json until the revert succeeds, so `python -m sao.faults revert` can always
finish the job.
"""

from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sao.faults import control
from sao.faults.catalog import FAULTS

ROOT = Path(__file__).resolve().parents[3]
LIVE = ROOT / "runs" / "live"


def firing(snapshot: Path) -> set[tuple[str, str]]:
    """(alertname, service) pairs firing in the latest alert snapshot."""
    files = sorted(glob.glob(str(snapshot / "alerts" / "*.json")))
    if not files:
        return set()
    d = json.loads(Path(files[-1]).read_text())
    return {(a["labels"].get("alertname"), a["labels"].get("service_name", "")) for a in d["data"]["alerts"]
            if a.get("state") == "firing"}


def record(fault_id: str, baseline_s: int = 120, after_s: int = 30, hold: bool = False,
           name: str | None = None) -> Path:
    f = FAULTS[fault_id]
    reverted = control.revert_all()
    if reverted:
        print(f"reverted leftover faults: {reverted}")
    ok, bad = control.healthy()
    if not ok:
        raise SystemExit(f"cluster not healthy before the episode: {bad}")
    ep = LIVE / (name or f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{fault_id}")
    snap = ep / "snapshot"
    snap.mkdir(parents=True)
    (ep / "truth.yaml").write_text(f.truth_yaml())
    info = {"fault": fault_id, "family": f.family, "root": f.root, "expected_symptoms": f.symptoms}
    rec = subprocess.Popen([sys.executable, "-m", "sao.telemetry.live", str(snap)],
                           stdout=(ep / "recorder.log").open("w"), stderr=subprocess.STDOUT)
    try:
        print(f"[{fault_id}] baseline {baseline_s}s …", flush=True)
        time.sleep(baseline_s)
        baseline_alerts = firing(snap)
        info["injected_at"] = time.time()
        control.inject(fault_id)
        print(f"[{fault_id}] injected; waiting up to {f.settle_s}s for symptoms …", flush=True)
        deadline, seen = time.time() + f.settle_s, set()
        while time.time() < deadline:
            time.sleep(15)
            seen = {a for a in firing(snap) - baseline_alerts if a[0] in ("RequestErrorRate", "RequestLatency")}
            print(f"  {int(time.time() - info['injected_at'])}s: new alerts {sorted(seen) or '-'}", flush=True)
            if seen and time.time() > info["injected_at"] + 90:  # let the picture develop a little
                break
        info["new_alerts"] = sorted(seen)
        info["symptom_observed"] = bool(seen)
        if hold:
            input(f"[{fault_id}] fault active. Snapshot: {snap}\nPress Enter to revert … ")
        else:
            time.sleep(after_s)
    finally:
        try:  # the recording ends while the fault is still active (as in ITBench-AA snapshots)
            rec.terminate()
            rec.wait(90)
        except Exception as e:  # noqa: BLE001 - nothing may stand between a fault and its revert
            rec.kill()
            info["recorder_stop_error"] = repr(e)
        finally:
            info["reverted"] = control.revert_all()
            info["reverted_at"] = time.time()
            (ep / "episode.json").write_text(json.dumps(info, indent=1))
    print(f"[{fault_id}] done → {ep.relative_to(ROOT)} (symptoms: {info['new_alerts'] or 'NONE'})")
    return ep


def record_all(baseline_s: int, rep: int = 1) -> None:
    """Record every fault once (episode runs/live/<fault>-r<rep>); finished episodes are skipped, so rerunning
    after a crash continues. A half-written episode (no episode.json) is deleted and recorded again."""
    import shutil
    todo = [f for f in FAULTS if not (LIVE / f"{f}-r{rep}" / "episode.json").exists()]
    t0 = time.time()
    for i, fid in enumerate(todo, 1):
        ep = LIVE / f"{fid}-r{rep}"
        if ep.exists():
            shutil.rmtree(ep)
        record(fid, baseline_s, name=ep.name)
        el = time.time() - t0
        print(f"=== {i}/{len(todo)} done, ETA {el / i * (len(todo) - i) / 60:.0f} min", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("fault", nargs="?", choices=sorted(FAULTS))
    ap.add_argument("--all", action="store_true", help="record every fault once (resumable)")
    ap.add_argument("--baseline", type=int, default=120)
    ap.add_argument("--hold", action="store_true")
    a = ap.parse_args()
    if a.all:
        record_all(a.baseline)
    else:
        record(a.fault, a.baseline, hold=a.hold)
