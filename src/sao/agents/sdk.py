"""One agent run on the Claude Agent SDK: a system prompt, a subset of the read-only telemetry tools, a turn cap and a
structured final answer. Every SDK message passes through the guard (CLI login only, known hooks only, usage stop).
Used by setup A (one agent) and by every member of the team (setups B and C).
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, query

from sao.llm import guard
from sao.telemetry.snapshot import TOOLS

MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
API_DEFAULT_EFFORT = {"claude-haiku-5-5": "medium", "claude-sonnet-5-5": "high", "claude-opus-5-5": "medium"}


GATEWAY = None  # a sao.gateway.local.GatewayHandle while an incident runs through the gateway (set by the runner)


def agent_id(role: str) -> str:
    """Agent identity at the gateway: investigator:<signal> → agent:investigator; the single agent of setup A is
    read-only like an investigator."""
    base = role.split(":")[0]
    return f"agent:{'investigator' if base in ('A', 'investigator') else base}"


def _gateway_notice() -> str:
    """Behind a screening gateway, agents are told how untrusted telemetry is marked."""
    if GATEWAY is not None and getattr(GATEWAY, "screening", False):
        from sao.gateway.screening import AGENT_NOTICE
        return "\n\n" + AGENT_NOTICE
    return ""


def telemetry_server(snapshot: Path, role: str = "A") -> dict:
    if GATEWAY is not None:
        return GATEWAY.mcp("telemetry", agent=agent_id(role))
    return {"type": "stdio", "command": sys.executable, "args": ["-m", "sao.mcp.telemetry_server"],
            "env": {"SAO_SNAPSHOT": str(snapshot)}}


async def run_agent(*, role: str, system: str, prompt: str, snapshot: Path, model: str, schema: dict,
                    tools: list[str] | None = None, max_turns: int = 40, effort: str | None = None) -> dict:
    """Run one agent to its structured answer. `tools`: telemetry tool names it may call (None = all, [] = none).
    Returns {"output", "is_error", "turns", "tool_calls", tokens, cost, wall_s, model, role}."""
    guard.before_run()
    model_id = MODELS.get(model, model)
    allowed = TOOLS if tools is None else tools
    blocked = [f"mcp__telemetry__{t}" for t in TOOLS if t not in allowed]
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"sao-{role}-") as cwd:
        opts = ClaudeAgentOptions(
            system_prompt=system + _gateway_notice(), model=model_id, effort=effort or API_DEFAULT_EFFORT.get(model_id),
            tools=[], mcp_servers={"telemetry": telemetry_server(snapshot, role)} if allowed else {},
            strict_mcp_config=True, allowed_tools=[f"mcp__telemetry__{t}" for t in allowed],
            disallowed_tools=blocked, setting_sources=[], max_turns=max_turns, cwd=cwd, env=guard.sdk_env(),
            include_hook_events=True, output_format={"type": "json_schema", "schema": schema})
        calls, result = [], None
        try:
            async for msg in query(prompt=prompt, options=opts):
                guard.check(msg)
                kind = type(msg).__name__
                if kind == "AssistantMessage":
                    for b in msg.content:
                        if type(b).__name__ == "ToolUseBlock":
                            calls.append({"tool": b.name.split("__")[-1], "input": b.input})
                elif kind == "ResultMessage":
                    result = msg
        except ClaudeSDKError as e:
            # The SDK raises when the agent hits its turn cap; that is a failed answer, not a crash of the run.
            if result is None or "maximum number of turns" not in str(e):
                raise
    if result is None:
        raise RuntimeError(f"{role}: the agent ended without a result")
    usage = result.usage or {}
    return {"role": role, "output": (result.structured_output or {}) if not result.is_error else {},
            "is_error": result.is_error, "stop": result.subtype, "turns": result.num_turns, "tool_calls": calls,
            "cost_usd_api_equivalent": result.total_cost_usd or 0.0,
            "input_tokens": sum(usage.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                                  "cache_read_input_tokens")),
            "output_tokens": usage.get("output_tokens", 0), "wall_s": round(time.monotonic() - t0, 1),
            "model": model_id}
