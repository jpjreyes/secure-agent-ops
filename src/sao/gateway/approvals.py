"""Human approvals: a signed list of the exact actions a person approved, never a claim in an agent's token.

The approver reviews the executor's plan (tool + arguments for each action) and the issuer signs an approval token
(typ approval+jwt) listing the hash of each approved call, for this gateway, valid a few minutes. The executor's MCP
connection carries it in the X-SAO-Approval header; the gateway marks a call `approved` for Cedar only if the call's
exact (server, tool, arguments) hash is in the list. The model never needs to see or forward it, and a changed
argument (another service, another replica count) is simply not approved.
"""

from __future__ import annotations

import time
import uuid

import jwt

from sao.gateway.audit import call_hash
from sao.identity.keys import Signer, sign_jwt

APPROVAL_TTL_S = 900


def sign_approval(signer: Signer, issuer: str, approver: str, actions: list[dict], incident: str,
                  audience: str = "sao-gateway", ttl_s: int = APPROVAL_TTL_S) -> str:
    """actions: [{"server", "tool", "args"}] exactly as the executor will call them."""
    now = int(time.time())
    return sign_jwt(signer, {"iss": issuer, "sub": approver, "aud": audience, "iat": now, "exp": now + ttl_s,
                             "jti": str(uuid.uuid4()), "incident": incident,
                             "actions": sorted(call_hash(a["server"], a["tool"], a["args"]) for a in actions)},
                    typ="approval+jwt")


class ApprovalChecker:
    def __init__(self, trusted: dict[str, dict], audience: str = "sao-gateway"):
        self.trusted, self.audience = trusted, audience

    def approved(self, token: str | None, server: str, tool: str, args: dict, on_behalf_of: str | None
                 ) -> tuple[bool, str]:
        """(approved?, why) for one call. The approver must be the person the executor acts for."""
        if not token:
            return False, "no approval"
        try:
            h = jwt.get_unverified_header(token)
            iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
            jwks = self.trusted.get(iss) or {"keys": []}
            key = next((k for k in jwks["keys"] if k.get("kid") == h.get("kid")), None)
            if h.get("alg") != "ES256" or h.get("typ") != "approval+jwt" or key is None:
                return False, "approval not from a trusted issuer"
            c = jwt.decode(token, jwt.PyJWK(key).key, algorithms=["ES256"], audience=self.audience, issuer=iss,
                           leeway=30, options={"require": ["sub", "exp", "actions"]})
        except jwt.PyJWTError as e:
            return False, f"approval rejected: {e}"
        if on_behalf_of is None or c["sub"] != on_behalf_of:
            return False, f"approved by {c['sub']}, but the call acts for {on_behalf_of}"
        if call_hash(server, tool, args) not in c["actions"]:
            return False, "this exact call was not approved"
        return True, f"approved by {c['sub']} ({c['jti']})"
