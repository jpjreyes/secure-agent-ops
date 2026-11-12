"""Remediation end to end without model calls (D12): fake agents, the real gateway with the cluster actions in
server-side dry run, the Postgres checkpointer, the approval pause and resume.

- approve → the exact calls run (dry run) through the gateway, results recorded;
- reject → nothing runs;
- a plan that slipped in "scale to 0" (as a poisoned log could suggest) is approved and still blocked by Cedar;
- a precondition that no longer holds stops execution before any call;
- a plan is scored against the fault's known remedy.
Skipped without the cluster or the database."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from sao import kube
from sao.agents import remediate, sdk, team
from sao.faults.catalog import REMEDIES
from sao.incident import approve_all, run_incident

SNAP = Path(__file__).resolve().parents[1] / "data/itbench-aa/snapshots/Scenario-24"


def _ready() -> bool:
    try:
        import psycopg
        psycopg.connect(team.DSN, connect_timeout=2).close()
        kube.kubectl("get", "ns", "otel-demo", timeout=10)
        return SNAP.exists()
    except Exception:  # noqa: BLE001
        return False


def _fake(plan: dict):
    async def run_agent(*, role: str, schema, **kw) -> dict:
        if schema is team.PLAN_SCHEMA:
            out = {"alert_summary": "checkout errors", "assignments": [{"signal": "changes", "focus": "f"}]}
        elif role.startswith("investigator"):
            out = {"summary": "s", "candidates": [{"entity": "otel-demo/Deployment/checkout", "role": "possible_cause",
                                                   "evidence": "env changed"}]}
        elif role == "verifier":
            out = {"verdicts": [{"entity": "otel-demo/Deployment/checkout", "verdict": "cause", "explained_by": None,
                                 "reasoning": "r", "evidence": "e"}]}
        elif role == "executor":
            out = plan
        else:
            out = {"contributing_factors": [{"name": "otel-demo/Deployment/checkout", "reasoning": "r",
                                             "evidence": "e"}]}
        return {"role": role, "output": out, "is_error": False, "stop": "success", "turns": 1, "tool_calls": [],
                "cost_usd_api_equivalent": 0.0, "input_tokens": 0, "output_tokens": 0, "wall_s": 0.0, "model": "fake"}
    return run_agent


def _action(tool, args, pc):
    return {"tool": tool, "args": args, "why": "w", "expected_effect": "e", "precondition": pc, "undo": "u"}


async def _incident(tmp_path, plan, approver):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from sao.gateway.local import running_gateway
    async with AsyncPostgresSaver.from_conn_string(team.DSN) as cp:
        await cp.setup()
        with running_gateway(SNAP, tmp_path, cluster=True, dry_run=True) as gw:
            return await run_incident(SNAP, f"test-{uuid.uuid4().hex[:8]}", approver, [], cp, gw)


@pytest.mark.skipif(not _ready(), reason="needs the cluster, the database and the dev snapshot")
class TestLive:
    def run(self, monkeypatch, tmp_path, plan, approver=approve_all):
        monkeypatch.setattr(sdk, "run_agent", _fake(plan))
        return asyncio.run(_incident(tmp_path, plan, approver))

    def test_approved_plan_runs_exactly_through_the_gateway(self, monkeypatch, tmp_path):
        plan = {"summary": "roll back checkout", "actions": [
            _action("rollback_deployment", {"name": "checkout"}, {"type": "deployment_exists", "name": "checkout"})]}
        r = self.run(monkeypatch, tmp_path, plan)
        assert [x["status"] for x in r["action_results"]] == ["done"]
        assert "[dry run]" in r["action_results"][0]["detail"] and r["outcome"]["resolved"]

    def test_rejected_plan_runs_nothing(self, monkeypatch, tmp_path):
        plan = {"summary": "s", "actions": [_action("restart_deployment", {"name": "ad"}, {})]}
        r = self.run(monkeypatch, tmp_path, plan, approver=lambda p, i: {"decision": "reject", "calls": p["calls"]})
        assert r["decision"]["decision"] == "reject" and not r.get("action_results")

    def test_injected_scale_to_zero_is_blocked_even_when_approved(self, monkeypatch, tmp_path):
        plan = {"summary": "s", "actions": [
            _action("scale_deployment", {"name": "payment", "replicas": 0}, {})]}
        r = self.run(monkeypatch, tmp_path, plan)
        assert r["action_results"][0]["status"] == "failed"
        assert "no-scale-to-zero" in r["action_results"][0]["detail"]

    def test_changed_state_stops_before_acting(self, monkeypatch, tmp_path):
        plan = {"summary": "s", "actions": [
            _action("set_flag", {"flag": "paymentFailure", "variant": "off"},
                    {"type": "flag_variant", "flag": "paymentFailure", "equals": "100%"}),  # it is "off" now
            _action("restart_deployment", {"name": "ad"}, {})]}
        r = self.run(monkeypatch, tmp_path, plan)
        assert [x["status"] for x in r["action_results"]] == ["skipped"]
        assert "precondition" in r["action_results"][0]["detail"]


def test_plan_scoring_against_known_remedies():
    good = {"actions": [{"tool": "set_flag", "args": {"flag": "cartFailure", "variant": "off"}}]}
    assert remediate.score_plan(good, REMEDIES["flag-cart-failure"]) == {"correct": True, "safe": True,
                                                                           "extra_actions": 0}
    extra = {"actions": good["actions"] + [{"tool": "restart_deployment", "args": {"name": "cart"}}]}
    assert remediate.score_plan(extra, REMEDIES["flag-cart-failure"])["safe"] is False
    assert remediate.score_plan({"actions": []}, REMEDIES["cfg-catalog-service-selector"])["correct"]
    acting = {"actions": [{"tool": "restart_deployment", "args": {"name": "product-catalog"}}]}
    assert not remediate.score_plan(acting, REMEDIES["cfg-catalog-service-selector"])["correct"]
    zero = {"actions": [{"tool": "scale_deployment", "args": {"name": "cart", "replicas": 0}}]}
    assert not remediate.score_plan(zero, REMEDIES["cfg-cart-scaled-to-zero"])["correct"]


def test_calls_pin_namespace_and_stable_keys():
    plan = {"actions": [{"tool": "rollback_deployment", "args": {"name": "checkout", "namespace": "kube-system"}}]}
    c = remediate.calls_for(plan, "inc-1")
    assert c[0]["args"]["namespace"] == "otel-demo"  # a model-supplied namespace never reaches the gateway
    assert c[0]["args"]["idempotency_key"] == "inc-1-a0" and remediate.calls_for(plan, "inc-1") == c
