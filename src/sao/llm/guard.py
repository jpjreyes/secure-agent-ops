"""Guard for every Claude Agent SDK run: local CLI login in an isolated configuration, never an API key, usage guard.

Local development and evals run the Agent SDK on a Claude login (CLI token from `claude setup-token`); the deployed
version uses an API key. The guarantees and the setup match the CLI backend used in the other projects:

  1. isolated configuration: CLAUDE_CONFIG_DIR is an empty folder (no user plugins, hooks, settings or memory files),
     logged in with the token in ~/.config/claude-eval/oauth_token, run from an empty working folder;
  2. never an API key: API-key variables are removed from this process (the SDK merges its env over ours), and a run
     stops unless the CLI reports apiKeySource == "none";
  3. hooks: only KNOWN_HOOKS (forced by the account's organization-managed settings, which no client setting can turn
     off) with their exact output; anything else stops the run;
  4. usage guard: after each rate-limit report, at or above CLAUDE_EVAL_STOP_AT (default 80%) of the 5-hour or the
     weekly window, the next run raises QuotaStop; paid extra usage (isUsingOverage) stops at once.

EvalStop derives from BaseException so task-level `except Exception` handlers can't swallow "stop the run".
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_EVAL_HOME", Path.home() / ".config" / "claude-eval"))
TOKEN, CONFIG = HOME / "oauth_token", HOME / "config"
STOP_AT = float(os.environ.get("CLAUDE_EVAL_STOP_AT", "0.80"))
REMOVE = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
          "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")
KNOWN_HOOKS = {
    "SessionStart": '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"This session may have '
                    'run before. Call mcp__plugin_context-mode_context-mode__ctx_resume for a summary of what happened."}}',
    "UserPromptSubmit": "",
}

last_windows: dict | None = None


class EvalStop(BaseException):
    """Stop the whole run (not a per-task failure)."""


class QuotaStop(EvalStop):
    """Usage reached the stop threshold, or paid extra usage was used."""


class NotClean(EvalStop):
    """The run would not use the CLI login in an isolated configuration."""


def read_token() -> str:
    return "".join(TOKEN.read_text().split())


def sdk_env() -> dict[str, str]:
    """Environment for the SDK's CLI subprocess. Also scrubs API-key variables from this process."""
    for k in REMOVE:
        os.environ.pop(k, None)
    if not (TOKEN.exists() and CONFIG.is_dir()):
        raise NotClean(f"isolated CLI setup missing ({TOKEN}, {CONFIG}); run `claude setup-token` and save it there")
    tok = read_token()
    if tok.startswith("sk-ant-api"):
        raise NotClean(f"{TOKEN} holds an API key, not a CLI login token")
    return {"CLAUDE_CONFIG_DIR": str(CONFIG), "CLAUDE_CODE_OAUTH_TOKEN": tok}


def _util(w: dict | None) -> float:
    return max((x.get("utilization") or 0.0 for x in (w or {}).values()), default=0.0)


def before_run() -> None:
    if _util(last_windows) >= STOP_AT:
        raise QuotaStop(f"usage at {_util(last_windows):.0%} (stop at {STOP_AT:.0%}): {last_windows}")


def check(msg) -> None:
    """Inspect one SDK message; raise EvalStop when a guarantee is broken."""
    global last_windows
    kind = type(msg).__name__
    if kind == "SystemMessage" and getattr(msg, "subtype", "") == "init":
        src = (msg.data or {}).get("apiKeySource")
        if src != "none":
            raise NotClean(f"not using the CLI login: apiKeySource={src!r}")
    elif kind == "HookEventMessage" or (kind == "SystemMessage" and str(getattr(msg, "subtype", "")).startswith("hook")):
        data = getattr(msg, "data", {}) or {}
        event = getattr(msg, "hook_event_name", None) or data.get("hook_event") or str(data.get("hook_name", "")).split(":")[0]
        sub = getattr(msg, "subtype", "") or data.get("subtype", "")
        if event not in KNOWN_HOOKS:
            raise NotClean(f"unexpected hook {event!r}")
        if "response" in str(sub) and str(data.get("output") or "").strip() != KNOWN_HOOKS[event]:
            raise NotClean(f"hook {event!r} injected unexpected text: {str(data.get('output'))[:120]!r}")
    elif kind == "RateLimitEvent":
        info = msg.rate_limit_info
        raw = getattr(info, "raw", None) or {}
        last_windows = raw.get("unifiedWindows") or last_windows
        if raw.get("isUsingOverage"):
            raise QuotaStop(f"the run used paid extra usage: {raw}")
        if str(getattr(info, "status", "")) in ("rejected", "RateLimitStatus.REJECTED"):
            raise QuotaStop(f"usage limit reached: {raw}")
