"""Kill switch: stop an agent, a single token or a tool everywhere, effective on the next call.

The gateway consults it on every request (token verification and every tool call), so there is no session to
outlive a revocation. Local backend: one JSON file shared by every gateway process (written atomically; readers
re-read it only when its mtime changes, so checking costs a stat call). On AWS the same interface is backed by a
DynamoDB table (M7). Each entry records who revoked what, when and why; the gateway also writes the denial to the
audit log.

  uv run python -m sao.gateway.killswitch revoke agent:executor --reason "acting on injected text"
  uv run python -m sao.gateway.killswitch disable-tool cluster/scale_deployment --reason "under review"
  uv run python -m sao.gateway.killswitch list | clear
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT = Path(os.environ.get("SAO_KILLSWITCH", ROOT / "runs" / "gateway" / "killswitch.json"))


class KillSwitch:
    def __init__(self, path: str | Path = DEFAULT):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._state = {"agents": {}, "tokens": {}, "tools": {}}

    # ---------- reading (hot path) ----------
    def _load(self) -> dict:
        try:
            m = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._mtime, self._state = None, {"agents": {}, "tokens": {}, "tools": {}}
            return self._state
        if m != self._mtime:
            with self._lock:
                try:
                    self._state = json.loads(self.path.read_text())
                    self._mtime = m
                except (json.JSONDecodeError, OSError):
                    pass  # a write in progress: keep the last good state, re-read next call
        return self._state

    def is_revoked(self, token_id: str, agents: list[str]) -> bool:
        s = self._load()
        return token_id in s["tokens"] or any(a in s["agents"] for a in agents)

    def tool_disabled(self, tool_uid: str) -> bool:
        return tool_uid in self._load()["tools"]

    # ---------- writing ----------
    def _write(self, update) -> dict:
        with self._lock:
            state = json.loads(self.path.read_text()) if self.path.exists() else {"agents": {}, "tokens": {},
                                                                                  "tools": {}}
            update(state)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1))
            os.replace(tmp, self.path)
            return state

    def _entry(self, by: str, reason: str) -> dict:
        return {"at": time.time(), "by": by, "reason": reason}

    def revoke_agent(self, agent: str, by: str = "operator", reason: str = "") -> dict:
        return self._write(lambda s: s["agents"].__setitem__(agent, self._entry(by, reason)))

    def revoke_token(self, token_id: str, by: str = "operator", reason: str = "") -> dict:
        return self._write(lambda s: s["tokens"].__setitem__(token_id, self._entry(by, reason)))

    def disable_tool(self, tool_uid: str, by: str = "operator", reason: str = "") -> dict:
        return self._write(lambda s: s["tools"].__setitem__(tool_uid, self._entry(by, reason)))

    def clear(self) -> dict:
        return self._write(lambda s: s.update({"agents": {}, "tokens": {}, "tools": {}}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["revoke", "revoke-token", "disable-tool", "list", "clear"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--reason", default="")
    ap.add_argument("--by", default=os.environ.get("USER", "operator"))
    a = ap.parse_args()
    ks = KillSwitch()
    if a.cmd == "revoke":
        ks.revoke_agent(a.target, a.by, a.reason)
    elif a.cmd == "revoke-token":
        ks.revoke_token(a.target, a.by, a.reason)
    elif a.cmd == "disable-tool":
        ks.disable_tool(a.target, a.by, a.reason)
    elif a.cmd == "clear":
        ks.clear()
    print(json.dumps(ks._load(), indent=1))


if __name__ == "__main__":
    main()
