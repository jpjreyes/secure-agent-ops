"""Setup D (evals/PLAN.md "Setup D"): the setup-A agent as the doer, then the setup-C verifier, unchanged.
Model calls are faked; the checkpointer is LangGraph's in-memory one."""

from __future__ import annotations

import asyncio
from collections import Counter

from langgraph.checkpoint.memory import MemorySaver

from sao.agents import sdk, team
from sao.agents.prompts import DIAGNOSIS_SCHEMA, TASK
from sao.llm import guard


def _fake(seen: list[dict], calls: Counter):
    async def run_agent(*, role: str, **kw) -> dict:
        calls[role] += 1
        seen.append({"role": role, **kw})
        if role == "solo":
            out = {"contributing_factors": [
                {"name": "otel-demo/Deployment/shipping", "reasoning": "bad QUOTE_ADDR", "evidence": "rev 6"},
                {"name": "otel-demo/Deployment/currency", "reasoning": "errors?", "evidence": "none"}]}
        elif role == "verifier":
            out = {"verdicts": [
                {"entity": "otel-demo/Deployment/shipping", "verdict": "cause", "explained_by": None,
                 "reasoning": "r", "evidence": "e"},
                {"entity": "otel-demo/Deployment/currency", "verdict": "unrelated", "explained_by": None,
                 "reasoning": "r", "evidence": "e"}]}
        else:
            raise AssertionError(f"setup D must not run {role}")
        return {"role": role, "output": out, "is_error": False, "stop": "success", "turns": 3, "tool_calls": [],
                "cost_usd_api_equivalent": 0.01, "input_tokens": 10, "output_tokens": 5, "wall_s": 0.1,
                "model": kw["model"]}
    return run_agent


def _run(monkeypatch):
    seen, calls = [], Counter()
    monkeypatch.setattr(sdk, "run_agent", _fake(seen, calls))
    monkeypatch.setattr(guard, "last_windows", None)

    async def go():
        graph = team.build().compile(checkpointer=MemorySaver())
        return await graph.ainvoke({"snapshot": "/tmp/snap", "setup": "D"}, {"configurable": {"thread_id": "d1"}})
    return seen, calls, asyncio.run(go())


def test_d_runs_the_setup_a_agent_then_the_verifier_only(monkeypatch):
    seen, calls, values = _run(monkeypatch)
    assert calls == Counter({"solo": 1, "verifier": 1})
    solo = seen[0]
    # the doer is setup A: same task prompt, model, every read-only tool, same turn cap and answer schema
    assert solo["system"] == TASK and solo["model"] == "sonnet" and solo["tools"] is None
    assert solo["max_turns"] == 100 and solo["schema"] is DIAGNOSIS_SCHEMA
    # the verifier is setup C's, unchanged, and rules on the doer's candidates with the doer's evidence
    ver = seen[1]
    assert ver["system"] == team.VERIFIER and ver["schema"] is team.VERDICT_SCHEMA and ver["tools"] is None
    assert ver["max_turns"] == team.TURNS["verifier"] and ver["model"] == team.MODELS["verifier"]
    assert "- otel-demo/Deployment/shipping" in ver["prompt"] and "bad QUOTE_ADDR" in ver["prompt"]


def test_d_submits_the_verified_answer_not_the_draft(monkeypatch):
    _, _, values = _run(monkeypatch)
    out = team.team_result(values, "D")
    assert [f["name"] for f in out["factors"]] == ["otel-demo/Deployment/shipping"]
    assert len(out["draft"]["contributing_factors"]) == 2
    assert [c["node"] for c in out["calls"]] == ["solo", "verify"]


def test_b_and_c_still_start_with_the_commander():
    assert team.start({"setup": "C"}) == "commander_plan"
    assert team.start({"setup": "B"}) == "commander_plan"
    assert team.start({"setup": "D"}) == "solo"


def test_d_doer_is_read_only_at_the_gateway():
    assert sdk.agent_id("solo") == "agent:investigator"
    assert sdk.agent_id("A") == "agent:investigator"
    assert sdk.agent_id("verifier") == "agent:verifier"


def _verdict_run(monkeypatch, verdicts):
    async def run_agent(*, role: str, **kw) -> dict:
        out = ({"contributing_factors": [{"name": "otel-demo/Deployment/payment", "reasoning": "r", "evidence": "e"}]}
               if role == "solo" else {"verdicts": verdicts})
        return {"role": role, "output": out, "is_error": False, "stop": "success", "turns": 1, "tool_calls": [],
                "cost_usd_api_equivalent": 0.0, "input_tokens": 0, "output_tokens": 0, "wall_s": 0.0,
                "model": kw["model"]}
    monkeypatch.setattr(sdk, "run_agent", run_agent)
    monkeypatch.setattr(guard, "last_windows", None)

    async def go():
        graph = team.build().compile(checkpointer=MemorySaver())
        return await graph.ainvoke({"snapshot": "/tmp/snap", "setup": "D", "remediate": True},
                                   {"configurable": {"thread_id": "esc"}})
    return asyncio.run(go())


def test_no_verified_cause_escalates_instead_of_resubmitting_the_draft(monkeypatch):
    values = _verdict_run(monkeypatch, [{"entity": "otel-demo/Deployment/payment", "verdict": "symptom",
                                         "explained_by": None, "reasoning": "r", "evidence": "e"}])
    assert values["final"]["contributing_factors"] == []
    assert "hand the incident to a person" in values["final"]["escalate"]
    assert "fix_plan" not in values  # nothing to fix automatically: no executor, no approval request
    assert team.team_result(values, "D")["factors"] == []


def test_a_verifier_that_gives_no_verdicts_keeps_the_draft_marked_unverified(monkeypatch):
    values = _verdict_run(monkeypatch, [])
    assert [f["name"] for f in values["final"]["contributing_factors"]] == ["otel-demo/Deployment/payment"]
    assert values["final"]["unverified"] is True
