"""Agent identity (D10): the flows work, and every way to get or use a token you shouldn't is refused.

Keys are generated in memory; the HTTP tests run the Starlette app in-process."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from sao.identity.app import TokenClient, create_app
from sao.identity.issuer import ASSERTION_TYPE, AT_TYPE, CC, TX, Client, Issuer, OAuthError, Registry, make_assertion
from sao.identity.keys import KmsSigner, LocalSigner, b64u, der_to_raw, raw_to_der, sign_jwt
from sao.identity.verify import AuthError, Verifier

ISS = "http://issuer.test"
SPEC = {
    "agent:commander": dict(scopes=["telemetry:read"], may_act_for_users=True, may_delegate_to=["agent:executor"]),
    "agent:investigator": dict(scopes=["telemetry:read"]),
    "agent:executor": dict(scopes=["telemetry:read", "cluster:write"]),
    "user:oncall": dict(scopes=["telemetry:read", "cluster:write"], kind="user"),
    "user:viewer": dict(scopes=["telemetry:read"], kind="user"),
}


@pytest.fixture(scope="module")
def world():
    keys = {cid: LocalSigner.generate() for cid in SPEC}
    reg = Registry({cid: Client(client_id=cid, jwk=keys[cid].jwk(), **spec) for cid, spec in SPEC.items()})
    issuer = Issuer(ISS, LocalSigner.generate(), reg)
    revoked: set[str] = set()
    verifier = Verifier({ISS: issuer.jwks()}, is_revoked=lambda jti, agents: jti in revoked or bool(
        revoked & set(agents)))
    return issuer, keys, verifier, revoked


def form(issuer, keys, cid, **extra):
    return {"client_assertion_type": ASSERTION_TYPE,
            "client_assertion": make_assertion(keys[cid], cid, issuer.token_endpoint), **extra}


def cc(issuer, keys, cid, scope=None):
    return issuer.token(form(issuer, keys, cid, grant_type=CC, **({"scope": scope} if scope else {})))["access_token"]


def tx(issuer, keys, actor, subject, scope=None):
    return issuer.token(form(issuer, keys, actor, grant_type=TX, subject_token=subject, subject_token_type=AT_TYPE,
                             **({"scope": scope} if scope else {})))["access_token"]


# ---------- flows ----------
def test_client_credentials_gives_the_agent_its_own_identity(world):
    issuer, keys, verifier, _ = world
    p = verifier.verify(cc(issuer, keys, "agent:investigator"))
    assert p.agent == "agent:investigator" and p.on_behalf_of is None and p.chain == ()
    assert p.scopes == {"telemetry:read"}


def test_delegation_chain_user_commander_executor(world):
    issuer, keys, verifier, _ = world
    user = cc(issuer, keys, "user:oncall")
    by_commander = tx(issuer, keys, "agent:commander", user)
    by_executor = tx(issuer, keys, "agent:executor", by_commander)
    p = verifier.verify(by_executor)
    assert p.agent == "agent:executor" and p.on_behalf_of == "user:oncall"
    assert p.chain == ("agent:commander", "agent:executor")
    assert p.scopes == {"telemetry:read", "cluster:write"}  # own scopes ∩ the user's grant, not ∩ the commander's
    c = jwt.decode(by_executor, options={"verify_signature": False})
    assert c["act"] == {"sub": "agent:executor", "act": {"sub": "agent:commander"}}  # RFC 8693 nested actors


def test_delegation_never_exceeds_the_user(world):
    issuer, keys, verifier, _ = world
    viewer = cc(issuer, keys, "user:viewer")
    by_executor = tx(issuer, keys, "agent:executor", tx(issuer, keys, "agent:commander", viewer))
    assert verifier.verify(by_executor).scopes == {"telemetry:read"}  # a read-only user can't get writes done


# ---------- refused: getting tokens ----------
def test_assertion_signed_with_another_key_is_refused(world):
    issuer, keys, *_ = world
    f = form(issuer, keys, "agent:investigator", grant_type=CC)
    f["client_assertion"] = make_assertion(keys["agent:investigator"], "agent:executor", issuer.token_endpoint)
    with pytest.raises(OAuthError, match="invalid_client"):
        issuer.token(f)


def test_replayed_assertion_is_refused(world):
    issuer, keys, *_ = world
    f = form(issuer, keys, "agent:investigator", grant_type=CC)
    issuer.token(dict(f))
    with pytest.raises(OAuthError, match="replayed"):
        issuer.token(dict(f))


def test_assertion_for_another_audience_or_too_long_is_refused(world):
    issuer, keys, *_ = world
    f = form(issuer, keys, "agent:investigator", grant_type=CC)
    f["client_assertion"] = make_assertion(keys["agent:investigator"], "agent:investigator", "http://evil/token")
    with pytest.raises(OAuthError, match="invalid_client"):
        issuer.token(f)
    f["client_assertion"] = make_assertion(keys["agent:investigator"], "agent:investigator", issuer.token_endpoint,
                                           ttl_s=3600)
    with pytest.raises(OAuthError, match="lifetime"):
        issuer.token(f)


def test_scope_escalation_is_refused(world):
    issuer, keys, *_ = world
    with pytest.raises(OAuthError, match="invalid_scope"):
        cc(issuer, keys, "agent:investigator", scope="cluster:write")


def test_only_allowed_agents_act_for_users(world):
    issuer, keys, *_ = world
    user = cc(issuer, keys, "user:oncall")
    for actor in ("agent:investigator", "agent:executor"):  # the executor only acts through the commander
        with pytest.raises(OAuthError, match="may not act for users"):
            tx(issuer, keys, actor, user)


def test_chain_can_only_continue_to_allowed_agents(world):
    issuer, keys, *_ = world
    by_commander = tx(issuer, keys, "agent:commander", cc(issuer, keys, "user:oncall"))
    with pytest.raises(OAuthError, match="may not delegate"):
        tx(issuer, keys, "agent:investigator", by_commander)


def test_an_agent_token_is_not_a_user(world):
    issuer, keys, *_ = world
    with pytest.raises(OAuthError, match="not a user"):
        tx(issuer, keys, "agent:commander", cc(issuer, keys, "agent:investigator"))


# ---------- refused: using tokens at the gateway ----------
def test_expired_token_is_refused(world):
    issuer, _, verifier, _ = world
    now = int(time.time())
    old = sign_jwt(issuer.signer, {"iss": ISS, "sub": "agent:executor", "aud": "sao-gateway", "iat": now - 900,
                                   "exp": now - 300, "jti": "x"})
    with pytest.raises(AuthError, match="(?i)expired"):
        verifier.verify(old)


def test_wrong_audience_and_untrusted_issuer_are_refused(world):
    issuer, keys, verifier, _ = world
    now = int(time.time())
    other_aud = sign_jwt(issuer.signer, {"iss": ISS, "sub": "a", "aud": "another-api", "iat": now, "exp": now + 60,
                                         "jti": "y"})
    with pytest.raises(AuthError, match="(?i)audience"):
        verifier.verify(other_aud)
    rogue = LocalSigner.generate()
    forged = sign_jwt(rogue, {"iss": "http://rogue", "sub": "agent:executor", "aud": "sao-gateway", "iat": now,
                              "exp": now + 60, "jti": "z"})
    with pytest.raises(AuthError, match="untrusted issuer"):
        verifier.verify(forged)
    forged_same_iss = sign_jwt(rogue, {"iss": ISS, "sub": "agent:executor", "aud": "sao-gateway", "iat": now,
                                       "exp": now + 60, "jti": "z"})
    with pytest.raises(AuthError, match="unknown signing key"):
        verifier.verify(forged_same_iss)


def test_algorithm_confusion_and_none_are_refused(world):
    issuer, _, verifier, _ = world
    now = int(time.time())
    claims = {"iss": ISS, "sub": "agent:executor", "aud": "sao-gateway", "iat": now, "exp": now + 60, "jti": "q",
              "scope": "cluster:write"}
    unsigned = jwt.encode(claims, key=None, algorithm="none", headers={"typ": "at+jwt"})
    with pytest.raises(AuthError, match="algorithm"):
        verifier.verify(unsigned)
    hs = jwt.encode(claims, "secret-guess", algorithm="HS256", headers={"typ": "at+jwt"})
    with pytest.raises(AuthError, match="algorithm"):
        verifier.verify(hs)


def test_client_assertion_cannot_be_used_as_access_token(world):
    issuer, keys, verifier, _ = world
    a = make_assertion(keys["agent:executor"], "agent:executor", issuer.token_endpoint)
    with pytest.raises(AuthError, match="typ"):
        verifier.verify(a)


def test_kill_switch_revokes_an_agent_anywhere_in_the_chain(world):
    issuer, keys, verifier, revoked = world
    tok = tx(issuer, keys, "agent:executor", tx(issuer, keys, "agent:commander", cc(issuer, keys, "user:oncall")))
    verifier.verify(tok)
    revoked.add("agent:commander")  # revoking the delegator also stops what it delegated
    try:
        with pytest.raises(AuthError, match="revoked"):
            verifier.verify(tok)
    finally:
        revoked.clear()


# ---------- keys, KMS format, HTTP ----------
def test_der_raw_round_trip_and_kms_signer(world):
    key = ec.generate_private_key(ec.SECP256R1())
    der = key.sign(b"msg", ec.ECDSA(hashes.SHA256()))
    assert raw_to_der(der_to_raw(der)) == der and len(der_to_raw(der)) == 64

    class FakeKms:  # behaves like boto3 kms: DER public key, DER signature over a SHA-256 digest
        def get_public_key(self, KeyId):
            from cryptography.hazmat.primitives import serialization
            return {"PublicKey": key.public_key().public_bytes(serialization.Encoding.DER,
                                                               serialization.PublicFormat.SubjectPublicKeyInfo)}

        def sign(self, KeyId, Message, MessageType, SigningAlgorithm):
            from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
            assert MessageType == "DIGEST" and SigningAlgorithm == "ECDSA_SHA_256"
            return {"Signature": key.sign(Message, ec.ECDSA(Prehashed(hashes.SHA256())))}

    signer = KmsSigner("alias/sao-issuer", client=FakeKms())
    now = int(time.time())
    tok = sign_jwt(signer, {"iss": ISS, "sub": "agent:executor", "aud": "sao-gateway", "iat": now, "exp": now + 60,
                            "jti": "k"})
    assert Verifier({ISS: {"keys": [signer.jwk()]}}).verify(tok).agent == "agent:executor"
    assert b64u(b"") == ""


def test_http_discovery_jwks_and_token_client(world):
    issuer, keys, _, _ = world
    from starlette.testclient import TestClient
    with TestClient(create_app(issuer), base_url=ISS) as tc:
        d = tc.get("/.well-known/openid-configuration").json()
        assert d["token_endpoint"] == f"{ISS}/token" and "private_key_jwt" in d["token_endpoint_auth_methods_supported"]
        jwks = tc.get("/jwks.json").json()
        user = TokenClient(ISS, "user:oncall", keys["user:oncall"], http=tc).client_credentials()
        tok = TokenClient(ISS, "agent:commander", keys["agent:commander"], http=tc).exchange(user)
        p = Verifier({ISS: jwks}).verify(tok)
        assert p.agent == "agent:commander" and p.on_behalf_of == "user:oncall"
        bad = tc.post("/token", data={"grant_type": CC})
        assert bad.status_code == 401 and bad.json()["error"] == "invalid_client"
        assert bad.headers["cache-control"] == "no-store"
