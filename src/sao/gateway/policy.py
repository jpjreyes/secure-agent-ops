"""The gateway's decisions: Cedar over (agent, tool, the call's actual arguments, delegation, approval).

- authorize(): one tool call → Allow/Deny plus the policy ids that decided it (for the audit record).
- visible_tools(): the tool list an agent gets; tools it could never call are left out (unknown tools are invisible),
  decided by asking Cedar with the most permissive context the agent could legitimately have (approval given,
  in-scope namespace).
Tool attributes (read_only, destructive) come from the upstream server's MCP tool annotations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cedarpy

from sao.identity.verify import Principal

ROOT = Path(__file__).resolve().parents[3]
POLICIES = ROOT / "policies" / "gateway.cedar"
SCHEMA = ROOT / "policies" / "schema.cedarschema.json"
# Arguments Cedar sees (declared in the schema, so policies on them are type-checked). Others never reach a policy;
# a tool whose safety depends on an undeclared argument must have it added to the schema first.
ARG_TYPES = {"namespace": str, "name": str, "kind": str, "replicas": int, "flag": str, "variant": str}


@dataclass(frozen=True)
class ToolInfo:
    server: str          # upstream name, e.g. "telemetry", "cluster"
    name: str            # tool name on that server
    read_only: bool
    destructive: bool

    @property
    def uid(self) -> str:
        return f"{self.server}/{self.name}"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    policies: tuple[str, ...]   # ids of the policies that determined the decision
    errors: tuple[str, ...] = ()


def tool_from_annotations(server: str, name: str, annotations: dict | None) -> ToolInfo:
    """MCP annotations → tool attributes. Missing annotations are treated as the unsafe default (not read-only,
    destructive), per the MCP spec's defaults for destructiveHint."""
    a = annotations or {}
    read_only = bool(a.get("readOnlyHint", False))
    destructive = False if read_only else bool(a.get("destructiveHint", True))
    return ToolInfo(server, name, read_only, destructive)


class Policy:
    def __init__(self, policies: str | None = None, schema: dict | None = None):
        self.text = policies if policies is not None else POLICIES.read_text()
        self.schema = schema or json.loads(SCHEMA.read_text())
        v = cedarpy.validate_policies(self.text, self.schema)
        if not v.validation_passed:
            raise ValueError(f"policies do not validate against the schema: {[e.error for e in v.errors]}")
        self.policy_set = cedarpy.PolicySet.from_str(self.text)
        self.ids = self._annotation_ids()

    def _annotation_ids(self) -> dict[str, str]:
        """Cedar names policies policy0, policy1…; map them to their @id annotations for readable audit records."""
        out = {}
        for i, block in enumerate(json.loads(cedarpy.policies_to_json_str(self.text))["staticPolicies"].items()):
            pid, body = block
            out[pid] = (body.get("annotations") or {}).get("id", pid)
        return out

    def _entities(self, tool: ToolInfo) -> list[dict]:
        return [{"uid": {"type": "Tool", "id": tool.uid}, "parents": [],
                 "attrs": {"server": tool.server, "read_only": tool.read_only, "destructive": tool.destructive}}]

    @staticmethod
    def _context(p: Principal, args: dict, approved: bool) -> dict:
        ctx = {"scopes": sorted(p.scopes), "chain": list(p.chain), "delegated": bool(p.chain), "approved": approved,
               "args": {k: v for k, v in args.items() if k in ARG_TYPES and isinstance(v, ARG_TYPES[k])
                        and not (ARG_TYPES[k] is int and isinstance(v, bool))}}
        if p.on_behalf_of:
            ctx["on_behalf_of"] = p.on_behalf_of
        return ctx

    def authorize(self, p: Principal, tool: ToolInfo, args: dict, approved: bool = False) -> Decision:
        req = {"principal": {"type": "Agent", "id": p.agent}, "action": {"type": "Action", "id": "call"},
               "resource": {"type": "Tool", "id": tool.uid}, "context": self._context(p, args, approved)}
        r = cedarpy.is_authorized(req, self.policy_set, self._entities(tool), self.schema)
        reasons = tuple(self.ids.get(x, x) for x in (r.diagnostics.reasons or []))
        errors = tuple(str(e) for e in (r.diagnostics.errors or []))
        allowed = r.decision == cedarpy.Decision.Allow
        return Decision(allowed, reasons or (() if allowed else ("default-deny",)), errors)

    def visible(self, p: Principal, tool: ToolInfo) -> bool:
        """Could this agent ever call this tool (some arguments, with approval)? Otherwise hide it."""
        probe_args = {"namespace": "otel-demo"} if tool.server == "cluster" else {}
        return self.authorize(p, tool, probe_args, approved=True).allowed

    def visible_tools(self, p: Principal, tools: list[ToolInfo]) -> list[ToolInfo]:
        return [t for t in tools if self.visible(p, t)]
