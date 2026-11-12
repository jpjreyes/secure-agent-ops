"""Signing keys for the token issuer (ES256, P-256).

- LocalSigner: a key file on disk (local evals). Created on first use, chmod 600, outside git (runs/identity/).
- KmsSigner: an AWS KMS ECC_NIST_P256 key (the deployed issuer); the private key never leaves KMS. KMS returns DER
  ECDSA signatures; JWS (RFC 7518 §3.4) needs the raw 64-byte r||s, so the signature is converted.

Both sign the JWS signing input themselves, so the same token code runs locally and on Lambda.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def der_to_raw(der: bytes) -> bytes:
    """DER ECDSA signature (KMS, cryptography) → JWS r||s, 32 bytes each for P-256."""
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def raw_to_der(raw: bytes) -> bytes:
    return encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))


def public_jwk(pub: ec.EllipticCurvePublicKey, kid: str | None = None) -> dict:
    n = pub.public_numbers()
    jwk = {"kty": "EC", "crv": "P-256", "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big")),
           "use": "sig", "alg": "ES256"}
    jwk["kid"] = kid or thumbprint(jwk)
    return jwk


def thumbprint(jwk: dict) -> str:
    """RFC 7638 JWK thumbprint (stable key id)."""
    canon = json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"), sort_keys=True)
    return b64u(hashlib.sha256(canon.encode()).digest())


class Signer(Protocol):
    kid: str

    def sign(self, signing_input: bytes) -> bytes: ...   # raw r||s

    def jwk(self) -> dict: ...


class LocalSigner:
    def __init__(self, key: ec.EllipticCurvePrivateKey):
        self._key = key
        self._jwk = public_jwk(key.public_key())
        self.kid = self._jwk["kid"]

    @classmethod
    def generate(cls) -> LocalSigner:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_file(cls, path: Path) -> LocalSigner:
        """Load the key, or create it (0600) if missing."""
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
        return cls(serialization.load_pem_private_key(path.read_bytes(), password=None))

    def sign(self, signing_input: bytes) -> bytes:
        return der_to_raw(self._key.sign(signing_input, ec.ECDSA(hashes.SHA256())))

    def jwk(self) -> dict:
        return dict(self._jwk)


class KmsSigner:
    """AWS KMS asymmetric key (KeySpec ECC_NIST_P256, KeyUsage SIGN_VERIFY). Used by the deployed issuer."""

    def __init__(self, key_id: str, client=None):
        self.key_id = key_id
        if client is None:
            import boto3  # only on AWS
            client = boto3.client("kms")
        self.kms = client
        der_pub = self.kms.get_public_key(KeyId=key_id)["PublicKey"]
        self._jwk = public_jwk(serialization.load_der_public_key(der_pub))
        self.kid = self._jwk["kid"]

    def sign(self, signing_input: bytes) -> bytes:
        digest = hashlib.sha256(signing_input).digest()
        der = self.kms.sign(KeyId=self.key_id, Message=digest, MessageType="DIGEST",
                            SigningAlgorithm="ECDSA_SHA_256")["Signature"]
        return der_to_raw(der)

    def jwk(self) -> dict:
        return dict(self._jwk)


def sign_jwt(signer: Signer, claims: dict, typ: str = "at+jwt") -> str:
    """Compact JWS with ES256, signed by `signer` (local key or KMS)."""
    header = {"alg": "ES256", "typ": typ, "kid": signer.kid}
    enc = lambda o: b64u(json.dumps(o, separators=(",", ":")).encode())  # noqa: E731
    signing_input = f"{enc(header)}.{enc(claims)}".encode()
    return signing_input.decode() + "." + b64u(signer.sign(signing_input))
