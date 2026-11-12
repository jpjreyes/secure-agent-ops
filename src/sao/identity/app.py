"""HTTP surface of the issuer (Starlette) and the client helper agents' orchestrator uses.

  uv run uvicorn sao.identity.app:serve --factory --port 18100     (local issuer; keys under runs/identity/)

Endpoints: GET /.well-known/openid-configuration, GET /jwks.json, POST /token (form-encoded, RFC 6749 errors).
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sao.identity.issuer import ASSERTION_TYPE, AT_TYPE, CC, TX, Client, Issuer, OAuthError, Registry, make_assertion
from sao.identity.keys import LocalSigner, Signer

ROOT = Path(__file__).resolve().parents[3]
KEYS = ROOT / "runs" / "identity" / "keys"
REGISTRY = ROOT / "infra" / "identity" / "registry.yaml"


def client_key(client_id: str) -> LocalSigner:
    return LocalSigner.from_file(KEYS / f"{client_id.replace(':', '_')}.pem")


def load_registry(path: Path = REGISTRY) -> Registry:
    """Local registry: public keys come from each client's local key file (created on first use)."""
    cfg = yaml.safe_load(path.read_text())
    clients = {cid: Client(client_id=cid, jwk=client_key(cid).jwk(), scopes=c.get("scopes", []),
                           kind=c.get("kind", "agent"), may_act_for_users=c.get("may_act_for_users", False),
                           may_delegate_to=c.get("may_delegate_to", []))
               for cid, c in cfg["clients"].items()}
    ext = {}
    for iss, e in (cfg.get("external_issuers") or {}).items():
        ext[iss] = {"jwks": httpx.get(e["jwks_uri"], timeout=10).json(), "audience": e["audience"]}
    return Registry(clients, ext)


def create_app(issuer: Issuer) -> Starlette:
    async def discovery(_: Request):
        return JSONResponse(issuer.discovery())

    async def jwks(_: Request):
        return JSONResponse(issuer.jwks(), headers={"Cache-Control": "max-age=300"})

    async def token(req: Request):
        form = dict(await req.form())
        try:
            return JSONResponse(issuer.token(form), headers={"Cache-Control": "no-store"})
        except OAuthError as e:
            return JSONResponse({"error": e.error, "error_description": e.description}, status_code=e.status,
                                headers={"Cache-Control": "no-store"})

    return Starlette(routes=[Route("/.well-known/openid-configuration", discovery), Route("/jwks.json", jwks),
                             Route("/token", token, methods=["POST"])])


def local_issuer(url: str | None = None) -> Issuer:
    url = url or os.environ.get("SAO_ISSUER_URL", "http://127.0.0.1:18100")
    return Issuer(url, LocalSigner.from_file(KEYS / "issuer.pem"), load_registry())


class TokenClient:
    """What the orchestrator does for each agent: authenticate with the agent's key, get or exchange a token."""

    def __init__(self, issuer_url: str, client_id: str, signer: Signer, http: httpx.Client | None = None):
        self.issuer_url, self.client_id, self.signer = issuer_url.rstrip("/"), client_id, signer
        self.http = http or httpx.Client(timeout=10)
        self.token_endpoint = f"{self.issuer_url}/token"

    def _post(self, form: dict) -> dict:
        form |= {"client_assertion_type": ASSERTION_TYPE,
                 "client_assertion": make_assertion(self.signer, self.client_id, self.token_endpoint)}
        r = self.http.post(self.token_endpoint, data=form)
        body = r.json()
        if r.status_code != 200:
            raise OAuthError(body.get("error", "error"), body.get("error_description", ""), r.status_code)
        return body

    def client_credentials(self, scope: str | None = None) -> str:
        return self._post({"grant_type": CC, **({"scope": scope} if scope else {})})["access_token"]

    def exchange(self, subject_token: str, scope: str | None = None, subject_type: str = AT_TYPE) -> str:
        return self._post({"grant_type": TX, "subject_token": subject_token, "subject_token_type": subject_type,
                           **({"scope": scope} if scope else {})})["access_token"]


def serve() -> Starlette:
    """uvicorn factory: uv run uvicorn sao.identity.app:serve --factory --port 18100"""
    return create_app(local_issuer())
