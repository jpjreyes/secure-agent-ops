"""Gateway side: verify an access token and turn it into the principal Cedar decides on.

Trusts issuers by configuration (issuer URL + JWKS), so this project's issuer, a customer's Okta or Entra ID work
the same way. Checks: ES256 only (no `none`, no algorithm confusion), known key id, issuer, audience = the gateway,
exp/nbf with 30 s leeway, typ at+jwt, then the kill switch on the token id and on every agent in the chain.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import jwt

LEEWAY_S = 30


class AuthError(Exception):
    """401: missing, invalid, expired or revoked credentials."""


@dataclass(frozen=True)
class Principal:
    agent: str                 # the agent making this call (innermost actor, or the client itself)
    on_behalf_of: str | None   # the user, if the token is delegated
    chain: tuple[str, ...]     # actors from the first delegate to the caller, e.g. (agent:commander, agent:executor)
    scopes: frozenset[str]
    token_id: str
    issuer: str
    expires_at: int


def _chain(act: dict | None) -> list[str]:
    out = []
    while act:
        out.append(act["sub"])
        act = act.get("act")
    return list(reversed(out))  # outermost act is the current actor → reverse to first-delegate-first


class Verifier:
    def __init__(self, trusted: dict[str, dict], audience: str = "sao-gateway",
                 is_revoked: Callable[[str, list[str]], bool] | None = None):
        """trusted: issuer → JWKS ({"keys": [...]}). is_revoked(token_id, agents) → True to refuse (kill switch)."""
        self.trusted = trusted
        self.audience = audience
        self.is_revoked = is_revoked or (lambda _jti, _agents: False)

    def verify(self, token: str | None) -> Principal:
        if not token:
            raise AuthError("missing bearer token")
        try:
            header = jwt.get_unverified_header(token)
            iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError as e:
            raise AuthError(f"malformed token: {e}") from None
        if header.get("alg") != "ES256":
            raise AuthError(f"algorithm not allowed: {header.get('alg')}")
        if header.get("typ") != "at+jwt":
            raise AuthError("not an access token (typ must be at+jwt)")
        jwks = self.trusted.get(iss)
        if jwks is None:
            raise AuthError("untrusted issuer")
        key = next((k for k in jwks["keys"] if k.get("kid") == header.get("kid")), None)
        if key is None:
            raise AuthError("unknown signing key")
        try:
            c = jwt.decode(token, jwt.PyJWK(key).key, algorithms=["ES256"], audience=self.audience, issuer=iss,
                           leeway=LEEWAY_S, options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]})
        except jwt.PyJWTError as e:
            raise AuthError(f"token rejected: {e}") from None
        chain = _chain(c.get("act"))
        agent = chain[-1] if chain else c["sub"]
        if self.is_revoked(c["jti"], chain or [agent]):
            raise AuthError("revoked (kill switch)")
        return Principal(agent=agent, on_behalf_of=c["sub"] if chain else None, chain=tuple(chain),
                         scopes=frozenset(c.get("scope", "").split()), token_id=c["jti"], issuer=iss,
                         expires_at=c["exp"])
