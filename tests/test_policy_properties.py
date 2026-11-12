"""Safety properties of the gateway's Cedar policies, checked exhaustively over their decision space (D4, D15).

The policies only look at: the principal (compared with Agent::"agent:executor"), the tool's attributes (server,
read_only, destructive) and its id (compared with the scale tool), the scopes (contains two constants), delegation,
the chain (contains the commander), approval, and the arguments namespace, name, replicas (compared with constants).
Cedar can't distinguish values except through those comparisons, so every combination of {each constant, one other
value, absent} per attribute covers every behavior the policies can have. All combinations are evaluated by Cedar
itself (no reimplementation), and each property must hold on every allowed request.

This is an exhaustive check over a finite abstraction, not an SMT proof; the SMT proof with Cedar's symbolic
compiler (cedar-policy-symcc + cvc5) is the next step and would replace it in CI.
"""

from __future__ import annotations

import itertools

import cedarpy
import pytest

from sao.gateway.policy import Policy

POL = Policy()
PROTECTED = ["prometheus", "jaeger", "opensearch", "otel-collector", "otel-collector-agent", "flagd"]
PRINCIPALS = ["agent:executor", "agent:investigator", "agent:commander"]
TOOLS = [  # (uid, server) - the scale tool's id is named in a policy, the others are interchangeable
    ("cluster/scale_deployment", "cluster"), ("cluster/rollback_deployment", "cluster"),
    ("telemetry/overview", "telemetry"), ("other/x", "other")]
SCOPES = [[], ["telemetry:read"], ["cluster:write"], ["telemetry:read", "cluster:write"]]
CHAINS = [[], ["agent:commander"], ["agent:commander", "agent:executor"], ["agent:investigator"]]
NAMESPACES = [None, "otel-demo", "kube-system"]
# equivalence classes: every protected name behaves the same under `contains` (two kept as a cross-check), and
# replicas only matter through `< 1`
NAMES = [None, "checkout", "prometheus", "flagd"]
REPLICAS = [None, 0, 1]


def requests():
    for (uid, server), ro, de in itertools.product(TOOLS, [False, True], [False, True]):
        ents = [{"uid": {"type": "Tool", "id": uid}, "parents": [],
                 "attrs": {"server": server, "read_only": ro, "destructive": de}}]
        batch = []
        for who, scopes, chain, delegated, approved, ns, name, reps, obo in itertools.product(
                PRINCIPALS, SCOPES, CHAINS, [False, True], [False, True], NAMESPACES, NAMES, REPLICAS,
                [False, True]):
            args = {k: v for k, v in (("namespace", ns), ("name", name), ("replicas", reps)) if v is not None}
            ctx = {"scopes": scopes, "chain": chain, "delegated": delegated, "approved": approved, "args": args}
            if obo:
                ctx["on_behalf_of"] = "user:oncall"
            batch.append({"principal": {"type": "Agent", "id": who}, "action": {"type": "Action", "id": "call"},
                          "resource": {"type": "Tool", "id": uid}, "context": ctx})
        yield (uid, server, ro, de), ents, batch


@pytest.fixture(scope="module")
def allowed():
    """Every request the policies allow (each with its tool attributes), over the whole abstract space."""
    out, total = [], 0
    for tool, ents, batch in requests():
        results = cedarpy.is_authorized_batch(batch, POL.policy_set, ents, POL.schema)
        total += len(batch)
        for req, r in zip(batch, results, strict=True):
            assert not r.diagnostics.errors, r.diagnostics.errors
            if r.decision == cedarpy.Decision.Allow:
                out.append((tool, req))
    print(f"\n{total} requests evaluated, {len(out)} allowed")
    assert total > 100_000, total
    return out


def test_destructive_is_never_allowed_without_approval(allowed):
    assert all(req["context"]["approved"] for (_, _, _, de), req in allowed if de)


def test_only_the_delegated_executor_ever_gets_a_non_read_only_tool(allowed):
    for (_, _, ro, _), req in allowed:
        if ro:
            continue
        c = req["context"]
        assert req["principal"]["id"] == "agent:executor"
        assert c["delegated"] and "agent:commander" in c["chain"] and "cluster:write" in c["scopes"]
        assert c["args"].get("namespace") == "otel-demo"


def test_protected_platform_is_never_touched_on_the_cluster(allowed):
    assert not [r for (_, server, _, _), r in allowed if server == "cluster"
                and r["context"]["args"].get("name") in PROTECTED]


def test_scale_to_zero_or_below_is_never_allowed(allowed):
    assert not [r for (uid, *_), r in allowed if uid == "cluster/scale_deployment"
                and r["context"]["args"].get("replicas", 1) < 1]


def test_read_only_needs_the_read_scope_outside_the_cluster_server(allowed):
    """Found by this check: rule 2 (executor cluster actions) also covers read-only tools on the cluster server, so
    there `cluster:write` without `telemetry:read` is enough. Intended: writing the cluster implies reading it. Every
    other read-only tool needs telemetry:read."""
    assert all("telemetry:read" in r["context"]["scopes"] for (_, server, ro, _), r in allowed
               if ro and server != "cluster")


def test_a_tool_claiming_read_only_but_destructive_still_needs_approval(allowed):
    # a malicious or buggy upstream annotating a tool both ways must not get a free pass
    assert all(r["context"]["approved"] for (_, _, ro, de), r in allowed if ro and de)


def test_something_is_allowed(allowed):
    """Guard against a vacuous pass (properties trivially true if nothing is ever allowed)."""
    kinds = {(ro, de) for (_, _, ro, de), _ in allowed}
    assert (True, False) in kinds and (False, True) in kinds
