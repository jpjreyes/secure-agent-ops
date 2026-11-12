"""Gateway policies (D4): who may call what, with which arguments. Every row is one decision with its reason."""

from __future__ import annotations

import pytest

from sao.gateway.policy import Policy, ToolInfo, tool_from_annotations
from sao.identity.verify import Principal

POL = Policy()
EXEC = Principal("agent:executor", "user:oncall", ("agent:commander", "agent:executor"),
                 frozenset({"telemetry:read", "cluster:write"}), "t", "i", 0)
EXEC_UNDELEGATED = Principal("agent:executor", None, (), frozenset({"telemetry:read", "cluster:write"}), "t", "i", 0)
INV = Principal("agent:investigator", None, (), frozenset({"telemetry:read"}), "t", "i", 0)
NO_SCOPE = Principal("agent:investigator", None, (), frozenset(), "t", "i", 0)
ROLLBACK, SCALE, FLAG = (ToolInfo("cluster", n, False, True) for n in ("rollback_deployment", "scale_deployment", "set_flag"))
OVERVIEW = ToolInfo("telemetry", "overview", True, False)
OK = {"namespace": "otel-demo", "name": "checkout"}


@pytest.mark.parametrize("who,tool,args,approved,allowed,reason", [
    (INV, OVERVIEW, {}, False, True, "read-only-tools"),
    (NO_SCOPE, OVERVIEW, {}, False, False, "default-deny"),
    (EXEC, ROLLBACK, OK, True, True, "executor-cluster-actions"),
    (EXEC, ROLLBACK, OK, False, False, "destructive-needs-approval"),
    (EXEC_UNDELEGATED, ROLLBACK, OK, True, False, "default-deny"),        # not acting for a person
    (INV, ROLLBACK, OK, True, False, "default-deny"),                     # investigators never write
    (EXEC, ROLLBACK, {"namespace": "kube-system", "name": "x"}, True, False, "default-deny"),
    (EXEC, ROLLBACK, {"name": "checkout"}, True, False, "default-deny"),  # namespace must be explicit
    (EXEC, ROLLBACK, {"namespace": "otel-demo", "name": "prometheus"}, True, False, "protect-platform"),
    (EXEC, SCALE, {"namespace": "otel-demo", "name": "cart", "replicas": 0}, True, False, "no-scale-to-zero"),
    (EXEC, SCALE, {"namespace": "otel-demo", "name": "cart", "replicas": 2}, True, True, "executor-cluster-actions"),
    (EXEC, SCALE, {"namespace": "otel-demo", "name": "cart", "replicas": True}, True, True,
     "executor-cluster-actions"),  # a bool is not a replica count: dropped, so the rule can't be tricked into crashing
])
def test_decisions(who, tool, args, approved, allowed, reason):
    d = POL.authorize(who, tool, args, approved)
    assert d.allowed is allowed and reason in d.policies, d


def test_tool_list_is_filtered_per_agent():
    tools = [ROLLBACK, SCALE, FLAG, OVERVIEW]
    assert [t.name for t in POL.visible_tools(INV, tools)] == ["overview"]
    assert [t.name for t in POL.visible_tools(EXEC_UNDELEGATED, tools)] == ["overview"]
    assert [t.name for t in POL.visible_tools(EXEC, tools)] == ["rollback_deployment", "scale_deployment", "set_flag", "overview"]


def test_missing_annotations_are_treated_as_destructive():
    assert tool_from_annotations("cluster", "x", None).destructive
    assert not tool_from_annotations("telemetry", "y", {"readOnlyHint": True}).destructive


def test_policies_must_validate_against_the_schema():
    with pytest.raises(ValueError, match="validate"):
        Policy('permit (principal, action, resource) when { context.args.typo == "x" };')


def test_every_tool_named_in_a_policy_exists():
    """A policy naming a tool that doesn't exist silently never applies (found once: "cluster/scale")."""
    import asyncio
    import re

    from sao.mcp import cluster_server, telemetry_server
    real = {f"cluster/{t.name}" for t in asyncio.run(cluster_server.server.list_tools())}
    real |= {f"telemetry/{t.name}" for t in asyncio.run(telemetry_server.server.list_tools())}
    named = set(re.findall(r'Tool::"([^"]+)"', POL.text))
    assert named and named <= real, named - real
