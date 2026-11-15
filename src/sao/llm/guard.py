"""Guard for every Claude Agent SDK run: local CLI login in an isolated configuration, never an API key, usage guard.

Local development and evals run the Agent SDK on a Claude login (CLI token from `claude setup-token`); the deployed
version uses an API key. The guarantees and the setup match the CLI backend used in the other projects:

  1. isolated configuration: CLAUDE_CONFIG_DIR is an empty folder (no user plugins, hooks, settings or memory files),
     logged in with the token in ~/.config/claude-eval/oauth_token, run from an empty working folder;
  2. never an API key: API-key variables are removed from this process (the SDK merges its env over ours), and a run
     stops unless the CLI reports apiKeySource == "none";
  3. hooks: only KNOWN_HOOKS (forced by the account's organization-managed settings, which no client setting can turn
     off) with their exact output; anything else stops the run;
  4. usage guard: after each rate-limit report, at or above CLAUDE_EVAL_STOP_AT (default 80%) of the weekly window,
     or CLAUDE_EVAL_STOP_AT_5H (default: the same) of the 5-hour window, the next run raises QuotaStop; paid extra
     usage (isUsingOverage) stops at once.

API mode (SAO_LLM=api) exists for the deployed demo and the API parity check only: the key comes from the
environment (on AWS, from SSM at start-up), the configuration folder is a fresh empty one, and a run stops unless the
CLI reports apiKeySource == "ANTHROPIC_API_KEY". The usage guard does not apply there (the workspace has its own
spend cap). Evals never set it.

EvalStop derives from BaseException so task-level `except Exception` handlers can't swallow "stop the run".
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_EVAL_HOME", Path.home() / ".config" / "claude-eval"))
TOKEN, CONFIG = HOME / "oauth_token", HOME / "config"
STOP_AT = float(os.environ.get("CLAUDE_EVAL_STOP_AT", "0.80"))
STOP_AT_5H = float(os.environ.get("CLAUDE_EVAL_STOP_AT_5H", STOP_AT))
REMOVE = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
          "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")
KNOWN_HOOKS = {
    "SessionStart": '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"This session may '
                    'have run before. Call mcp__plugin_context-mode_context-mode__ctx_resume for a summary of what '
                    'happened."}}',
    "UserPromptSubmit": "",
    "PostToolUse": "",  # runs after each tool call; empty output = the tool result reaches the model unchanged
}

last_windows: dict | None = None
API_MODE = os.environ.get("SAO_LLM") == "api"
API_CONFIG = Path(os.environ.get("SAO_API_CONFIG_DIR", "/tmp/sao-claude-config"))


class EvalStop(BaseException):
    """Stop the whole run (not a per-task failure)."""


class QuotaStop(EvalStop):
    """Usage reached the stop threshold, or paid extra usage was used. `retry_at` (epoch seconds, or None) is when
    the window that tripped the stop resets; runners print it and exit 75 so a driver can wait and resume."""

    def __init__(self, msg: str, retry_at: float | None = None, final: bool = False):
        super().__init__(msg)
        self.retry_at = retry_at
        self.final = final  # paid extra usage: never resume automatically


class NotClean(EvalStop):
    """The run would not use the CLI login in an isolated configuration."""


def read_token() -> str:
    return "".join(TOKEN.read_text().split())


CAPTURE_URL: str | None = None  # set by evals/capture.py: a request-logging proxy on loopback, never anything else


def sdk_env() -> dict[str, str]:
    """Environment for the SDK's CLI subprocess. Also scrubs API-key variables from this process."""
    if API_MODE:
        return _api_env()
    for k in REMOVE:
        os.environ.pop(k, None)
    extra = {}
    if CAPTURE_URL:
        if not CAPTURE_URL.startswith("http://127.0.0.1:"):
            raise NotClean(f"capture proxy must be on loopback: {CAPTURE_URL}")
        extra["ANTHROPIC_BASE_URL"] = CAPTURE_URL
    if not (TOKEN.exists() and CONFIG.is_dir()):
        raise NotClean(f"isolated CLI setup missing ({TOKEN}, {CONFIG}); run `claude setup-token` and save it there")
    tok = read_token()
    if tok.startswith("sk-ant-api"):
        raise NotClean(f"{TOKEN} holds an API key, not a CLI login token")
    # The org-managed context-mode plugin replaces any tool result over 4 KB with a pointer to its own search tool
    # (which our agents don't have). Its own setting raises that limit, so tool results reach the model unchanged;
    # the guard still checks that every PostToolUse hook returns empty output.
    return {"CLAUDE_CONFIG_DIR": str(CONFIG), "CLAUDE_CODE_OAUTH_TOKEN": tok,
            "CTX_REPLACE_TRIGGER_BYTES": str(10**12), **extra}


def _api_env() -> dict[str, str]:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key.startswith("sk-ant-") or key.startswith("sk-ant-oat"):  # an API key, not a CLI login token
        raise NotClean("SAO_LLM=api needs ANTHROPIC_API_KEY")
    for k in REMOVE:
        if k != "ANTHROPIC_API_KEY":
            os.environ.pop(k, None)
    API_CONFIG.mkdir(parents=True, exist_ok=True)
    return {"ANTHROPIC_API_KEY": key, "CLAUDE_CONFIG_DIR": str(API_CONFIG), "HOME": str(API_CONFIG.parent),
            "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def _util(w: dict | None) -> float:
    return max((x.get("utilization") or 0.0 for x in (w or {}).values()), default=0.0)


def _epoch(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e12 else 1)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def limit(window: str) -> float:
    """Stop threshold for one window: the 5-hour window may have its own, every other window uses STOP_AT."""
    return STOP_AT_5H if window == "five_hour" else STOP_AT


def retry_at(threshold: float | None = None, reserve: float = 0.0) -> float | None:
    """When the windows at or above their threshold (minus `reserve`) reset (the latest of them)."""
    times = [_epoch(w.get("resetsAt") or w.get("resets_at")) for k, w in (last_windows or {}).items()
             if (w.get("utilization") or 0.0) >= (limit(k) if threshold is None else threshold) - reserve]
    times = [t for t in times if t]
    return max(times) if times else None


def before_run(reserve: float = 0.0) -> None:
    """Refuse to start new work at the stop threshold. `reserve` (a fraction of the window) is the measured cost
    of the work about to start: an episode starts only if it can finish below the threshold, so stops land between
    episodes instead of cutting one off."""
    if API_MODE:
        return
    over = {k: (w.get("utilization") or 0.0) for k, w in (last_windows or {}).items()
            if (w.get("utilization") or 0.0) + reserve >= limit(k)}
    if over:
        shown = ", ".join(f"{k} {u:.0%} (stop at {limit(k):.0%})" for k, u in over.items())
        raise QuotaStop(f"usage {shown} + reserve {reserve:.1%}: {last_windows}", retry_at=retry_at(reserve=reserve))


def check(msg) -> None:
    """Inspect one SDK message; raise EvalStop when a guarantee is broken."""
    global last_windows
    kind = type(msg).__name__
    if kind == "SystemMessage" and getattr(msg, "subtype", "") == "init":
        src = (msg.data or {}).get("apiKeySource")
        expected = "ANTHROPIC_API_KEY" if API_MODE else "none"
        if src != expected:
            raise NotClean(f"not using the CLI login: apiKeySource={src!r}")
    elif kind == "HookEventMessage" or (
            kind == "SystemMessage" and str(getattr(msg, "subtype", "")).startswith("hook")):
        data = getattr(msg, "data", {}) or {}
        event = (getattr(msg, "hook_event_name", None) or data.get("hook_event")
                 or str(data.get("hook_name", "")).split(":")[0])
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
            raise QuotaStop(f"the run used paid extra usage: {raw}", final=True)
        if str(getattr(info, "status", "")) in ("rejected", "RateLimitStatus.REJECTED"):
            raise QuotaStop(f"usage limit reached: {raw}", retry_at=retry_at(1.0) or retry_at(0.0))
