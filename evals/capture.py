"""Request capture: what does the model actually receive on the subscription path?

Starts a request-logging proxy on 127.0.0.1 that forwards to the Anthropic API unchanged, points the CLI at it
(guard.CAPTURE_URL, loopback only), runs one setup-A agent and one team investigator on a dev scenario, and checks
every captured request against what we intend to send:
  - system prompt = ours (plus only the known org-managed sentence, reported),
  - tools = only our telemetry tools (+ the SDK's structured-output tool), no built-in or user tools,
  - model id pinned, and the sampling/thinking parameters recorded so the API path can send the same.
Bodies are saved to runs/capture/ (local, git-ignored). The auth header is never logged or saved.

  uv run python -m evals.capture
"""

from __future__ import annotations

import asyncio
import http.client
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sao.agents import sdk, team
from sao.agents.prompts import DIAGNOSIS_SCHEMA, TASK
from sao.llm import guard

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runs" / "capture"
UPSTREAM = "api.anthropic.com"
captured: list[dict] = []
TLS = ssl.create_default_context(cafile=__import__("certifi").where())
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
       "proxy-authenticate", "host", "content-length"}


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _forward(self, method: str) -> None:
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        if body and "/messages" in self.path:
            try:
                captured.append({"path": self.path, "body": json.loads(body)})
            except json.JSONDecodeError:
                pass
        conn = http.client.HTTPSConnection(UPSTREAM, timeout=600, context=TLS)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        conn.request(method, self.path, body=body, headers=headers)
        resp = conn.getresponse()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while chunk := resp.read1(65536):
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        conn.close()

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")


def check(req: dict, expected_system: str, allowed_tools: set[str]) -> dict:
    b = req["body"]
    system = b.get("system")
    sys_text = "\n".join(x.get("text", "") for x in system) if isinstance(system, list) else (system or "")
    tools = [t.get("name") for t in b.get("tools", [])]
    unexpected_tools = [t for t in tools if t not in allowed_tools]
    extra_system = sys_text.replace(expected_system, "").strip()
    first_user = b["messages"][0]["content"] if b.get("messages") else ""
    user_text = first_user if isinstance(first_user, str) else " ".join(
        c.get("text", "") for c in first_user if isinstance(c, dict))
    return {"model": b.get("model"), "max_tokens": b.get("max_tokens"), "thinking": b.get("thinking"),
            "temperature": b.get("temperature"), "output_config": b.get("output_config"),
            "system_has_ours": expected_system.strip()[:200] in sys_text,
            "system_extra": extra_system[:1500], "tools": tools, "unexpected_tools": unexpected_tools,
            "first_user_message": user_text[:1500], "n_messages": len(b.get("messages", []))}


async def run() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    guard.CAPTURE_URL = f"http://127.0.0.1:{srv.server_address[1]}"
    snap = ROOT / "data/itbench-aa/snapshots/Scenario-24"
    OUT.mkdir(parents=True, exist_ok=True)
    report = {}
    cases = [
        ("A", TASK, None, "Investigate the incident and give the JSON diagnosis.", "sonnet", DIAGNOSIS_SCHEMA, 6),
        ("investigator", team.INVESTIGATOR.format(signal="changes", focus="What changed?"),
         team.SIGNAL_TOOLS["changes"], "Investigate and report.", "haiku", team.FINDINGS_SCHEMA, 4),
    ]
    for name, system, tools, prompt, model, schema, turns in cases:
        captured.clear()
        r = await sdk.run_agent(role=name, system=system, prompt=prompt, snapshot=snap, model=model, schema=schema,
                                tools=tools, max_turns=turns)
        allowed = {f"mcp__telemetry__{t}" for t in (tools or team.SIGNAL_TOOLS["changes"] + [
            "log_errors", "search_logs", "trace_summary", "trace_errors", "dependencies", "metric_anomalies",
            "metric_series", "list_metrics"])} | {"StructuredOutput"}
        msgs = [c for c in captured if c["body"].get("messages")]
        (OUT / f"{name}.json").write_text(json.dumps(msgs, indent=1))
        checks = [check(c, system, allowed) for c in msgs]
        report[name] = {"requests": len(msgs), "agent_turns": r["turns"], "first": checks[0] if checks else None,
                        "models": sorted({c["model"] for c in checks}),
                        "any_unexpected_tools": sorted({t for c in checks for t in c["unexpected_tools"]}),
                        "system_extra_variants": sorted({c["system_extra"] for c in checks})}
    srv.shutdown()
    guard.CAPTURE_URL = None
    (OUT / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    asyncio.run(run())
