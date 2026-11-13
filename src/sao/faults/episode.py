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
from sao.faults.catalog import ALL_FAULTS, FAULTS, HARD

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


BACKGROUND_NOISE = {"flagd"}


def _alerts_by_time(snapshot: Path) -> list[tuple[float, set[tuple[str, str]]]]:
    out = []
    for f in sorted(glob.glob(str(snapshot / "alerts" / "*.json"))):
        stamp = Path(f).stem.removeprefix("alerts_at_")  # 2026-11-12T19-14-43.616290Z
        day, clock = stamp.split("T")
        ts = datetime.strptime(f"{day}T{clock.replace('-', ':')}", "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        try:
            d = json.loads(Path(f).read_text())
        except json.JSONDecodeError:
            continue
        out.append((ts.timestamp(), {(a["labels"].get("alertname"), a["labels"].get("service_name", ""))
                                     for a in d["data"]["alerts"] if a.get("state") == "firing"}))
    return out


def assess(snapshot: Path, injected_at: float, expected: list[str]) -> dict:
    """Which service alerts appeared after injection that were never firing before it (baseline = every alert seen
    at any time before injection, so flapping alerts don't count), and which of them are on expected services."""
    series = _alerts_by_time(snapshot)
    before = set().union(*[a for t, a in series if t < injected_at]) if series else set()
    after = set().union(*[a for t, a in series if t >= injected_at]) if series else set()
    new = {a for a in after - before if a[0] in ("RequestErrorRate", "RequestLatency")}
    on_expected = sorted(a for a in new if a[1] in expected)
    # an app already failing before injection; flagd is excluded: on an idle cluster it logs a steady trickle of
    # stream-timeout errors (3.3 per 10 min measured, every other service at 0), i.e. background noise, not a fault
    dirty = sorted(a for a in before if a[0] == "RequestErrorRate" and a[1] not in BACKGROUND_NOISE)
    return {"new_alerts": sorted(new), "expected_alerts": on_expected, "symptom_observed": bool(on_expected),
            "baseline_alerts": sorted(before), "baseline_errors": dirty, "baseline_clean": not dirty}


def wait_quiet(max_s: int = 600, poll_s: int = 20) -> float:
    """Wait until no error alert has fired on two checks in a row: the previous fault's errors stay in the 2-minute
    rate window after its revert, and a baseline that starts too early inherits them (D13)."""
    from sao.agents.remediate import live_alerts
    t0, quiet = time.time(), 0
    while time.time() - t0 < max_s:
        errors = {a for a in live_alerts() if a[0] == "RequestErrorRate"}
        quiet = quiet + 1 if not errors else 0
        if quiet >= 2:
            break
        time.sleep(poll_s)
    return round(time.time() - t0, 1)


def record(fault_id: str, baseline_s: int = 120, after_s: int = 30, hold: bool = False,
           name: str | None = None) -> Path:
    f = ALL_FAULTS[fault_id]
    reverted = control.revert_all()
    if reverted:
        print(f"reverted leftover faults: {reverted}")
    for _ in range(12):  # restarts from the previous revert may still be rolling out
        ok, bad = control.healthy()
        if ok:
            break
        time.sleep(10)
    if not ok:
        raise SystemExit(f"cluster not healthy before the episode: {bad}")
    info_quiet = wait_quiet()
    ep = LIVE / (name or f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{fault_id}")
    snap = ep / "snapshot"
    snap.mkdir(parents=True)
    (ep / "truth.yaml").write_text(f.truth_yaml())
    info = {"fault": fault_id, "family": f.family, "root": f.root, "expected_symptoms": f.symptoms,
            "quiet_wait_s": info_quiet}
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
        info.update(assess(snap, info["injected_at"], f.symptoms))
        if hold:
            input(f"[{fault_id}] fault active. Snapshot: {snap}\nPress Enter to revert … ")
        else:
            time.sleep(after_s)
        info["complete"] = True  # only a normal finish; an interrupted episode is recorded again
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


def record_all(baseline_s: int, rep: int = 1, ids: list[str] | None = None) -> None:
    """Record every fault once (episode runs/live/<fault>-r<rep>); finished episodes are skipped, so rerunning
    after a crash continues. A half-written episode (no episode.json) is deleted and recorded again."""
    import shutil
    def complete(fid: str) -> bool:
        p = LIVE / f"{fid}-r{rep}" / "episode.json"
        return p.exists() and json.loads(p.read_text()).get("complete", False)

    todo = [f for f in (ids or FAULTS) if not complete(f)]
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
    ap.add_argument("fault", nargs="?", choices=sorted(ALL_FAULTS))
    ap.add_argument("--all", action="store_true", help="record every fault once (resumable)")
    ap.add_argument("--hard", action="store_true", help="record every harder episode once (resumable)")
    ap.add_argument("--baseline", type=int, default=120)
    ap.add_argument("--hold", action="store_true")
    a = ap.parse_args()
    if a.all or a.hard:
        record_all(a.baseline, ids=sorted(HARD) if a.hard else None)
    else:
        record(a.fault, a.baseline, hold=a.hold)
