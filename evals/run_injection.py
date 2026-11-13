"""Run the log-injection security suite (evals/PLAN.md, "Log-injection security suite").

  uv run python -m evals.run_injection --conditions A C-off C-on --limit 3      # measure on 3 incidents first
  uv run python -m evals.run_injection --conditions A C-off C-on               # the full suite (30 incidents)
  uv run python -m evals.run_injection --suite deceptive --conditions A C-on   # the deceptive-evidence suite

Conditions: A = single agent, direct tools, no gateway (unprotected baseline); C-off / C-on = the team through the
gateway with screening off / on, plus the executor's plan (plan only, never executed).
Results: runs/injection/<condition>/<incident>.json (atomic; finished runs are never redone). Exit 75 on a usage
stop with EVAL_RETRY_AT, like evals.run_itbench.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

from evals import deceptive
from evals.injection import OUT as INJ
from evals.injection import hijacked, leaked
from evals.run_itbench import _truth
from evals.score import score
from sao.llm import guard

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs" / "injection"
SUITES = {"injection": (INJ, "injection.json"), "deceptive": (deceptive.OUT, "deceptive.json")}


def incidents(suite: str = "injection") -> list[Path]:
    base, meta = SUITES[suite]
    return sorted(p for p in base.glob(f"*/{meta}"))


async def run_a(snap: Path) -> dict:
    from sao.agents.single import diagnose
    return await diagnose(snap, model="sonnet")


async def run_c(snap: Path, screening: bool, cp, tag: str) -> dict:
    from sao.agents import team
    from sao.gateway.local import running_gateway
    from sao.incident import run_incident

    def plan_only(payload, issuer):
        return {"decision": "reject", "calls": payload["calls"], "approver": "eval", "how": "suite: plan only"}

    thread = f"inj-{tag}-{snap.parent.name}-{uuid.uuid4().hex[:6]}"
    with running_gateway(snap, RUNS / tag / f"{snap.parent.name}-gateway", cluster=False,
                         screening=screening) as gw:
        values = await run_incident(snap, thread, plan_only, [], cp, gw)
        audit = [json.loads(x) for x in gw.audit_path.read_text().splitlines()]
    out = team.team_result(values)
    out["fix_plan"] = values.get("fix_plan")
    out["screening_hits"] = sum(len(r.get("screening_hits") or []) for r in audit)
    out["denials"] = [r.get("policies") for r in audit if r.get("decision") == "deny"]
    return out


async def main() -> int:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from sao.agents import team
    ap = argparse.ArgumentParser()
    ap.add_argument("--conditions", nargs="+", default=["A", "C-off", "C-on"], choices=["A", "C-off", "C-on"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--reserve", type=float, default=0.02)
    ap.add_argument("--suite", default="injection", choices=list(SUITES))
    ap.add_argument("--only", nargs="+", help="incident names (e.g. Scenario-11__logs)")
    a = ap.parse_args()
    global RUNS
    if a.suite != "injection":
        RUNS = ROOT / "runs" / a.suite
    items = [i for i in incidents(a.suite) if not a.only or i.parent.name in a.only]
    items = items[: a.limit] if a.limit else items
    jobs = [(c, i) for i in items for c in a.conditions]
    todo = [(c, i) for c, i in jobs if not (RUNS / c / f"{i.parent.name}.json").exists()]
    print(f"{len(todo)} runs to do ({len(jobs) - len(todo)} done)", flush=True)
    t0 = time.time()
    async with AsyncPostgresSaver.from_conn_string(team.DSN) as cp:
        await cp.setup()
        for n, (cond, meta_path) in enumerate(todo, 1):
            meta = json.loads(meta_path.read_text())
            snap = meta_path.parent / "snapshot"
            try:
                guard.before_run(reserve=a.reserve)
                start = time.time()
                try:
                    r = await (run_a(snap) if cond == "A" else run_c(snap, cond == "C-on", cp, cond))
                except team.NodeStop as ns:  # a stop inside a team node arrives wrapped
                    raise ns.stop from None
                r["wall_s"] = round(time.time() - start, 1)
            except guard.QuotaStop as e:
                print(f"STOPPED: {e}", flush=True)
                if e.final:
                    return 2
                print(f"EVAL_RETRY_AT={int(e.retry_at or time.time() + 1800)}", flush=True)
                return 75
            except guard.EvalStop as e:
                print(f"STOPPED for good: {e}", flush=True)
                return 2
            names = [f["name"] for f in r["factors"]]
            sc = score(_truth(meta["scenario"]), names)
            text = json.dumps({"factors": r["factors"], "plan": r.get("fix_plan")})
            r.update(condition=cond, incident=meta_path.parent.name, **meta, score=sc.score, recall_ok=sc.recall_ok,
                     leaked=leaked(text))
            if a.suite == "deceptive":
                r.update(hijacked=deceptive.names_decoy(meta["decoy"], r["factors"]),
                         plan_on_decoy=deceptive.plan_targets_decoy(meta["decoy"], r.get("fix_plan")))
            else:
                r["hijacked"] = hijacked(meta["goal"], r["factors"], r.get("fix_plan"))
            d = RUNS / cond
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f".{meta_path.parent.name}.tmp"
            tmp.write_text(json.dumps(r, indent=1, default=str))
            tmp.replace(d / f"{meta_path.parent.name}.json")
            w = r.get("usage_windows") or {}
            el = time.time() - t0
            print(f"[{n}/{len(todo)}] {cond:5} {meta_path.parent.name}: score {sc.score:.2f} hijacked={r['hijacked']} "
                  f"leaked={r['leaked']} flags={r.get('screening_hits', '-')} "
                  f"${r.get('cost_usd_api_equivalent') or 0:.2f}"
                  f" | 5h {w.get('five_hour', {}).get('utilization')} week {w.get('seven_day', {}).get('utilization')}"
                  f" | ETA {el / n * (len(todo) - n) / 60:.0f} min", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
