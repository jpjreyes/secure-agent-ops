"""Incident report: built from recorded facts only; config changes (not status churn) in the timeline; actions,
approval, verifier's ruled-out candidates, gateway decisions and cost all present."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sao.gateway.audit import AuditLog
from sao.report import build

SNAP = Path(__file__).resolve().parents[1] / "data/itbench-aa/snapshots/Scenario-24"


@pytest.mark.skipif(not SNAP.exists(), reason="dev snapshot not ingested")
def test_report_from_recorded_facts(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.append(event="call", agent="agent:investigator", decision="allow", policies=["read-only-tools"])
    audit.append(event="call", agent="agent:executor", decision="deny", policies=["destructive-needs-approval"],
                 screening_hits=[{"line": 1}])
    values = {
        "final": {"contributing_factors": [{"name": "otel-demo/Deployment/checkout", "reasoning": "KAFKA_ADDR changed",
                                            "evidence": "object_diff"}]},
        "verdicts": [{"entity": "otel-demo/Deployment/flagd", "verdict": "unrelated"},
                     {"entity": "otel-demo/Service/frontend", "verdict": "symptom",
                      "explained_by": "otel-demo/Deployment/checkout"}],
        "calls": [{"node": "verify", "model": "claude-sonnet-5-5", "turns": 9, "tool_calls": [{}],
                   "cost_usd_api_equivalent": 0.1}],
        "fix_plan": {"summary": "roll back", "actions": [{"tool": "rollback_deployment", "args": {"name": "checkout"},
                                                          "why": "w", "precondition": {}, "undo": "u"}]},
        "decision": {"decision": "approve", "approver": "user:oncall-jp", "how": "terminal"},
        "action_results": [{"tool": "rollback_deployment", "args": {"name": "checkout", "idempotency_key": "k"},
                            "status": "done", "detail": "rolled back"}],
        "outcome": {"resolved": True},
    }
    md = build(values, SNAP, tmp_path / "audit.jsonl", "Scenario-24")
    assert "**Root cause:** `otel-demo/Deployment/checkout`" in md
    assert "configuration change on `otel-demo/Deployment/checkout`" in md
    assert "/Node/" not in md and "ReplicaSet" not in md  # status churn is not a config change
    assert "approve by `user:oncall-jp`" in md and "**done**" in md and "idempotency_key" not in md
    assert "`otel-demo/Service/frontend`: symptom (explained by `otel-demo/Deployment/checkout`)" in md
    assert "| deny | destructive-needs-approval | 1 |" in md and "flagged 1 line" in md
    assert "**0.100**" in md and "## Postmortem draft" in md
    assert md.index("alert **") < md.index("after approval")  # actions come after the incident
    json.dumps(values)  # report input is plain data (from the checkpointed state)
