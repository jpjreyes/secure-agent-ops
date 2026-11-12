"""Run a setup on ITBench-AA scenarios, score every run, save each run atomically; stop-safe and resumable.

  uv run python -m evals.run_itbench --setup A --split dev --repeats 1 --model sonnet
  uv run python -m evals.run_itbench --setup C --split dev --repeats 1
  uv run python -m evals.run_itbench --setup C --ids Scenario-24 --repeats 1
  scripts/run_eval.sh --setup C --split dev --repeats 3      (waits for usage resets and resumes on its own)

Results: runs/itbench/<setup>-<tag>/<scenario>/rep<k>.json. A finished run is never redone.

Stops (the 80% usage rule, a crash, a kill) lose at most the agent calls in flight:
- Before each episode the runner checks usage + the measured cost of one episode (--reserve), so the stop usually
  lands between episodes.
- Setups B and C checkpoint every finished step in Postgres (thread = setup/tag/scenario/rep). Rerunning the same
  command resumes an interrupted episode from its last finished step; results record `resumed: true`.
- Setup A has no checkpoint: an interrupted run leaves no file and is redone.
- run_config.json pins models, turn caps and prompts per results folder; resuming with a different configuration is
  refused, so one folder never mixes two configurations.

Exit codes: 0 done · 75 usage stop (prints EVAL_RETRY_AT=<epoch>) · 2 stop for good (paid extra usage, unclean
environment).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

from evals.score import Truth, score
from sao.llm import guard

ROOT = Path(__file__).resolve().parents[1]
SNAP, TRUTH = (ROOT / "data/itbench-aa" / d for d in ("snapshots", "truth"))
RUNS = ROOT / "runs" / "itbench"


def team_config() -> dict:
    from sao.agents import team
    prompts = "".join([team.COMMANDER_PLAN, team.INVESTIGATOR, team.SYNTHESIZE, team.VERIFIER])
    return {"models": team.MODELS, "turns": team.TURNS, "signal_tools": team.SIGNAL_TOOLS,
            "prompts_sha": hashlib.sha256(prompts.encode()).hexdigest()[:12]}


def pin_config(out_dir: Path, cfg: dict) -> None:
    p = out_dir / "run_config.json"
    if p.exists():
        old = json.loads(p.read_text())
        if old != cfg:
            sys.exit(f"{p} was written with a different configuration:\n old {old}\n new {cfg}\n"
                     "Use a new --tag for the new configuration.")
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=1))


async def run_one(setup: str, sid: str, k: int, model: str, tag: str, cp) -> dict:
    if setup == "A":
        from sao.agents.single import diagnose
        return await diagnose(SNAP / sid, model=model)
    from sao.agents import team
    return await team.diagnose(SNAP / sid, setup, f"{setup}-{tag}-{sid}-rep{k}", cp)


def _truth(sid: str) -> Truth:
    return Truth.from_yaml((TRUTH / f"{sid}.yaml").read_text())


async def run_all(a, todo: list[tuple[str, int]], out_dir: Path) -> int:
    cp_ctx = None
    if a.setup in ("B", "C"):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        from sao.agents import team
        cp_ctx = AsyncPostgresSaver.from_conn_string(team.DSN)
    cp = await cp_ctx.__aenter__() if cp_ctx else None
    try:
        if cp:
            await cp.setup()
        t0 = time.time()
        for i, (sid, k) in enumerate(todo, 1):
            try:
                guard.before_run(reserve=a.reserve)
                start = time.time()
                r = await run_one(a.setup, sid, k, a.model, a.tag, cp)
                r["wall_s"] = round(time.time() - start, 1)
            except guard.QuotaStop as e:
                print(f"STOPPED before finishing {sid} rep{k}: {e}", flush=True)
                if e.final:
                    return 2
                print(f"EVAL_RETRY_AT={int(e.retry_at or time.time() + 1800)}", flush=True)
                return 75
            except guard.EvalStop as e:
                print(f"STOPPED for good: {e}", flush=True)
                return 2
            sc = score(_truth(sid), [f["name"] for f in r["factors"]])
            r.update(scenario=sid, rep=k, setup=a.setup, score=sc.score, recall_ok=sc.recall_ok, tp=sc.tp, fp=sc.fp,
                     missed=sc.missed)
            if a.setup == "C" and r.get("draft") is not None:  # what the draft scored: the verifier's effect
                r["draft_score"] = score(_truth(sid), [f["name"] for f in r["draft"].get("contributing_factors",
                                                                                          [])]).score
            d = out_dir / sid
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f".rep{k}.tmp"
            tmp.write_text(json.dumps(r, indent=1, default=str))
            tmp.replace(d / f"rep{k}.json")
            el = time.time() - t0
            w = r.get("usage_windows") or {}
            extra = f" (draft {r['draft_score']:.2f})" if "draft_score" in r else ""
            print(f"[{i}/{len(todo)}] {sid} rep{k}: score {sc.score:.2f}{extra} (tp {sc.tp}, fp {sc.fp}, missed "
                  f"{sc.missed}){' [resumed]' if r.get('resumed') else ''} {r['turns']} turns, "
                  f"{len(r['tool_calls'])} tool calls, {r['wall_s']}s, ~${r['cost_usd_api_equivalent'] or 0:.2f} "
                  f"API-eq | usage 5h {w.get('five_hour', {}).get('utilization')} week "
                  f"{w.get('seven_day', {}).get('utilization')} | ETA {el / i * (len(todo) - i) / 60:.0f} min",
                  flush=True)
            for f in r["factors"]:
                print(f"     → {f['name']}", flush=True)
        return 0
    finally:
        if cp_ctx:
            await cp_ctx.__aexit__(None, None, None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", default="A", choices=["A", "B", "C"])
    ap.add_argument("--split", choices=["dev", "test"])
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--model", default="sonnet", help="setup A only (the team's models are in sao.agents.team)")
    ap.add_argument("--tag", default="v1", help="B/C: names the results folder; change it when the team changes")
    ap.add_argument("--reserve", type=float, default=0.0,
                    help="fraction of the usage window one episode needs (measured); an episode starts only if it "
                         "fits below the 80%% stop")
    a = ap.parse_args()
    split = json.loads((ROOT / "evals/split.json").read_text())
    ids = a.ids or split[a.split or "dev"]
    if a.split == "test" or any(i in split["test"] for i in ids):
        if input("This touches TEST scenarios (scored once, see evals/PLAN.md). Type 'test' to continue: ") != "test":
            return
    if a.setup == "A":
        out_dir = RUNS / f"A-{a.model}"
    else:
        out_dir = RUNS / f"{a.setup}-{a.tag}"
        pin_config(out_dir, {"setup": a.setup, **team_config()})
    jobs = [(sid, k) for sid in ids for k in range(1, a.repeats + 1)]
    todo = [(s, k) for s, k in jobs if not (out_dir / s / f"rep{k}.json").exists()]
    print(f"{len(todo)} runs to do ({len(jobs) - len(todo)} already done) → {out_dir.relative_to(ROOT)}", flush=True)
    sys.exit(asyncio.run(run_all(a, todo, out_dir)))


if __name__ == "__main__":
    main()
