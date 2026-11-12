"""Setup A: one agent with every read-only telemetry tool (the baseline the team must beat).

Runs on the Claude Agent SDK: the model, its tools (our telemetry MCP server only; no built-in tools), a turn cap and
the structured diagnosis as the final answer. Every SDK message passes through the guard (CLI login only, known hooks
only, usage stop).
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from sao.agents.prompts import DIAGNOSIS_SCHEMA, TASK
from sao.llm import guard

MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
API_DEFAULT_EFFORT = {"claude-haiku-5-5": "medium", "claude-sonnet-5-5": "high", "claude-opus-5-5": "medium"}


def telemetry_server(snapshot: Path) -> dict:
    return {"type": "stdio", "command": sys.executable, "args": ["-m", "sao.mcp.telemetry_server"],
            "env": {"SAO_SNAPSHOT": str(snapshot)}}


async def diagnose(snapshot: Path, model: str = "sonnet", effort: str | None = None, max_turns: int = 100) -> dict:
    """Investigate one incident snapshot; return the diagnosis plus turns, tool calls, tokens and cost."""
    guard.before_run()
    model_id = MODELS.get(model, model)
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="sao-agent-") as cwd:
        opts = ClaudeAgentOptions(
            system_prompt=TASK, model=model_id, effort=effort or API_DEFAULT_EFFORT.get(model_id),
            tools=[], mcp_servers={"telemetry": telemetry_server(snapshot)}, strict_mcp_config=True,
            allowed_tools=["mcp__telemetry__*"], setting_sources=[], max_turns=max_turns, cwd=cwd,
            env=guard.sdk_env(), include_hook_events=True,
            output_format={"type": "json_schema", "schema": DIAGNOSIS_SCHEMA})
        calls, result = [], None
        async for msg in query(prompt="Investigate the incident and give the JSON diagnosis.", options=opts):
            guard.check(msg)
            kind = type(msg).__name__
            if kind == "AssistantMessage":
                for b in msg.content:
                    if type(b).__name__ == "ToolUseBlock":
                        calls.append({"tool": b.name.split("__")[-1], "input": b.input})
            elif kind == "ResultMessage":
                result = msg
    if result is None:
        raise RuntimeError("the agent ended without a result")
    factors = (result.structured_output or {}).get("contributing_factors", []) if not result.is_error else []
    usage = result.usage or {}
    return {"factors": factors, "is_error": result.is_error, "stop": result.subtype, "turns": result.num_turns,
            "tool_calls": calls, "cost_usd_api_equivalent": result.total_cost_usd,
            "input_tokens": sum(usage.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                                  "cache_read_input_tokens")),
            "output_tokens": usage.get("output_tokens", 0), "wall_s": round(time.monotonic() - t0, 1),
            "model": model_id, "usage_windows": guard.last_windows}
