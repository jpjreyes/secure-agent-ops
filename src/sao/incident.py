"""Run one incident end to end: diagnosis (team) → remediation plan → a person's approval → execution → check.

  uv run python -m sao.incident runs/live/<episode> --live          # terminal approval, real actions
  uv run python -m sao.incident runs/live/<episode> --dry-run       # terminal approval, server-side dry run

The graph pauses at the approval step (LangGraph interrupt, checkpointed in Postgres), so an incident can wait for a
person across process restarts: rerunning the same command with the same --thread resumes it. The approver signs
exactly the calls shown (sao.gateway.approvals); every call goes through the gateway as the executor acting for the
on-call person.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import Callable
from pathlib import Path

from langgraph.types import Command

from sao import tracing
from sao.agents import sdk, team
from sao.gateway.approvals import sign_approval
from sao.gateway.local import ONCALL, running_gateway

Approver = Callable[[dict, object], dict]   # (interrupt payload, issuer) → resume value


def approve_all(payload: dict, issuer) -> dict:
    """For dry-run evals only: approves every proposed call exactly as shown."""
    calls = payload["calls"]
    token = sign_approval(issuer.signer, issuer.iss, ONCALL, calls, "eval")
    return {"decision": "approve", "calls": calls, "approver": ONCALL, "approval": token, "how": "approve-all (eval)"}


def terminal_approver(payload: dict, issuer) -> dict:
    plan, calls = payload["plan"], payload["calls"]
    print("\n=== Remediation plan ===\n" + plan.get("summary", ""))
    for i, (a, c) in enumerate(zip(plan["actions"], calls, strict=True), 1):
        print(f"\n[{i}] {c['tool']}({json.dumps({k: v for k, v in c['args'].items() if k != 'idempotency_key'})})")
        print(f"    why: {a['why']}\n    expected: {a['expected_effect']}\n    precondition: {a['precondition']}"
              f"\n    undo: {a['undo']}")
    ans = input("\nApprove these exact calls? [y]es / [n]o / [e]dit a call's arguments: ").strip().lower()
    if ans.startswith("e"):
        i = int(input("call number: ")) - 1
        calls[i]["args"] |= json.loads(input("new arguments as JSON (merged): "))
        ans = input(f"approve with call {i + 1} = {calls[i]['args']}? [y/n]: ").strip().lower()
    if not ans.startswith("y"):
        return {"decision": "reject", "calls": calls, "approver": ONCALL}
    token = sign_approval(issuer.signer, issuer.iss, ONCALL, calls, payload.get("incident", "incident"))
    return {"decision": "approve", "calls": calls, "approver": ONCALL, "approval": token, "how": "terminal"}


async def run_incident(snapshot: Path, thread_id: str, approver: Approver, incident_alerts: list, cp, gw) -> dict:
    graph = team.build().compile(checkpointer=cp)
    config = {"configurable": {"thread_id": thread_id}}
    prior = await graph.aget_state(config)
    inp = None if (prior and prior.values) else {"snapshot": str(snapshot), "setup": "C", "remediate": True,
                                                  "incident_alerts": incident_alerts}
    sdk.GATEWAY = gw
    try:
        with tracing.span("incident", **{"sao.thread": thread_id, "sao.remediate": True}) as s:
            result = await graph.ainvoke(inp, config, durability="sync")
            while result.get("__interrupt__"):
                payload = result["__interrupt__"][0].value
                answer = approver(payload, gw.issuer)
                tracing.set_attrs(s, **{"sao.approval": answer.get("decision"), "sao.approver": answer.get("approver")})
                result = await graph.ainvoke(Command(resume=answer), config, durability="sync")
    finally:
        sdk.GATEWAY = None
    return result


async def main() -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    ap = argparse.ArgumentParser()
    ap.add_argument("episode", type=Path, help="a recorded live episode (runs/live/<name>) or a snapshot folder")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="real actions on the cluster")
    mode.add_argument("--dry-run", action="store_true", help="actions validated by the API server, not applied")
    ap.add_argument("--thread", default=None)
    a = ap.parse_args()
    ep = a.episode
    snap = ep / "snapshot" if (ep / "snapshot").exists() else ep
    meta = ep / "episode.json"
    alerts = json.loads(meta.read_text()).get("expected_alerts", []) if meta.exists() else []
    thread = a.thread or f"incident-{uuid.uuid4().hex[:8]}"
    async with AsyncPostgresSaver.from_conn_string(team.DSN) as cp:
        await cp.setup()
        with running_gateway(snap, ep / "gateway" / thread, cluster=True, dry_run=a.dry_run) as gw:
            r = await run_incident(snap, thread, terminal_approver, alerts, cp, gw)
    from sao.report import build
    out = ep / "gateway" / thread
    (out / "report.md").write_text(build(r, snap, out / "audit.jsonl", title=ep.name))
    print(json.dumps({k: r.get(k) for k in ("final", "decision", "action_results", "outcome")},
                     indent=1, default=str)[:3000])
    print(f"thread {thread}\nreport: {out / 'report.md'}\naudit log: {out / 'audit.jsonl'}")


if __name__ == "__main__":
    asyncio.run(main())
