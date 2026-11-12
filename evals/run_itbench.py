"""Run a setup on ITBench-AA scenarios, score every run, save each run atomically.

  uv run python -m evals.run_itbench --setup A --split dev --repeats 1 --model sonnet
  uv run python -m evals.run_itbench --setup A --ids Scenario-24 --repeats 1

Results: runs/itbench/<setup>-<model>/<scenario>/rep<k>.json (a finished run is never redone; a run cut off by the
usage stop leaves no file and is redone next time). Prints progress with ETA and the usage windows after each run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from evals.score import Truth, score
from sao.llm import guard

ROOT = Path(__file__).resolve().parents[1]
SNAP, TRUTH, RUNS = (ROOT / "data/itbench-aa" / d for d in ("snapshots", "truth", "runs"))
RUNS = ROOT / "runs" / "itbench"


async def run_one(setup: str, sid: str, model: str) -> dict:
    if setup == "A":
        from sao.agents.single import diagnose
        return await diagnose(SNAP / sid, model=model)
    raise NotImplementedError(f"setup {setup} not built yet")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", default="A", choices=["A", "B", "C"])
    ap.add_argument("--split", choices=["dev", "test"])
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args()
    split = json.loads((ROOT / "evals/split.json").read_text())
    ids = a.ids or split[a.split or "dev"]
    if a.split == "test" or any(i in split["test"] for i in ids):
        if input("This touches TEST scenarios (scored once, see evals/PLAN.md). Type 'test' to continue: ") != "test":
            return
    jobs = [(sid, k) for sid in ids for k in range(1, a.repeats + 1)]
    out_dir = RUNS / f"{a.setup}-{a.model}"
    todo = [(s, k) for s, k in jobs if not (out_dir / s / f"rep{k}.json").exists()]
    print(f"{len(todo)} runs to do ({len(jobs) - len(todo)} already done) → {out_dir.relative_to(ROOT)}", flush=True)
    t0 = time.time()
    for i, (sid, k) in enumerate(todo, 1):
        try:
            r = asyncio.run(run_one(a.setup, sid, a.model))
        except guard.EvalStop as e:
            print(f"STOPPED before {sid} rep{k}: {e}\nRe-run the same command after the reset to continue.")
            return
        sc = score(Truth.from_yaml((TRUTH / f"{sid}.yaml").read_text()), [f["name"] for f in r["factors"]])
        r.update(scenario=sid, rep=k, setup=a.setup, score=sc.score, recall_ok=sc.recall_ok, tp=sc.tp, fp=sc.fp,
                 missed=sc.missed)
        d = out_dir / sid
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".rep{k}.tmp"
        tmp.write_text(json.dumps(r, indent=1, default=str))
        tmp.replace(d / f"rep{k}.json")
        el = time.time() - t0
        w = r.get("usage_windows") or {}
        print(f"[{i}/{len(todo)}] {sid} rep{k}: score {sc.score:.2f} (tp {sc.tp}, fp {sc.fp}, missed {sc.missed}) "
              f"{r['turns']} turns, {len(r['tool_calls'])} tool calls, {r['wall_s']}s, "
              f"~${r['cost_usd_api_equivalent'] or 0:.2f} API-eq | usage 5h "
              f"{w.get('five_hour', {}).get('utilization')} week {w.get('seven_day', {}).get('utilization')} | "
              f"ETA {el / i * (len(todo) - i) / 60:.0f} min", flush=True)
        for f in r["factors"]:
            print(f"     → {f['name']}")


if __name__ == "__main__":
    main()
