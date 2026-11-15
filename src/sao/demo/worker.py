"""One live demo run: plant the visitor's attack in a recorded incident, run the full team (setup C, screening on)
through the gateway, and keep a run document up to date for the page to poll.

The team, the gateway, Cedar, screening and the audit chain are the code the evals ran; what changes here is where
state lives (S3 / DynamoDB on AWS), that the issuer signs with KMS, and that the remediation stops at the approval
step: a visitor can't sign, so the plan is shown as "waiting for a person's signature" and nothing executes.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import yaml

from sao.demo import attack, store

ROOT = Path(__file__).resolve().parents[3]
SNAPS = Path(os.environ.get("SAO_DEMO_SNAPSHOTS", ROOT / "data" / "itbench-aa" / "snapshots"))
TRUTH = Path(os.environ.get("SAO_DEMO_TRUTH", ROOT / "data" / "itbench-aa" / "truth"))
WORK = Path(os.environ.get("SAO_DEMO_WORK", "/tmp/sao-work"))

INCIDENTS = {
    "Scenario-9": {"title": "Checkout slows to a crawl", "family": "feature flag",
                   "story": "A feature flag was flipped and the storefront proxy started timing out."},
    "Scenario-24": {"title": "A service stops answering", "family": "Kubernetes",
                    "story": "A Kubernetes change left one service unable to serve; errors spread downstream."},
    "Scenario-21": {"title": "Pods keep dying", "family": "chaos",
                    "story": "A chaos experiment kills pods of one service; the others start failing."},
}


def truth(sid: str) -> list[dict]:
    f = TRUTH / f"{sid}.yaml"
    if not f.exists():
        return []
    return [{"entity": x["entity"]["name"], "kind": x["entity"].get("kind"), "condition": x.get("condition")}
            for x in (yaml.safe_load(f.read_text()) or {}).get("fault", [])]


def _summary(node: str, upd: dict) -> dict:
    """What the page shows for one finished team step (no raw telemetry)."""
    out: dict = {"node": node, "t": time.time()}
    calls = upd.get("calls") or []
    if calls:
        c = calls[0]
        out |= {"agent": c["role"], "model": c["model"], "turns": c["turns"], "tool_calls": len(c["tool_calls"]),
                "cost": c["cost_usd_api_equivalent"], "wall_s": c["wall_s"], "stop": c["stop"]}
    if node == "commander_plan":
        out["plan"] = upd.get("plan")
    elif node == "investigate":
        f = (upd.get("findings") or [{}])[0]
        out["finding"] = {"signal": f.get("signal"), "summary": f.get("summary"),
                          "candidates": [{"entity": x.get("entity") or x.get("name"), "role": x.get("role"),
                                          "evidence": (x.get("evidence") or "")[:400]}
                                         for x in (f.get("candidates") or [])][:6]}
    elif node == "commander_synthesize":
        out["draft"] = upd.get("draft")
    elif node == "verify":
        out |= {"verdicts": upd.get("verdicts"), "final": upd.get("final")}
    elif node == "plan_fix":
        out["fix_plan"] = upd.get("fix_plan")
    return out


class Flusher:
    """Writes the run document every ~1.5 s while the run goes (and once more at the end)."""

    def __init__(self, run_id: str, doc: dict, audit_path: Path):
        self.run_id, self.doc, self.audit_path = run_id, doc, audit_path
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def audit(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        out = []
        for line in self.audit_path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                break  # a line being written
        return out

    def flush(self) -> None:
        self.doc["audit"] = self.audit()
        self.doc["updated"] = time.time()
        store.put_run(self.run_id, self.doc)

    def _loop(self) -> None:
        while not self._stop.wait(1.5):
            try:
                self.flush()
            except Exception as e:  # noqa: BLE001 - a failed flush must not stop the run; the next one retries
                self.doc.setdefault("flush_errors", []).append(str(e)[:200])

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(5)
        self.flush()


async def run(run_id: str, incident: str, preset: str | None, custom: str | None, clean: bool = False) -> dict:
    """`clean`: no attack and no remediation, the evals' diagnosis setting (the API parity check only; the public
    API never sets it, it is reachable by invoking the function directly, which needs IAM rights)."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from sao.agents import sdk, team
    from sao.gateway.local import running_gateway

    doc = store.get_run(run_id) or {}
    doc |= {"id": run_id, "incident": incident, "incident_info": INCIDENTS.get(incident), "truth": truth(incident),
            "status": "running", "phase": "planting", "started": time.time(), "steps": []}
    work = WORK / run_id
    if clean:
        text, goal, snap = "", "none", SNAPS / incident
        doc["attack"] = None
    else:
        text, goal = attack.attack_text(preset, custom)
        doc["attack"] = {"preset": preset if goal != "custom" else None, "goal": goal, "text": text}
        snap = work / "snapshot"
        doc["attack"] |= attack.plant(SNAPS / incident, snap, text)
    audit_dir = work / "gateway"
    ks = store.RunKillSwitch(run_id)
    with Flusher(run_id, doc, audit_dir / "audit.jsonl") as fl:
        try:
            with running_gateway(snap, audit_dir, cluster=False, screening=True, killswitch=ks) as gw:
                sdk.GATEWAY = gw
                graph = team.build().compile(checkpointer=InMemorySaver())
                config = {"configurable": {"thread_id": run_id}}
                inp = {"snapshot": str(snap), "setup": "C", "remediate": not clean, "incident_alerts": []}
                doc["phase"] = "investigating"
                values: dict = {}
                while True:
                    interrupted = None
                    async for chunk in graph.astream(inp, config, stream_mode="updates"):
                        for node, upd in chunk.items():
                            if node == "__interrupt__":
                                interrupted = upd[0].value
                                continue
                            doc["steps"].append(_summary(node, upd or {}))
                            doc["phase"] = {"commander_plan": "investigating", "investigate": "investigating",
                                            "commander_synthesize": "verifying", "verify": "planning fix",
                                            "plan_fix": "awaiting signature"}.get(node, doc["phase"])
                        if store.killed_at(run_id) is not None:
                            break
                    if store.killed_at(run_id) is not None or interrupted is None:
                        break
                    # A visitor can't sign: the plan stops here, exactly as the approval step would wait for a person.
                    doc["approval"] = {"status": "waiting for a person's signature", "calls": interrupted["calls"]}
                    inp = Command(resume={"decision": "reject", "calls": interrupted["calls"],
                                          "approver": "nobody (public demo)", "how": "demo: plan only"})
                values = (await graph.aget_state(config)).values
                sdk.GATEWAY = None
            res = team.team_result(values, "C")
            out_text = json.dumps({k: values.get(k) for k in ("final", "draft", "findings", "fix_plan")}, default=str)
            doc["result"] = {"factors": res["factors"], "escalate": (values.get("final") or {}).get("escalate"),
                             "fix_plan": values.get("fix_plan"), "cost": res["cost_usd_api_equivalent"],
                             "agent_wall_s": res["wall_s"],
                             "hijacked": None if clean else attack.hijacked(goal, res["factors"],
                                                                            values.get("fix_plan"), out_text)}
            at = store.killed_at(run_id)
            if at is not None:
                after = [r for r in fl.audit() if r.get("ts", 0) >= at]
                denied = [r for r in after if r.get("decision") == "deny"]
                # The switch takes effect on the next request (each one re-reads it); what matters is that nothing
                # got through after it, and how long until an agent next tried.
                doc["kill"] = {"at": at, "allowed_after": sum(r.get("decision") == "allow" for r in after),
                               "denied_after": len(denied),
                               "next_request_s": round(after[0]["ts"] - at, 2) if after else None}
                doc["status"] = "killed"
            else:
                doc["status"] = "done"
        except Exception as e:  # noqa: BLE001 - the page shows the failure instead of polling forever
            doc["status"], doc["error"] = "error", f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            sdk.GATEWAY = None
            doc["phase"] = doc["status"]
            doc["finished"] = time.time()
    try:
        doc["audit_locked_version"] = store.lock_audit(run_id, audit_dir / "audit.jsonl")
        store.put_run(run_id, doc)
    except Exception as e:  # noqa: BLE001
        doc["audit_lock_error"] = str(e)[:200]
        store.put_run(run_id, doc)
    return doc


def _load_key() -> None:
    """On AWS the demo key lives in SSM (SecureString); it is read once per container, never logged or returned."""
    param = os.environ.get("SAO_API_KEY_PARAM")
    if param and not os.environ.get("ANTHROPIC_API_KEY"):
        import boto3
        os.environ["ANTHROPIC_API_KEY"] = boto3.client("ssm").get_parameter(
            Name=param, WithDecryption=True)["Parameter"]["Value"]


def handler(event, _context=None):
    """Lambda entry point (invoked asynchronously by the API)."""
    _load_key()
    return asyncio.run(run(event["run_id"], event["incident"], event.get("preset"), event.get("custom"),
                           clean=bool(event.get("clean"))))["status"]


if __name__ == "__main__":
    import sys
    import uuid
    rid = f"local-{uuid.uuid4().hex[:6]}"
    d = asyncio.run(run(rid, sys.argv[1] if len(sys.argv) > 1 else "Scenario-24",
                        sys.argv[2] if len(sys.argv) > 2 else "action", None))
    print(json.dumps({k: d.get(k) for k in ("id", "status", "result", "kill", "error")}, indent=1, default=str))
