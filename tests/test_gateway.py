"""The gateway end to end: a real HTTP server with two real upstream MCP servers (telemetry on a benchmark snapshot,
cluster actions in server-side dry-run with a temporary ledger), MCP clients holding tokens from the real issuer.

Checks: no token → 401; per-agent tool lists; read calls pass through; destructive calls need an approval for that
exact call by the person the executor acts for; a denial names the policy; the kill switch stops a valid token; the
audit chain verifies and detects an edit. Skipped if the snapshot or the cluster is not available."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from sao import kube
from sao.gateway import audit
from sao.gateway.approvals import sign_approval
from sao.gateway.killswitch import KillSwitch
from sao.gateway.limits import RateLimiter
from sao.gateway.policy import Policy
from sao.gateway.server import Gateway, Upstream
from sao.identity.issuer import ASSERTION_TYPE, AT_TYPE, CC, TX, Client, Issuer, Registry, make_assertion
from sao.identity.keys import LocalSigner
from sao.identity.verify import Verifier

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "data/itbench-aa/snapshots/Scenario-24"
ISS = "http://issuer.test"


def _cluster_up() -> bool:
    try:
        kube.kubectl("get", "ns", "otel-demo", timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not SNAP.exists() or not _cluster_up(), reason="needs the dev snapshot and the cluster")
SPEC = {
    "agent:commander": dict(scopes=["telemetry:read"], may_act_for_users=True, may_delegate_to=["agent:executor"]),
    "agent:investigator": dict(scopes=["telemetry:read"]),
    "agent:executor": dict(scopes=["telemetry:read", "cluster:write"]),
    "user:oncall": dict(scopes=["telemetry:read", "cluster:write"], kind="user"),
}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gw")
    keys = {c: LocalSigner.generate() for c in SPEC}
    issuer = Issuer(ISS, LocalSigner.generate(),
                    Registry({c: Client(client_id=c, jwk=keys[c].jwk(), **s) for c, s in SPEC.items()}))
    revoked: set[str] = set()
    verifier = Verifier({ISS: issuer.jwks()}, is_revoked=lambda jti, agents: bool(revoked & set(agents)))
    py = sys.executable
    ups = [Upstream("telemetry", StdioServerParameters(command=py, args=["-m", "sao.mcp.telemetry_server"],
                                                       env={"SAO_SNAPSHOT": str(SNAP)})),
           Upstream("cluster", StdioServerParameters(command=py, args=["-m", "sao.mcp.cluster_server"],
                                                     env={"SAO_DRY_RUN": "1",
                                                          "SAO_ACTION_LEDGER": str(tmp / "ledger.jsonl")}))]
    ks = KillSwitch(tmp / "killswitch.json")
    gw = Gateway(verifier, Policy(), tmp / "audit.jsonl", ups, killswitch=ks)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(gw.app(), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)

    def tok(cid, grant=CC, subject=None):
        f = {"grant_type": grant, "client_assertion_type": ASSERTION_TYPE,
             "client_assertion": make_assertion(keys[cid], cid, issuer.token_endpoint)}
        if subject:
            f |= {"subject_token": subject, "subject_token_type": AT_TYPE}
        return issuer.token(f)["access_token"]

    yield {"url": f"http://127.0.0.1:{port}", "tok": tok, "issuer": issuer, "revoked": revoked, "tmp": tmp, "ks": ks,
           "gw": gw}
    srv.should_exit = True
    th.join(10)


async def _session(url, token, approval=None, fn=None):
    headers = {"Authorization": f"Bearer {token}"} | ({"X-SAO-Approval": approval} if approval else {})
    async with create_mcp_http_client(headers=headers) as http, \
            streamable_http_client(url, http_client=http) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        return await fn(s)


def run(url, token, fn, approval=None):
    return asyncio.run(_session(url, token, approval, fn))


def executor_token(env):
    user = env["tok"]("user:oncall")
    return env["tok"]("agent:executor", TX, env["tok"]("agent:commander", TX, user))


def test_no_or_bad_token_is_401(env):
    import httpx
    assert httpx.post(env["url"] + "/mcp/telemetry/", json={}).status_code == 401
    assert httpx.post(env["url"] + "/mcp/telemetry/", json={},
                      headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401


def test_tool_lists_are_filtered_per_agent(env):
    lst = lambda s: s.list_tools()  # noqa: E731
    inv = env["tok"]("agent:investigator")
    assert len(run(env["url"] + "/mcp/telemetry/", inv, lst).tools) == 15
    assert run(env["url"] + "/mcp/cluster/", inv, lst).tools == []  # investigators don't even see write tools
    ex = executor_token(env)
    assert len(run(env["url"] + "/mcp/cluster/", ex, lst).tools) == 5


def test_read_call_passes_through(env):
    r = run(env["url"] + "/mcp/telemetry/", env["tok"]("agent:investigator"),
            lambda s: s.call_tool("alerts", {}))
    assert not r.is_error and "alert snapshots" in r.content[0].text and "<untrusted-data" in r.content[0].text


def test_destructive_call_needs_approval_of_that_exact_call(env):
    ex = executor_token(env)
    url = env["url"] + "/mcp/cluster/"
    args = {"namespace": "otel-demo", "name": "checkout", "idempotency_key": "gw-test-rollback-1"}
    denied = run(url, ex, lambda s: s.call_tool("rollback_deployment", args))
    assert denied.is_error and "destructive-needs-approval" in denied.content[0].text
    iss = env["issuer"]
    ok_approval = sign_approval(iss.signer, ISS, "user:oncall",
                                [{"server": "cluster", "tool": "rollback_deployment", "args": args}], "inc-1")
    allowed = run(url, ex, lambda s: s.call_tool("rollback_deployment", args), approval=ok_approval)
    assert not allowed.is_error and "[dry run]" in allowed.content[0].text
    other = dict(args, name="payment", idempotency_key="gw-test-rollback-2")  # approval doesn't cover this call
    assert run(url, ex, lambda s: s.call_tool("rollback_deployment", other), approval=ok_approval).is_error
    wrong_person = sign_approval(iss.signer, ISS, "user:someone-else",
                                 [{"server": "cluster", "tool": "rollback_deployment", "args": args}], "inc-1")
    assert run(url, ex, lambda s: s.call_tool("rollback_deployment", args), approval=wrong_person).is_error


def test_argument_rules_hold_even_with_approval(env):
    ex = executor_token(env)
    args = {"namespace": "otel-demo", "name": "cart", "replicas": 0, "idempotency_key": "gw-test-scale-0"}
    appr = sign_approval(env["issuer"].signer, ISS, "user:oncall",
                         [{"server": "cluster", "tool": "scale_deployment", "args": args}], "inc-1")
    r = run(env["url"] + "/mcp/cluster/", ex, lambda s: s.call_tool("scale_deployment", args), approval=appr)
    assert r.is_error and "no-scale-to-zero" in r.content[0].text


def test_kill_switch_stops_a_valid_token(env):
    inv = env["tok"]("agent:investigator")
    env["revoked"].add("agent:investigator")
    try:
        with pytest.raises(BaseException):  # noqa: B017 - refused with 401 before MCP starts (client error type varies)
            run(env["url"] + "/mcp/telemetry/", inv, lambda s: s.list_tools())
    finally:
        env["revoked"].clear()


def test_audit_chain_verifies_and_detects_edits(env):
    path = env["tmp"] / "audit.jsonl"
    ok, msg = audit.verify(path)
    assert ok, msg
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert {r["decision"] for r in rows if r["event"] == "call"} >= {"allow", "deny"}
    tampered = env["tmp"] / "tampered.jsonl"
    rows[1]["decision"] = "allow"
    tampered.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    ok, msg = audit.verify(tampered)
    assert not ok and "content changed" in msg


def test_disabled_tool_is_hidden_and_refused(env):
    inv = env["tok"]("agent:investigator")
    url = env["url"] + "/mcp/telemetry/"
    env["ks"].disable_tool("telemetry/search_logs", reason="test")
    try:
        names = [t.name for t in run(url, inv, lambda s: s.list_tools()).tools]
        assert "search_logs" not in names and "alerts" in names
        r = run(url, inv, lambda s: s.call_tool("search_logs", {"pattern": "error"}))
        assert r.is_error and "kill switch" in r.content[0].text
    finally:
        env["ks"].clear()


def test_kill_switch_time_to_effect(env):
    """Revoke an agent while it is calling; the next call after the revocation is refused. Measured end to end
    (HTTP + token verification), reported in DECISIONS.md."""
    import httpx
    inv = env["tok"]("agent:investigator")
    url = env["url"] + "/mcp/telemetry/"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Authorization": f"Bearer {inv}", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-06-18"}
    timings = []
    with httpx.Client(timeout=10) as http:
        for _ in range(5):
            assert http.post(url, json=body, headers=headers).status_code != 401
            t_revoke = time.perf_counter()
            env["ks"].revoke_agent("agent:investigator", reason="test")
            while http.post(url, json=body, headers=headers).status_code != 401:
                pass
            timings.append((time.perf_counter() - t_revoke) * 1000)
            env["ks"].clear()
    timings.sort()
    print(f"kill switch time to effect: median {timings[2]:.1f} ms, max {timings[-1]:.1f} ms")
    (env["tmp"] / "killswitch_timing.json").write_text(json.dumps(timings))
    assert timings[-1] < 1000


def test_rate_limit_refuses_a_runaway_agent(env):
    env["gw"].limiter = RateLimiter(per_min=6, burst=3)
    inv = env["tok"]("agent:investigator")
    try:
        async def burst(s):
            return [await s.call_tool("overview", {}) for _ in range(5)]
        rs = run(env["url"] + "/mcp/telemetry/", inv, burst)
        assert [r.is_error for r in rs][:3] == [False] * 3 and rs[-1].is_error
        assert "rate limit" in rs[-1].content[0].text
    finally:
        env["gw"].limiter = RateLimiter()


def test_oversized_output_is_cut_with_a_notice(env, monkeypatch):
    import sao.gateway.server as srv
    monkeypatch.setattr(srv, "cap", lambda text, limit=300: (text[:limit] + "\n… [cut by the gateway]", True)
                        if len(text) > limit else (text, False))
    r = run(env["url"] + "/mcp/telemetry/", env["tok"]("agent:investigator"), lambda s: s.call_tool("events", {}))
    assert not r.is_error and "cut by the gateway" in r.content[0].text


def test_gateway_spans_are_children_of_the_calling_agent(env):
    """Agents run inside the Claude CLI, which doesn't forward trace context: the orchestrator puts the agent span's
    traceparent in the MCP headers and the gateway's decision span must come out as its child."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from sao import tracing
    exporter = InMemorySpanExporter()
    if not tracing.setup(exporter=exporter):
        pytest.skip("a tracer provider was already configured in this process")
    with tracing.span("agent investigator:changes", kind="AGENT") as agent_span:
        tp = tracing.traceparent()

        async def call(s):
            return await s.call_tool("alerts", {})
        headers_token = env["tok"]("agent:investigator")

        async def go():
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
            hdr = {"Authorization": f"Bearer {headers_token}", **tp}
            async with create_mcp_http_client(headers=hdr) as http, \
                    streamable_http_client(env["url"] + "/mcp/telemetry/", http_client=http) as (r, w), \
                    ClientSession(r, w) as s:
                await s.initialize()
                return await call(s)
        assert not asyncio.run(go()).is_error
    spans = {s.name: s for s in exporter.get_finished_spans()}
    gw = spans["gateway telemetry/alerts"]
    assert gw.parent is not None and gw.parent.span_id == agent_span.get_span_context().span_id
    assert gw.context.trace_id == agent_span.get_span_context().trace_id
    assert gw.attributes["sao.decision"] == "allow" and gw.attributes["sao.agent"] == "agent:investigator"
    assert "read-only-tools" in gw.attributes["sao.policies"]
