"""Live faults and the recorder, without a cluster:
- every fault's ground truth parses and scores like an ITBench-AA scenario (root found = 1.0, root + symptom = 0.5,
  symptom only = 0.0);
- what the recorder writes loads in the snapshot tools (same layout as the benchmark), including span status and
  kind spelled as in ITBench-AA."""

from __future__ import annotations

import json
import time

import pytest

from evals.score import Truth, score
from sao.faults.catalog import FAULTS
from sao.telemetry.live import Recorder, _iso
from sao.telemetry.snapshot import Snapshot


@pytest.mark.parametrize("fid", sorted(FAULTS))
def test_fault_truth_scores_like_itbench(fid):
    f = FAULTS[fid]
    t = Truth.from_yaml(f.truth_yaml())
    kind, name = f.root
    root = f"otel-demo/{kind}/{name}"
    assert score(t, [root]).score == 1.0
    symptom = next((s for s in f.symptoms if s != name), None)
    if symptom:
        assert score(t, [root, f"otel-demo/Service/{symptom}"]).score == 0.5
        assert score(t, [f"otel-demo/Deployment/{symptom}"]).score == 0.0


def test_recorder_output_loads_in_snapshot_tools(tmp_path):
    r = Recorder(tmp_path)
    now = time.time()
    dep = {"kind": "Deployment", "metadata": {"name": "checkout"},
           "spec": {"template": {"spec": {"containers": [{"name": "checkout", "env": [
               {"name": "PAYMENT_ADDR", "value": "payment:8080"}]}]}}}}
    bad = json.loads(json.dumps(dep))
    bad["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "payment:8081"
    for i, o in enumerate((dep, bad)):
        r.rows["objects"].append({"timestamp": _iso(now - 60 + i * 30), "object_kind": "Deployment",
                                  "object_name": "checkout", "namespace": "otel-demo", "body": json.dumps(o)})
    r.rows["events"].append({"timestamp": _iso(now - 20), "event_kind": "Normal", "reason": "ScalingReplicaSet",
                             "object_kind": "Deployment", "object_name": "checkout", "object_namespace": "otel-demo",
                             "message": "Scaled up", "event_count": 1})
    r.rows["traces"].append({"timestamp": _iso(now - 10), "trace_id": "t1", "span_id": "s1", "parent_span_id": "",
                             "service_name": "checkout", "span_name": "PlaceOrder", "span_kind": "Server",
                             "duration": "5000000", "status_code": "Error", "status_message": "dial tcp: i/o timeout"})
    r.rows["logs"].append({"timestamp": _iso(now - 10), "service_name": "checkout", "severity_text": "ERROR",
                           "body": "failed to charge card", "k8s_pod_name": "checkout-abc",
                           "k8s_namespace": "otel-demo"})
    for i in range(6):
        r.rows["metrics"].append({"metric_name": "traces_span_metrics_calls_total",
                                  "timestamp": _iso(now - 60 + i * 10),
                                  "value": 0.0 if i < 3 else 1.0, "pod_name": "", "namespace": "otel-demo",
                                  "service_name": "checkout", "metric_type": "error_rate"})
    (tmp_path / "alerts" / "alerts_at_x.json").write_text(json.dumps({"data": {"alerts": [
        {"labels": {"alertname": "RequestErrorRate", "service_name": "checkout", "namespace": "otel-demo",
                    "severity": "warning"}, "state": "firing", "activeAt": _iso(now - 5),
         "annotations": {"description": "Request error rate in service checkout is above 0"}}]}}))
    r.flush()
    s = Snapshot(tmp_path)
    assert "RequestErrorRate" in s.alerts()
    assert "env[PAYMENT_ADDR].value | payment:8080 | payment:8081" in s.object_diff("Deployment", "checkout")
    assert "dial tcp: i/o timeout" in s.trace_errors()
    assert "checkout" in s.trace_summary()
    assert "failed to charge card" in s.log_errors()
    assert "error_rate" in s.metric_anomalies()
    assert "ScalingReplicaSet" in s.events()
