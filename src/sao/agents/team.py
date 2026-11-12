"""Setups B and C: the incident-response team, orchestrated by LangGraph, each member a Claude Agent SDK agent.

    commander_plan ──Send──▶ investigate(changes) ─┐
         (alerts)     ├────▶ investigate(logs)    ├──▶ commander_synthesize ──▶ [C only] verify ──▶ END
                      ├────▶ investigate(traces)  │        (draft diagnosis)        (final diagnosis)
                      └────▶ investigate(metrics) ┘

- The commander reads only the overview and alerts, and assigns up to four investigators, one per signal, each with
  a focus. Investigators (Haiku) run in parallel, each with only its signal's tools, and report candidates with
  evidence, marking each as a possible cause or a symptom.
- The commander turns the findings into a draft diagnosis. In setup B that draft is the answer.
- Setup C adds the verifier (all tools): it tests every factor of the draft for "cause or symptom?" against the
  evidence, looks upstream of each one, and returns the final diagnosis. The single-agent baseline's typical errors
  were exactly symptoms listed instead of (or next to) the cause.

Durable state: every finished step is checkpointed in Postgres (thread = one episode). If the run stops mid-episode
(usage limit, crash, kill), resuming the same thread re-runs only the unfinished steps; investigators that finished
in a parallel step are not re-run (their results are stored as pending writes).
"""

from __future__ import annotations

import asyncio
import json
import operator
import os
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from sao.agents import sdk
from sao.agents.prompts import DIAGNOSIS_SCHEMA, TASK
from sao.llm import guard

DSN = os.environ.get("SAO_PG_DSN", "postgresql://sao:sao-local-only@127.0.0.1:55436/sao")

SIGNAL_TOOLS = {
    "changes": ["overview", "alerts", "events", "resources", "changes", "object_diff", "get_object"],
    "logs": ["overview", "log_errors", "search_logs"],
    "traces": ["overview", "trace_summary", "trace_errors", "dependencies"],
    "metrics": ["overview", "alerts", "metric_anomalies", "metric_series", "list_metrics"],
}

MODELS = {"commander": "sonnet", "investigator": "haiku", "verifier": "sonnet"}
TURNS = {"commander_plan": 8, "investigator": 25, "commander_synthesize": 4, "verifier": 30}


class NodeStop(Exception):
    """An EvalStop raised inside a node, carried as an ordinary exception so LangGraph records the step as failed
    (and keeps the writes of the parallel steps that finished) instead of being torn down mid-write."""

    def __init__(self, stop: guard.EvalStop):
        super().__init__(str(stop))
        self.stop = stop


class TeamState(TypedDict, total=False):
    snapshot: str
    setup: str
    plan: dict
    findings: Annotated[list[dict], operator.add]
    draft: dict
    final: dict
    calls: Annotated[list[dict], operator.add]


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "alert_summary": {"type": "string"},
        "assignments": {"type": "array", "maxItems": 4, "items": {
            "type": "object",
            "properties": {"signal": {"type": "string", "enum": list(SIGNAL_TOOLS)}, "focus": {"type": "string"}},
            "required": ["signal", "focus"]}},
    },
    "required": ["alert_summary", "assignments"],
}

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "candidates": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "namespace/Kind/name"},
                "role": {"type": "string", "enum": ["possible_cause", "symptom"]},
                "evidence": {"type": "string"},
                "first_seen": {"type": "string"},
            },
            "required": ["entity", "role", "evidence"]}},
    },
    "required": ["summary", "candidates"],
}

COMMANDER_PLAN = """You are the incident commander of an SRE team investigating a production incident in a Kubernetes
cluster from read-only telemetry. You do not investigate deeply yourself: read the overview and the firing alerts,
then assign investigators. Each investigator covers one signal and has only that signal's tools:
- changes: Kubernetes objects and events (config changes, rollouts, chaos experiments, scaling, restarts, OOMs)
- logs: error and warning logs
- traces: error spans, latency and the service dependency graph
- metrics: metric anomalies (error rate, latency, memory, CPU) over time
Assign 2 to 4 investigators. Always include `changes` (most incidents start with a change). Give each a short focus:
which services or alerts to start from and what question to answer. Summarize the alerts (names, services, start
times) for the team."""

INVESTIGATOR = """You are an SRE investigator on an incident-response team. You cover ONE signal ({signal}) with
read-only tools; other investigators cover the other signals. Your focus from the commander: {focus}

Find the entities (namespace/Kind/name, e.g. otel-demo/Deployment/checkout, otel-demo/ConfigMap/flagd-config,
chaos-mesh/NetworkChaos/x) that your signal shows are involved, and for each say whether it looks like a POSSIBLE
CAUSE (something changed or failed there first: a config or flag change, a chaos experiment, a rollout, a scale
change, a resource limit, a crash) or a SYMPTOM (it fails because something it depends on fails). Quote concrete
evidence with timestamps. Be brief; stop when you have answered the focus question. Do not guess beyond your
evidence."""

SYNTHESIZE = """You are the incident commander. Your investigators report below. Combine their findings into the
team's diagnosis following the task rules (root causes only, minimal, irreducible). Prefer the entity where the
fault ORIGINATED (the changed ConfigMap, the chaos experiment object, the Deployment whose config was changed, the
load generator) over services that merely fail because of it. Do not call any tools.

TASK RULES:
""" + TASK

VERIFIER = """You are the verifier on an SRE incident-response team. The commander drafted a diagnosis from the
investigators' findings. Your job is to catch the two most common errors before it is submitted:
1. A SYMPTOM listed as a root cause (a service that fails because something upstream failed).
2. A correct cause listed together with its own symptoms (extra entities are false positives).
Also check the opposite: the true origin may be an object nobody listed (a ConfigMap or feature flag change, a chaos
experiment object, a load generator, a scaled-down or misconfigured Deployment, a Service or NetworkPolicy change).

For each entity in the draft: find what changed or failed AT that entity and when; look for anything upstream that
changed earlier and explains it (object changes and diffs, chaos-mesh objects, flagd-config, events, dependencies,
error traces). Keep it only if it passes the irreducibility test. Replace symptoms with their upstream cause when the
evidence shows one. Return the final diagnosis (it may be identical to the draft if it holds up).

TASK RULES:
""" + TASK


def _record(r: dict, node: str) -> dict:
    return {k: r[k] for k in ("role", "model", "turns", "input_tokens", "output_tokens", "cost_usd_api_equivalent",
                              "wall_s", "is_error", "stop")} | {"node": node, "tool_calls": r["tool_calls"]}


_inflight = 0
SIBLING_WAIT_S = 900


async def _agent(node: str, **kw) -> dict:
    """Run one agent. On a stop, wait for the agents already running in parallel to finish before failing the step:
    LangGraph cancels the other tasks of a step when one fails, so raising at once would throw away (and later
    re-run) investigators that were nearly done. No new agent starts after a stop (guard.before_run refuses)."""
    global _inflight
    _inflight += 1
    try:
        return await sdk.run_agent(**kw)
    except guard.EvalStop as e:
        _inflight -= 1
        waited = 0.0
        while _inflight > 0 and waited < SIBLING_WAIT_S:
            await asyncio.sleep(0.5)
            waited += 0.5
        _inflight += 1
        raise NodeStop(e) from None
    finally:
        _inflight -= 1


async def commander_plan(state: TeamState) -> dict:
    r = await _agent("commander_plan", role="commander", system=COMMANDER_PLAN, snapshot=Path(state["snapshot"]),
                     prompt="Read the overview and alerts, then give the plan.", model=MODELS["commander"],
                     schema=PLAN_SCHEMA, tools=["overview", "alerts"], max_turns=TURNS["commander_plan"])
    plan = r["output"] or {"alert_summary": "(planning failed)", "assignments": [
        {"signal": s, "focus": "Find what changed or failed first."} for s in SIGNAL_TOOLS]}
    if not any(a["signal"] == "changes" for a in plan["assignments"]):
        plan["assignments"] = [{"signal": "changes", "focus": "What changed in the window?"}] + plan["assignments"][:3]
    return {"plan": plan, "calls": [_record(r, "commander_plan")]}


def fan_out(state: TeamState) -> list[Send]:
    seen, sends = set(), []
    for a in state["plan"]["assignments"]:
        if a["signal"] in seen:
            continue
        seen.add(a["signal"])
        sends.append(Send("investigate", {"snapshot": state["snapshot"], "signal": a["signal"], "focus": a["focus"],
                                          "alert_summary": state["plan"]["alert_summary"]}))
    return sends


async def investigate(task: dict) -> dict:
    sig = task["signal"]
    r = await _agent("investigate", role=f"investigator:{sig}",
                     system=INVESTIGATOR.format(signal=sig, focus=task["focus"]), snapshot=Path(task["snapshot"]),
                     prompt=f"Alerts (from the commander): {task['alert_summary']}\n\nInvestigate and report.",
                     model=MODELS["investigator"], schema=FINDINGS_SCHEMA, tools=SIGNAL_TOOLS[sig],
                     max_turns=TURNS["investigator"])
    out = r["output"] or {"summary": f"(investigator failed: {r['stop']})", "candidates": []}
    return {"findings": [{"signal": sig, **out}], "calls": [_record(r, f"investigate:{sig}")]}


def _findings_text(state: TeamState) -> str:
    return json.dumps({"alerts": state["plan"]["alert_summary"], "findings": state["findings"]}, indent=1)


async def commander_synthesize(state: TeamState) -> dict:
    r = await _agent("commander_synthesize", role="commander", system=SYNTHESIZE, snapshot=Path(state["snapshot"]),
                     prompt="Investigator reports:\n" + _findings_text(state) + "\n\nGive the JSON diagnosis.",
                     model=MODELS["commander"], schema=DIAGNOSIS_SCHEMA, tools=[],
                     max_turns=TURNS["commander_synthesize"])
    return {"draft": r["output"] or {"contributing_factors": []}, "calls": [_record(r, "commander_synthesize")]}


async def verify(state: TeamState) -> dict:
    r = await _agent("verify", role="verifier", system=VERIFIER, snapshot=Path(state["snapshot"]),
                     prompt="Investigator reports:\n" + _findings_text(state) + "\n\nDraft diagnosis:\n"
                            + json.dumps(state["draft"], indent=1) + "\n\nVerify and give the final JSON diagnosis.",
                     model=MODELS["verifier"], schema=DIAGNOSIS_SCHEMA, tools=None, max_turns=TURNS["verifier"])
    final = r["output"] if r["output"].get("contributing_factors") else state["draft"]
    return {"final": final, "calls": [_record(r, "verify")]}


def after_synthesis(state: TeamState) -> str:
    return "verify" if state.get("setup") == "C" else END


def build() -> StateGraph:
    g = StateGraph(TeamState)
    g.add_node("commander_plan", commander_plan)
    g.add_node("investigate", investigate)
    g.add_node("commander_synthesize", commander_synthesize)
    g.add_node("verify", verify)
    g.add_edge(START, "commander_plan")
    g.add_conditional_edges("commander_plan", fan_out, ["investigate"])
    g.add_edge("investigate", "commander_synthesize")
    g.add_conditional_edges("commander_synthesize", after_synthesis, ["verify", END])
    g.add_edge("verify", END)
    return g


async def diagnose(snapshot: Path, setup: str, thread_id: str, checkpointer) -> dict:
    """Run (or resume) one episode. Same return shape as setup A, plus per-agent calls and whether it resumed."""
    graph = build().compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    prior = await graph.aget_state(config)
    resumed = bool(prior and prior.values)
    if resumed and not prior.next:  # finished earlier (e.g. stopped after the graph ended, before saving the result)
        values = prior.values
    else:
        guard.before_run()
        inp = None if resumed else {"snapshot": str(snapshot), "setup": setup}
        try:
            values = await graph.ainvoke(inp, config, durability="sync")
        except NodeStop as e:
            raise e.stop from None
    calls = values.get("calls", [])
    answer = values.get("final") if setup == "C" else values.get("draft")
    return {"factors": (answer or {}).get("contributing_factors", []), "is_error": answer is None,
            "draft": values.get("draft"), "plan": values.get("plan"), "findings": values.get("findings"),
            "calls": calls, "resumed": resumed, "turns": sum(c["turns"] or 0 for c in calls),
            "tool_calls": [t for c in calls for t in c["tool_calls"]],
            "cost_usd_api_equivalent": round(sum(c["cost_usd_api_equivalent"] for c in calls), 4),
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "wall_s": round(sum(c["wall_s"] for c in calls), 1), "usage_windows": guard.last_windows,
            "models": MODELS}
