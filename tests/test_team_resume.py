"""Durable team state: a usage stop in one parallel investigator, then a resume, must
- not re-run the commander's plan or the investigators that already finished,
- re-run only the stopped investigator, and
- produce the same answer as an uninterrupted run.
Model calls are faked; the checkpointer is the real Postgres one (skipped if the database is not up)."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter

import pytest

from sao.agents import sdk, team
from sao.llm import guard

psycopg = pytest.importorskip("psycopg")


def _db_up() -> bool:
    try:
        psycopg.connect(team.DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="checkpointer database not running (scripts/dev_db.sh up)")


def _fake(calls: Counter, fail_once: set[str]):
    async def run_agent(*, role: str, **kw) -> dict:
        calls[role] += 1
        if role in fail_once:
            fail_once.discard(role)
            raise guard.QuotaStop("usage at 80%")
        if role == "commander" and kw["schema"] is team.PLAN_SCHEMA:
            out = {"alert_summary": "checkout errors", "assignments": [
                {"signal": s, "focus": "f"} for s in ("changes", "logs", "traces")]}
        elif role.startswith("investigator"):
            out = {"summary": role, "candidates": [{"entity": "otel-demo/Deployment/checkout", "role": "possible_cause",
                                                    "evidence": "env changed"}]}
        else:
            out = {"contributing_factors": [{"name": "otel-demo/Deployment/checkout", "reasoning": "r",
                                             "evidence": "e"}]}
        await asyncio.sleep(0.05 if role != "investigator:logs" else 0.3)  # logs finishes last
        return {"role": role, "output": out, "is_error": False, "stop": "success", "turns": 1, "tool_calls": [],
                "cost_usd_api_equivalent": 0.01, "input_tokens": 10, "output_tokens": 5, "wall_s": 0.1,
                "model": "fake"}
    return run_agent


async def _run(monkeypatch, fail_once: set[str]):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    calls: Counter = Counter()
    monkeypatch.setattr(sdk, "run_agent", _fake(calls, fail_once))
    monkeypatch.setattr(guard, "last_windows", None)
    thread = f"test-{uuid.uuid4()}"
    async with AsyncPostgresSaver.from_conn_string(team.DSN) as cp:
        await cp.setup()
        stopped = False
        try:
            await team.diagnose("unused", "C", thread, cp)
        except guard.EvalStop:
            stopped = True
        result = await team.diagnose("unused", "C", thread, cp)
    return calls, stopped, result


def test_stop_in_one_investigator_resumes_without_rerunning_finished_steps(monkeypatch):
    calls, stopped, result = asyncio.run(_run(monkeypatch, {"investigator:traces"}))
    assert stopped
    assert result["resumed"]
    assert calls["commander"] == 2            # plan once + synthesize once (plan not re-run)
    assert calls["investigator:changes"] == 1
    assert calls["investigator:logs"] == 1
    assert calls["investigator:traces"] == 2  # the stopped one, and its re-run
    assert calls["verifier"] == 1
    assert [f["name"] for f in result["factors"]] == ["otel-demo/Deployment/checkout"]
    assert len(result["findings"]) == 3


def test_uninterrupted_run_matches(monkeypatch):
    calls, stopped, result = asyncio.run(_run(monkeypatch, set()))
    assert not stopped and calls["investigator:traces"] == 1
    assert [f["name"] for f in result["factors"]] == ["otel-demo/Deployment/checkout"]


def test_final_keeps_only_unexplained_causes():
    v = [
        {"entity": "otel-demo/ConfigMap/flagd-config", "verdict": "cause", "explained_by": None, "reasoning": "r",
         "evidence": "e"},
        {"entity": "otel-demo/Deployment/kafka", "verdict": "cause", "explained_by": "otel-demo/ConfigMap/flagd-config",
         "reasoning": "r", "evidence": "e"},
        {"entity": "otel-demo/Deployment/frontend (pod x)", "verdict": "symptom", "explained_by": None,
         "reasoning": "r", "evidence": "e"},
        {"entity": "otel-demo/Deployment/cart", "verdict": "cause", "explained_by": "otel-demo/Deployment/cart",
         "reasoning": "r", "evidence": "e"},
    ]
    out = [f["name"] for f in team.final_from_verdicts(v)["contributing_factors"]]
    assert out == ["otel-demo/ConfigMap/flagd-config", "otel-demo/Deployment/cart"]


def test_candidates_union_draft_and_possible_causes():
    st = {"draft": {"contributing_factors": [{"name": "otel-demo/Deployment/a"}]},
          "findings": [{"candidates": [{"entity": "otel-demo/Pod/lg-1 (load generator)", "role": "possible_cause"},
                                       {"entity": "otel-demo/Deployment/b", "role": "symptom"},
                                       {"entity": "otel-demo/Deployment/a", "role": "possible_cause"}]}]}
    assert team.candidates(st) == ["otel-demo/Deployment/a", "otel-demo/Pod/lg-1"]
