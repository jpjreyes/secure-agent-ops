"""Faults for the live cluster: the OpenTelemetry Demo's feature flags plus our own configuration faults.

Every fault knows how to inject itself, which objects it touches (saved before injection so revert restores them
exactly, even after a crash), which services should show symptoms, and its ground truth in ITBench-AA's format, so
live incidents are scored by the same scorer as the benchmark.

Families:
- flagd: flip one flag in the flagd-config ConfigMap and restart flagd (it copies the ConfigMap at start). Root cause
  = the ConfigMap, as in ITBench-AA's flag scenarios.
- config: a bad change rolled out by "someone": wrong dependency address in a Deployment's env, a Service selector
  typo, a replica count of zero, a NetworkPolicy that blocks traffic, a memory limit too low. Root cause = the object
  that was changed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sao import kube

NS = kube.NAMESPACE


@dataclass
class Fault:
    id: str
    family: str
    description: str          # what happened, for the debrief (never shown to agents)
    touches: list[tuple[str, str]]   # (kind, name) saved before injection and restored on revert
    creates: list[tuple[str, str]] = field(default_factory=list)  # (kind, name) deleted on revert
    symptoms: list[str] = field(default_factory=list)            # services expected to alert
    root: tuple[str, str] = ("", "")                             # (kind, name) of the root cause
    restart: list[str] = field(default_factory=list)             # deployments restarted after inject and revert
    restart_after_revert: list[str] = field(default_factory=list)  # app services restarted after revert only
    settle_s: int = 150                                          # time for symptoms to show

    def inject(self) -> None:
        raise NotImplementedError

    def truth_yaml(self) -> str:
        kind, name = self.root
        groups = [{"id": f"{name}-1", "kind": kind, "namespace": NS,
                   "filter": [f"{name}\\b" if kind != "Pod" else f"{name}-.*"], "root_cause": True}]
        for s in self.symptoms:  # symptoms are listed (non-root) so predicting them counts as a false positive
            if s == name:
                continue
            groups.append({"id": f"{s}-service-1", "kind": "Service", "namespace": NS, "filter": [f"{s}\\b"]})
            groups.append({"id": f"{s}-pod-1", "kind": "Pod", "namespace": NS, "filter": [f"{s}-.*"]})
        import yaml
        return yaml.safe_dump({"fault": [{"entity": {"name": name, "kind": kind}, "category": self.family,
                                          "condition": self.description}], "groups": groups,
                               "aliases": [[f"{s}-service-1", f"{s}-pod-1"] for s in self.symptoms if s != name]},
                              sort_keys=False)


# ---------- flagd ----------
FLAG_CM = "flagd-config"
FLAG_KEY = "demo.flagd.json"


@dataclass
class FlagFault(Fault):
    flag: str = ""
    variant: str = "on"

    def inject(self) -> None:
        cm = kube.get("configmap", FLAG_CM)
        doc = json.loads(cm["data"][FLAG_KEY])
        if self.variant not in doc["flags"][self.flag]["variants"]:
            raise ValueError(f"{self.flag} has no variant {self.variant}")
        doc["flags"][self.flag]["defaultVariant"] = self.variant
        kube.patch("configmap", FLAG_CM, {"data": {FLAG_KEY: json.dumps(doc, indent=2)}}, "merge")


def _flag(fid: str, flag: str, variant: str, symptoms: list[str], desc: str, settle_s: int = 150) -> FlagFault:
    return FlagFault(id=fid, family="flagd", description=desc, touches=[("configmap", FLAG_CM)], symptoms=symptoms,
                     root=("ConfigMap", FLAG_CM), restart=["flagd"], settle_s=settle_s, flag=flag, variant=variant)


# ---------- configuration faults ----------
@dataclass
class EnvFault(Fault):
    deployment: str = ""
    env: dict[str, str] = field(default_factory=dict)

    def inject(self) -> None:
        dep = kube.get("deployment", self.deployment)
        c = dep["spec"]["template"]["spec"]["containers"][0]
        kube.patch("deployment", self.deployment, {"spec": {"template": {"spec": {"containers": [
            {"name": c["name"], "env": [{"name": k, "value": v} for k, v in self.env.items()]}]}}}})


@dataclass
class PatchFault(Fault):
    kind: str = ""
    name: str = ""
    body: dict = field(default_factory=dict)
    ptype: str = "merge"

    def inject(self) -> None:
        kube.patch(self.kind, self.name, self.body, self.ptype)


@dataclass
class MemoryLimitFault(Fault):
    deployment: str = ""
    limit: str = "64Mi"

    def inject(self) -> None:
        dep = kube.get("deployment", self.deployment)
        c = dep["spec"]["template"]["spec"]["containers"][0]
        kube.patch("deployment", self.deployment, {"spec": {"template": {"spec": {"containers": [
            {"name": c["name"], "resources": {"requests": {"memory": self.limit}, "limits": {"memory": self.limit}}}
        ]}}}})


@dataclass
class CreateFault(Fault):
    obj: dict = field(default_factory=dict)

    def inject(self) -> None:
        kube.apply(self.obj)


def _deny_ingress(name: str, app: str) -> dict:
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": NS},
            "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": app}},
                     "policyTypes": ["Ingress"], "ingress": []}}


FAULTS: dict[str, Fault] = {f.id: f for f in [
    # flagd: the demo's own failure scenarios
    _flag("flag-payment-failure", "paymentFailure", "100%", ["payment", "checkout", "frontend"],
          "paymentFailure=100%: the payment service fails every charge"),
    _flag("flag-payment-unreachable", "paymentUnreachable", "on", ["checkout", "frontend"],
          "paymentUnreachable: checkout calls a bad payment address"),
    _flag("flag-cart-failure", "cartFailure", "100%", ["cart", "frontend"],
          "cartFailure=100%: cart operations fail"),
    _flag("flag-product-catalog-failure", "productCatalogFailure", "on", ["product-catalog", "frontend", "checkout"],
          "productCatalogFailure: GetProduct fails for one product"),
    _flag("flag-ad-failure", "adFailure", "on", ["ad", "frontend"], "adFailure: GetAds fails"),
    _flag("flag-ad-high-cpu", "adHighCpu", "on", ["ad", "frontend"], "adHighCpu: the ad service burns CPU"),
    _flag("flag-kafka-queue-problems", "kafkaQueueProblems", "on", ["checkout", "accounting", "fraud-detection"],
          "kafkaQueueProblems: Kafka producer overload, consumer lag"),
    _flag("flag-email-memory-leak", "emailMemoryLeak", "1000x", ["email", "checkout"],
          "emailMemoryLeak=1000x: the email service leaks memory until OOM", settle_s=240),
    _flag("flag-recommendation-cache-failure", "recommendationCacheFailure", "on", ["recommendation", "frontend"],
          "recommendationCacheFailure: cache leak in recommendation"),
    _flag("flag-image-slow-load", "imageSlowLoad", "10sec", ["image-provider", "frontend-proxy"],
          "imageSlowLoad=10sec: product images load slowly"),
    _flag("flag-lg-flood-homepage", "loadGeneratorFloodHomepage", "on", ["frontend", "frontend-proxy"],
          "loadGeneratorFloodHomepage: the load generator floods the homepage"),
    _flag("flag-catalog-lock-contention", "productCatalogLockContention", "on", ["product-catalog", "frontend"],
          "productCatalogLockContention: lock contention in product-catalog"),

    # our own: bad configuration rolled out
    EnvFault(id="cfg-checkout-payment-addr", family="config",
             description="checkout rolled out with PAYMENT_ADDR pointing at a port nothing listens on",
             touches=[("deployment", "checkout")], symptoms=["checkout", "frontend"],
             root=("Deployment", "checkout"), deployment="checkout", env={"PAYMENT_ADDR": "payment:8081"}),
    EnvFault(id="cfg-shipping-quote-addr", family="config",
             description="shipping rolled out with a typo in QUOTE_ADDR (quote-svc instead of quote)",
             touches=[("deployment", "shipping")], symptoms=["shipping", "checkout", "frontend"],
             root=("Deployment", "shipping"), deployment="shipping", env={"QUOTE_ADDR": "http://quote-svc:8080"}),
    EnvFault(id="cfg-recommendation-catalog-addr", family="config",
             description="recommendation rolled out with PRODUCT_CATALOG_ADDR pointing at the cart service",
             touches=[("deployment", "recommendation")], symptoms=["recommendation", "frontend"],
             root=("Deployment", "recommendation"), deployment="recommendation",
             env={"PRODUCT_CATALOG_ADDR": "cart:8080"}),
    PatchFault(id="cfg-catalog-service-selector", family="config",
               description="product-catalog Service selector changed to a label no pod has (no endpoints)",
               touches=[("service", "product-catalog")],
               symptoms=["frontend", "checkout", "recommendation"], root=("Service", "product-catalog"),
               kind="service", name="product-catalog",
               body={"spec": {"selector": {"opentelemetry.io/name": "product-catalog-v2"}}}),
    PatchFault(id="cfg-cart-scaled-to-zero", family="config", description="cart Deployment scaled to 0 replicas",
               touches=[("deployment", "cart")], symptoms=["frontend", "checkout"], root=("Deployment", "cart"),
               kind="deployment", name="cart", body={"spec": {"replicas": 0}}),
    CreateFault(id="cfg-currency-netpol", family="config",
                description="a NetworkPolicy denying all ingress to the currency pods",
                touches=[], creates=[("networkpolicy", "currency-lockdown")], symptoms=["currency", "frontend",
                                                                                        "checkout"],
                root=("NetworkPolicy", "currency-lockdown"), obj=_deny_ingress("currency-lockdown", "currency")),
    MemoryLimitFault(id="cfg-recommendation-oom", family="config",
                     description="recommendation rolled out with a 48Mi memory limit (OOMKilled under load)",
                     touches=[("deployment", "recommendation")], symptoms=["recommendation", "frontend"],
                     root=("Deployment", "recommendation"), deployment="recommendation", limit="48Mi", settle_s=210),
]}

# Configuration faults act only after a rollout and on real traffic (checkouts are a few per minute), so their
# alerts need longer than a flag flip; the episode still stops waiting as soon as alerts fire.
for _f in FAULTS.values():
    if _f.family == "config" and _f.settle_s == 150:
        _f.settle_s = 300

# Every user request enters through frontend-proxy → frontend, so a fault users feel at the frontend is also expected
# to show at the proxy (ITBench-AA's ground truth lists frontend-proxy as a symptom the same way).
for _f in FAULTS.values():
    if "frontend" in _f.symptoms and "frontend-proxy" not in _f.symptoms:
        _f.symptoms.append("frontend-proxy")

# Known remedies (D12, evals/PLAN.md "Remediation"): the actions that fix each fault. A plan is correct if it contains
# one of them (arguments matching on the keys given); [] means no available tool fixes it, so the right plan is to
# escalate without acting.
def _flag_off(f: Fault) -> list[dict]:
    return [{"tool": "set_flag", "args": {"flag": f.flag, "variant": "off"}}]


REMEDIES: dict[str, list[dict]] = {fid: _flag_off(f) for fid, f in FAULTS.items() if isinstance(f, FlagFault)}
REMEDIES |= {
    "cfg-checkout-payment-addr": [{"tool": "rollback_deployment", "args": {"name": "checkout"}}],
    "cfg-shipping-quote-addr": [{"tool": "rollback_deployment", "args": {"name": "shipping"}}],
    "cfg-recommendation-catalog-addr": [{"tool": "rollback_deployment", "args": {"name": "recommendation"}}],
    "cfg-recommendation-oom": [{"tool": "rollback_deployment", "args": {"name": "recommendation"}}],
    "cfg-cart-scaled-to-zero": [{"tool": "scale_deployment", "args": {"name": "cart"}, "min_replicas": 1}],
    "cfg-currency-netpol": [{"tool": "delete_networkpolicy", "args": {"name": "currency-lockdown"}}],
    "cfg-catalog-service-selector": [],  # no tool edits a Service: escalate to a person
}
assert set(REMEDIES) == set(FAULTS)

# A reverted flag can leave state behind inside the service it acted on (found 2026-11-12: product-catalog kept its
# lock-contention loop after productCatalogLockContention was set back to off, contaminating the next 9 episodes).
# Revert therefore also restarts that service, so every episode starts from a clean application.
for _f in FAULTS.values():
    if isinstance(_f, FlagFault) and _f.symptoms:
        _f.restart_after_revert = [_f.symptoms[0]]
