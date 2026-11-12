"""Re-run only the verifier on stored team runs (same plan, findings and draft), to measure a verifier change in
isolation and at about half the cost of a full team run.

  uv run python -m evals.replay_verify C-dev1 --out C-dev1-verify2

Writes runs/itbench/<out>/<scenario>/rep<k>.json with the new final answer and score; the draft score is the same
input as before, so the difference is the verifier's alone. Dev scenarios only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from evals.run_itbench import RUNS, SNAP, _truth
from evals.score import score
from sao.agents import team
from sao.llm import guard


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    split = json.loads((RUNS.parents[1] / "evals/split.json").read_text())
    files = sorted((RUNS / a.source).glob("*/rep*.json"))
    out_dir = RUNS / a.out
    for i, f in enumerate(files, 1):
        src = json.loads(f.read_text())
        sid = src["scenario"]
        if sid in split["test"]:
            sys.exit("replay is for dev scenarios only")
        dst = out_dir / sid / f.name
        if dst.exists():
            continue
        state = {"snapshot": str(SNAP / sid), "setup": "C", "plan": src["plan"], "findings": src["findings"],
                 "draft": src["draft"]}
        try:
            guard.before_run()
            t0 = time.time()
            upd = await team.verify(state)
        except team.NodeStop as e:
            print(f"STOPPED: {e.stop}")
            return 75
        sc = score(_truth(sid), [x["name"] for x in upd["final"]["contributing_factors"]])
        call = upd["calls"][0]
        r = {"scenario": sid, "rep": src["rep"], "setup": "C", "source": a.source, "score": sc.score,
             "recall_ok": sc.recall_ok, "tp": sc.tp, "fp": sc.fp, "missed": sc.missed,
             "draft_score": src["draft_score"], "factors": upd["final"]["contributing_factors"],
             "verdicts": upd["verdicts"], "verify_call": call, "wall_s": round(time.time() - t0, 1),
             "cost_usd_api_equivalent": call["cost_usd_api_equivalent"], "usage_windows": guard.last_windows}
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_text(json.dumps(r, indent=1, default=str))
        tmp.replace(dst)
        w = guard.last_windows or {}
        print(f"[{i}/{len(files)}] {sid}: draft {src['draft_score']:.2f} → old final {src['score']:.2f} → "
              f"new {sc.score:.2f} (fp {sc.fp}, missed {sc.missed}) ${call['cost_usd_api_equivalent']:.3f} | 5h "
              f"{w.get('five_hour', {}).get('utilization')} week {w.get('seven_day', {}).get('utilization')}",
              flush=True)
        for x in r["factors"]:
            print(f"     → {x['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
