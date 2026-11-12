"""Token issuer for agent identities (D10): OAuth client credentials with private_key_jwt, RFC 8693 token exchange
for acting on behalf of a person, OIDC discovery + JWKS so the gateway can trust any OIDC issuer by configuration.

Flows (all at POST {issuer}/token, form-encoded, RFC 6749 errors):
- client_credentials: the client authenticates with a signed JWT assertion (RFC 7523: iss = sub = client_id, aud =
  the token endpoint, short-lived, single-use jti). Result: an access token for the client itself
  (sub = client_id, client_id = client_id).
- token-exchange (urn:ietf:params:oauth:grant-type:token-exchange): the authenticated client (the actor) presents a
  subject_token (a user's token, or an already delegated token) and receives a token with sub = that user and
  act = {sub: actor, act: <previous act>} (RFC 8693 §4.1, nested actors). Allowed only if the registry lets the
  actor act for users, and, for a chain, if the previous actor may delegate to this one. Scope = requested ∩
  actor's scopes ∩ the user's grant (carried down the chain as sao_ceiling).

Subject tokens can come from this issuer or from a trusted external OIDC issuer (a customer's Okta/Entra id_token),
listed in the registry with its JWKS.

Access tokens: ES256, typ at+jwt (RFC 9068), 10 minutes, aud = the gateway, unique jti.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

import jwt

from sao.identity.keys import Signer, sign_jwt

CC = "client_credentials"
TX = "urn:ietf:params:oauth:grant-type:token-exchange"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
AT_TYPE = "urn:ietf:params:oauth:token-type:access_token"
ID_TYPE = "urn:ietf:params:oauth:token-type:id_token"
TOKEN_TTL_S = 600
MAX_ASSERTION_TTL_S = 300
LEEWAY_S = 30


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(f"{error}: {description}")
        self.error, self.description, self.status = error, description, status


@dataclass
class Client:
    client_id: str                     # e.g. agent:executor, user:oncall-jp
    jwk: dict                          # public key for its assertions
    scopes: list[str]
    kind: str = "agent"                # agent | user
    may_act_for_users: bool = False    # may exchange a user's token for one acting on the user's behalf
    may_delegate_to: list[str] = field(default_factory=list)  # agents allowed to continue a chain it started


@dataclass
class Registry:
    clients: dict[str, Client]
    external_issuers: dict[str, dict] = field(default_factory=dict)  # iss → {"jwks": {...}, "audience": str}


class Issuer:
    def __init__(self, issuer_url: str, signer: Signer, registry: Registry, audience: str = "sao-gateway"):
        self.iss = issuer_url.rstrip("/")
        self.token_endpoint = f"{self.iss}/token"
        self.signer = signer
        self.registry = registry
        self.audience = audience
        self._seen_jti: dict[str, float] = {}

    # ---------- metadata ----------
    def discovery(self) -> dict:
        return {"issuer": self.iss, "token_endpoint": self.token_endpoint, "jwks_uri": f"{self.iss}/jwks.json",
                "grant_types_supported": [CC, TX], "token_endpoint_auth_methods_supported": ["private_key_jwt"],
                "token_endpoint_auth_signing_alg_values_supported": ["ES256"],
                "id_token_signing_alg_values_supported": ["ES256"], "response_types_supported": ["token"],
                "subject_types_supported": ["public"]}

    def jwks(self) -> dict:
        return {"keys": [self.signer.jwk()]}

    # ---------- client authentication ----------
    def authenticate(self, form: dict) -> Client:
        if form.get("client_assertion_type") != ASSERTION_TYPE or not form.get("client_assertion"):
            raise OAuthError("invalid_client", "private_key_jwt client assertion required", 401)
        assertion = form["client_assertion"]
        try:
            cid = jwt.decode(assertion, options={"verify_signature": False})["iss"]
        except (jwt.PyJWTError, KeyError) as e:
            raise OAuthError("invalid_client", f"malformed assertion: {e}", 401) from None
        client = self.registry.clients.get(cid)
        if client is None:
            raise OAuthError("invalid_client", "unknown client", 401)
        try:
            claims = jwt.decode(assertion, jwt.PyJWK(client.jwk).key, algorithms=["ES256"],
                                audience=self.token_endpoint, issuer=cid, leeway=LEEWAY_S,
                                options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]})
        except jwt.PyJWTError as e:
            raise OAuthError("invalid_client", f"assertion rejected: {e}", 401) from None
        if claims["sub"] != cid:
            raise OAuthError("invalid_client", "assertion sub must equal iss", 401)
        if claims["exp"] - claims["iat"] > MAX_ASSERTION_TTL_S:
            raise OAuthError("invalid_client", "assertion lifetime too long", 401)
        now = time.time()
        self._seen_jti = {j: t for j, t in self._seen_jti.items() if t > now}
        key = f"{cid}:{claims['jti']}"
        if key in self._seen_jti:
            raise OAuthError("invalid_client", "assertion replayed", 401)
        self._seen_jti[key] = claims["exp"] + LEEWAY_S
        return client

    # ---------- grants ----------
    def token(self, form: dict) -> dict:
        client = self.authenticate(form)
        grant = form.get("grant_type")
        if grant == CC:
            return self._client_credentials(client, form)
        if grant == TX:
            return self._exchange(client, form)
        raise OAuthError("unsupported_grant_type", str(grant))

    def _scopes(self, requested: str | None, *allowed: list[str]) -> list[str]:
        grant = set(allowed[0])
        for a in allowed[1:]:
            grant &= set(a)
        if requested:
            req = set(requested.split())
            if not req <= grant:
                raise OAuthError("invalid_scope", f"not allowed: {sorted(req - grant)}")
            grant = req
        return sorted(grant)

    def _mint(self, sub: str, client_id: str, scopes: list[str], act: dict | None = None,
              ceiling: list[str] | None = None) -> dict:
        now = int(time.time())
        claims = {"iss": self.iss, "sub": sub, "aud": self.audience, "iat": now, "nbf": now,
                  "exp": now + TOKEN_TTL_S, "jti": str(uuid.uuid4()), "client_id": client_id,
                  "scope": " ".join(scopes)}
        if act:
            claims["act"] = act
            claims["sao_ceiling"] = " ".join(ceiling or [])
        return {"access_token": sign_jwt(self.signer, claims), "token_type": "Bearer", "expires_in": TOKEN_TTL_S,
                "scope": claims["scope"], "issued_token_type": AT_TYPE}

    def _client_credentials(self, client: Client, form: dict) -> dict:
        return self._mint(client.client_id, client.client_id, self._scopes(form.get("scope"), client.scopes))

    def _verify_subject(self, token: str, token_type: str) -> dict:
        try:
            iss = jwt.decode(token, options={"verify_signature": False})["iss"]
        except (jwt.PyJWTError, KeyError) as e:
            raise OAuthError("invalid_grant", f"malformed subject_token: {e}") from None
        if iss == self.iss:
            key, aud = jwt.PyJWK(self.signer.jwk()).key, self.audience
        elif iss in self.registry.external_issuers:
            ext = self.registry.external_issuers[iss]
            kid = jwt.get_unverified_header(token).get("kid")
            match = [k for k in ext["jwks"]["keys"] if k.get("kid") == kid]
            if not match:
                raise OAuthError("invalid_grant", "unknown key for external issuer")
            key, aud = jwt.PyJWK(match[0]).key, ext["audience"]
        else:
            raise OAuthError("invalid_grant", "subject_token from an untrusted issuer")
        try:
            return jwt.decode(token, key, algorithms=["ES256", "RS256"], audience=aud, issuer=iss, leeway=LEEWAY_S,
                              options={"require": ["iss", "sub", "exp"]})
        except jwt.PyJWTError as e:
            raise OAuthError("invalid_grant", f"subject_token rejected: {e}") from None

    def _exchange(self, actor: Client, form: dict) -> dict:
        if form.get("subject_token_type") not in (AT_TYPE, ID_TYPE) or not form.get("subject_token"):
            raise OAuthError("invalid_request", "subject_token of type access_token or id_token required")
        if form.get("audience", self.audience) != self.audience:
            raise OAuthError("invalid_target", "unknown audience")
        subj = self._verify_subject(form["subject_token"], form["subject_token_type"])
        prev_act = subj.get("act")
        if prev_act:  # continuing a chain: the previous actor must allow this one
            prev = self.registry.clients.get(prev_act.get("sub", ""))
            if prev is None or actor.client_id not in prev.may_delegate_to:
                raise OAuthError("invalid_grant", f"{prev_act.get('sub')} may not delegate to {actor.client_id}")
            user = subj["sub"]
        else:
            user_client = self.registry.clients.get(subj["sub"])
            if subj["iss"] == self.iss and (user_client is None or user_client.kind != "user"):
                raise OAuthError("invalid_grant", "subject is not a user")
            if not actor.may_act_for_users:
                raise OAuthError("unauthorized_client", f"{actor.client_id} may not act for users")
            user = subj["sub"] if subj["iss"] == self.iss else f"{subj['iss']}#{subj['sub']}"
        # Ceiling = what the user granted, carried down the chain. Each hop gets its own scopes ∩ the ceiling, so a
        # router (the commander, read-only) can hand off to the executor without the executor losing write, and no
        # hop ever exceeds the user. An external user's rights are decided by Cedar, so the ceiling is the actor's.
        if prev_act:
            ceiling = subj.get("sao_ceiling", "").split()
        else:
            ceiling = subj.get("scope", "").split() if subj["iss"] == self.iss else actor.scopes
        scopes = self._scopes(form.get("scope"), actor.scopes, ceiling)
        act = {"sub": actor.client_id, **({"act": prev_act} if prev_act else {})}
        return self._mint(user, actor.client_id, scopes, act, ceiling=sorted(ceiling))


def make_assertion(signer: Signer, client_id: str, token_endpoint: str, ttl_s: int = 60) -> str:
    """The client side of private_key_jwt: a short-lived, single-use assertion signed with the client's key."""
    now = int(time.time())
    return sign_jwt(signer, {"iss": client_id, "sub": client_id, "aud": token_endpoint, "iat": now, "exp": now + ttl_s,
                             "jti": str(uuid.uuid4())}, typ="JWT")
