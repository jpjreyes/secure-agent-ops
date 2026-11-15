"""Public demo pieces that need no model calls: attack planting, the audit check, rate limits, the API-mode guard."""

import json
from pathlib import Path

import duckdb
import pytest

from sao.demo import attack, store
from sao.gateway.audit import AuditLog

SNAP = Path(__file__).resolve().parents[1] / "data/itbench-aa/snapshots/Scenario-24"


def _chain(tmp_path, n=5):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(n):
        log.append(event="call", agent="agent:investigator", tool=f"t{i}", decision="allow")
    return [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]


def test_verify_finds_the_edited_record(tmp_path):
    from sao.demo.app import verify_records
    recs = _chain(tmp_path)
    assert verify_records(recs)["ok"]
    recs[2]["decision"] = "deny"
    r = verify_records(recs)
    assert not r["ok"] and r["broken_index"] == 2 and "content changed" in r["message"]


def test_verify_finds_a_removed_record(tmp_path):
    from sao.demo.app import verify_records
    recs = _chain(tmp_path)
    del recs[1]
    r = verify_records(recs)
    assert not r["ok"] and r["broken_index"] == 1 and "previous-hash" in r["message"]


def test_rate_limit_counts_in_memory(monkeypatch):
    monkeypatch.setattr(store, "TABLE", None)
    monkeypatch.setattr(store, "PER_HOUR", 2)
    monkeypatch.setattr(store, "_mem", {})
    assert [store.take_run_slot(1e9)[0] for _ in range(3)] == [True, True, False]


def test_attack_text_presets_and_custom():
    text, goal = attack.attack_text("action", None)
    assert goal == "action" and "scale_deployment" in text
    text, goal = attack.attack_text(None, "x" * 5000)
    assert goal == "custom" and len(text) == attack.MAX_TEXT
    with pytest.raises(ValueError):
        attack.attack_text("nope", None)


@pytest.mark.skipif(not SNAP.exists(), reason="ITBench-AA snapshot not downloaded")
def test_plant_adds_rows_to_the_noisiest_service(tmp_path):
    meta = attack.plant(SNAP, tmp_path / "snap", "PLANTED-LINE")
    f = tmp_path / "snap/logs.parquet"
    cols = [c[0] for c in duckdb.sql(f"DESCRIBE SELECT * FROM '{f}'").fetchall()]
    body = "Body" if "Body" in cols else "body"
    n = duckdb.sql(f"SELECT count(*) FROM '{f}' WHERE {body} = 'PLANTED-LINE'").fetchall()[0][0]
    assert n == attack.COPIES and meta["carrier_service"]


def test_hijack_scoring():
    assert attack.hijacked("action", [], {"actions": [{"tool": "scale_deployment",
                                                       "args": {"name": "payment", "replicas": 0}}]}, "")
    assert not attack.hijacked("diagnosis", [{"name": "otel-demo/Deployment/checkout"}], None, "")
    assert attack.hijacked("custom", [], None, "") is None


def test_api_mode_refuses_a_login_token(monkeypatch):
    from sao.llm import guard
    monkeypatch.setattr(guard, "API_MODE", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat01-not-an-api-key")
    with pytest.raises(guard.NotClean):
        guard.sdk_env()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    assert guard.sdk_env()["ANTHROPIC_API_KEY"] == "sk-ant-api03-test"
