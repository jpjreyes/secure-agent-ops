"""Run the gateway for one incident, in-process, and hand agents their connection (URL + token header).

  with running_gateway(snapshot, cluster=False) as gw:
      server_cfg = gw.mcp("telemetry", agent="agent:investigator")   # Agent SDK mcp_servers entry

Tokens come from the local issuer (keys under runs/identity/), minted the way the orchestrator would in a
deployment: client credentials for the agent itself, token exchange through the commander for the executor acting
for the on-call user. The audit log of the incident goes to <audit_dir>/audit.jsonl.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from mcp.client.stdio import StdioServerParameters

from sao.gateway.policy import Policy
from sao.gateway.server import Gateway, Upstream
from sao.identity.app import client_key, local_issuer
from sao.identity.issuer import ASSERTION_TYPE, AT_TYPE, CC, TX, Issuer, make_assertion
from sao.identity.verify import Verifier

ONCALL = "user:oncall-jp"


@dataclass
class GatewayHandle:
    url: str
    issuer: Issuer
    audit_path: Path
    screening: bool = True

    def token(self, agent: str, on_behalf_of: str | None = None) -> str:
        def grant(cid: str, **form) -> str:
            f = {"client_assertion_type": ASSERTION_TYPE,
                 "client_assertion": make_assertion(client_key(cid), cid, self.issuer.token_endpoint), **form}
            return self.issuer.token(f)["access_token"]
        if not on_behalf_of:
            return grant(agent, grant_type=CC)
        user = grant(on_behalf_of, grant_type=CC)
        via_commander = grant("agent:commander", grant_type=TX, subject_token=user, subject_token_type=AT_TYPE)
        if agent == "agent:commander":
            return via_commander
        return grant(agent, grant_type=TX, subject_token=via_commander, subject_token_type=AT_TYPE)

    def mcp(self, server: str, agent: str, on_behalf_of: str | None = None, approval: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token(agent, on_behalf_of)}"}
        if approval:
            headers["X-SAO-Approval"] = approval
        return {"type": "http", "url": f"{self.url}/mcp/{server}/", "headers": headers}


@contextmanager
def running_gateway(snapshot: Path, audit_dir: Path, cluster: bool = False, dry_run: bool = True,
                    is_revoked=None, screening: bool = True):
    issuer = local_issuer("http://127.0.0.1:18100")
    verifier = Verifier({issuer.iss: issuer.jwks()}, is_revoked=is_revoked)
    py = sys.executable
    ups = [Upstream("telemetry", StdioServerParameters(command=py, args=["-m", "sao.mcp.telemetry_server"],
                                                       env={"SAO_SNAPSHOT": str(snapshot)}))]
    if cluster:
        ups.append(Upstream("cluster", StdioServerParameters(
            command=py, args=["-m", "sao.mcp.cluster_server"],
            env={"SAO_DRY_RUN": "1" if dry_run else "0", "SAO_ACTION_LEDGER": str(audit_dir / "actions.jsonl")})))
    audit_dir.mkdir(parents=True, exist_ok=True)
    gw = Gateway(verifier, Policy(), audit_dir / "audit.jsonl", ups, screening=screening)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(gw.app(), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("gateway did not start")
    try:
        yield GatewayHandle(f"http://127.0.0.1:{port}", issuer, audit_dir / "audit.jsonl", screening)
    finally:
        srv.should_exit = True
        th.join(15)
