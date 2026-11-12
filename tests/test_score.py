"""Scorer checks on hand-written submissions (dev-scenario ground truths only)."""

from evals.score import Truth, score

NETWORK = """
fault: [{category: Network, fault_mechanism: network_partition,
         entity: {kind: NetworkChaos, name: otel-demo-checkout-kafka-network-partition}}]
groups:
  - {id: otel-demo-checkout-kafka-network-partition, kind: NetworkChaos, namespace: chaos-mesh, root_cause: true,
     filter: [otel-demo-checkout-kafka-network-partition]}
  - {id: checkout-service-1, kind: Service, namespace: otel-demo, filter: ["checkout\\\\b"]}
  - {id: checkout-pod-1, kind: Pod, namespace: otel-demo, filter: ["checkout-.*"]}
aliases: [[checkout-service-1, checkout-pod-1]]
"""

FLAG = """
apiVersion: itbench.io/v1
kind: GroundTruth
spec:
  fault: [{category: Configuration Setting, entity: {kind: ConfigMap, name: flagd-config}}]
  groups:
    - {id: flagd-config-1, kind: ConfigMap, namespace: otel-demo, root_cause: true}
    - {id: ad-service-1, kind: Service, namespace: otel-demo, filter: ["ad\\\\b"]}
    - {id: ad-pod-1, kind: Pod, namespace: otel-demo, filter: ["ad-.*"]}
  aliases: [[ad-service-1, ad-pod-1]]
"""

POD_ROOT = """
groups:
  - {id: ad-pod-1, kind: Pod, namespace: otel-demo, root_cause: true, filter: ["ad-.*"]}
  - {id: ad-service-1, kind: Service, namespace: otel-demo, filter: ["ad\\\\b"]}
aliases: [[ad-service-1, ad-pod-1]]
"""

CHAOS = {"kind": "NetworkChaos", "name": "otel-demo-checkout-kafka-network-partition", "namespace": "chaos-mesh"}


def test_exact_root_cause_scores_1():
    s = score(Truth.from_yaml(NETWORK), [CHAOS])
    assert (s.score, s.tp, s.fp) == (1.0, 1, 0)


def test_extra_symptom_halves_precision():
    s = score(Truth.from_yaml(NETWORK), [CHAOS, {"kind": "Service", "name": "checkout", "namespace": "otel-demo"}])
    assert s.recall_ok and s.score == 0.5 and s.fp == 1


def test_symptom_only_scores_0():
    s = score(Truth.from_yaml(NETWORK), [{"kind": "Service", "name": "checkout"}])
    assert s.score == 0.0 and not s.recall_ok and s.missed == ["otel-demo-checkout-kafka-network-partition"]


def test_wrapped_truth_and_group_without_filter():
    s = score(Truth.from_yaml(FLAG), [{"kind": "ConfigMap", "name": "flagd-config", "namespace": "otel-demo"}])
    assert s.score == 1.0


def test_kind_must_match():
    s = score(Truth.from_yaml(FLAG), [{"kind": "Deployment", "name": "flagd-config"}])
    assert s.score == 0.0


def test_alias_of_root_counts():
    s = score(Truth.from_yaml(POD_ROOT), [{"kind": "Service", "name": "ad", "namespace": "otel-demo"}])
    assert s.score == 1.0


def test_pod_and_its_service_both_count_once_each():
    t = Truth.from_yaml(POD_ROOT)
    s = score(t, [{"kind": "Pod", "name": "ad-554b849958-lwpht"}, {"kind": "Service", "name": "ad"}])
    assert s.score == 1.0 and s.tp == 2


def test_wrong_namespace_misses():
    s = score(Truth.from_yaml(NETWORK), [{**CHAOS, "namespace": "otel-demo"}])
    assert s.score == 0.0


def test_duplicates_counted_once_and_empty_is_zero():
    assert score(Truth.from_yaml(NETWORK), [CHAOS, dict(CHAOS)]).score == 1.0
    assert score(Truth.from_yaml(NETWORK), []).score == 0.0
