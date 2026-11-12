"""Scorer checks on hand-written submissions (dev-scenario-style ground truths only)."""

from evals.score import Truth, score

NETWORK = """
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
  - {id: frontend-service-1, kind: Service, namespace: otel-demo, filter: ["frontend\\\\b"]}
aliases: [[ad-service-1, ad-pod-1]]
"""

DEPLOY_ROOT = """
groups:
  - {id: checkout-deployment, kind: Deployment, namespace: otel-demo, root_cause: true, filter: ["checkout\\\\b"]}
"""

CHAOS = "chaos-mesh/NetworkChaos/otel-demo-checkout-kafka-network-partition"


def s(truth, *names):
    return score(Truth.from_yaml(truth), [{"name": n} for n in names])


def test_exact_root_cause_scores_1():
    r = s(NETWORK, CHAOS)
    assert (r.score, r.tp, r.fp) == (1.0, 1, 0)


def test_extra_symptom_halves_precision():
    r = s(NETWORK, CHAOS, "otel-demo/Service/checkout")
    assert r.recall_ok and r.score == 0.5 and r.fp == 1


def test_symptom_only_scores_0():
    r = s(NETWORK, "otel-demo/Service/checkout")
    assert r.score == 0.0 and r.missed


def test_chaos_schedule_matches_spawned_chaos_kind():
    assert s(NETWORK, "chaos-mesh/Schedule/otel-demo-checkout-kafka-network-partition").score == 1.0


def test_wrapped_truth_and_group_without_filter():
    assert s(FLAG, "otel-demo/ConfigMap/flagd-config").score == 1.0


def test_kind_must_be_compatible():
    assert s(FLAG, "otel-demo/Deployment/flagd-config").score == 0.0


def test_alias_of_root_counts():
    assert s(POD_ROOT, "otel-demo/Service/ad").score == 1.0


def test_same_alias_group_counts_once():
    r = s(POD_ROOT, "otel-demo/Pod/ad-554b849958-lwpht", "otel-demo/Service/ad")
    assert r.score == 1.0 and (r.tp, r.fp) == (1, 0)


def test_deployment_matches_pod_group_and_back():
    assert s(POD_ROOT, "otel-demo/Deployment/ad").score == 1.0
    assert s(DEPLOY_ROOT, "otel-demo/Pod/checkout-5dccddf8bb-vh65b").score == 1.0


def test_unmatched_prediction_is_false_positive():
    r = s(POD_ROOT, "otel-demo/Deployment/ad", "otel-demo/Service/frontend", "67cbd7fe98a0776a")
    assert (r.tp, r.fp) == (1, 2) and abs(r.score - 1 / 3) < 1e-9


def test_wrong_namespace_misses_and_empty_is_zero():
    assert s(NETWORK, CHAOS.replace("chaos-mesh/", "otel-demo/")).score == 0.0
    assert s(NETWORK).score == 0.0


def test_spawned_chaos_object_matches_its_schedule_group():
    assert s(NETWORK, "chaos-mesh/NetworkChaos/otel-demo-checkout-kafka-network-partition-rjhbq").score == 1.0
    assert s(NETWORK, "chaos-mesh/NetworkChaos/otel-demo-checkout-kafka-network-partition-rjhbqx").score == 0.0
