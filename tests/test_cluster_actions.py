"""Executor's write tools: idempotency (a resumed episode never acts twice), retryable failures, dry-run flag, and
every tool annotated destructive (so the gateway always requires approval). kubectl is faked."""

from __future__ import annotations

import json

import pytest

from sao import kube
from sao.mcp import cluster_server as cs


@pytest.fixture
def fake(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cs, "LEDGER", tmp_path / "ledger.jsonl")

    def kubectl(*args, **_):
        calls.append(args)
        if "missing" in args:
            raise kube.KubeError("NotFound")
        return "ok\n"
    monkeypatch.setattr(kube, "kubectl", kubectl)
    return calls


def test_same_key_acts_once(fake):
    first = cs.rollback_deployment("otel-demo", "checkout", "key-0000001")
    again = cs.rollback_deployment("otel-demo", "checkout", "key-0000001")
    assert first == "ok" and again.startswith("already done")
    assert len(fake) == 1


def test_key_reused_for_another_action_is_refused(fake):
    cs.rollback_deployment("otel-demo", "checkout", "key-0000002")
    assert cs.scale_deployment("otel-demo", "checkout", 3, "key-0000002").startswith("refused")
    assert len(fake) == 1


def test_failure_does_not_consume_the_key(fake):
    assert cs.delete_networkpolicy("otel-demo", "missing", "key-0000003").startswith("failed")
    assert cs.delete_networkpolicy("otel-demo", "missing", "key-0000003").startswith("failed")
    assert len(fake) == 2
    rows = [json.loads(x) for x in cs.LEDGER.read_text().splitlines()]
    assert [r["ok"] for r in rows] == [False, False]


def test_short_key_refused_and_dry_run_flag(fake, monkeypatch):
    assert cs.restart_deployment("otel-demo", "ad", "short").startswith("refused")
    monkeypatch.setenv("SAO_DRY_RUN", "1")
    assert cs.scale_deployment("otel-demo", "cart", 1, "key-0000004").startswith("[dry run]")
    assert fake[-1][-1] == "--dry-run=server"


def test_every_tool_is_annotated_destructive():
    import asyncio
    tools = asyncio.run(cs.server.list_tools())
    assert {t.name for t in tools} == {"rollback_deployment", "restart_deployment", "scale_deployment", "set_flag",
                                       "delete_networkpolicy"}
    for t in tools:
        assert t.annotations.destructive_hint is True and t.annotations.read_only_hint is False
        assert "namespace" in t.input_schema["properties"] and "idempotency_key" in t.input_schema["required"]
