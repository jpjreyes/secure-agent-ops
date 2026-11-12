"""Tamper-evident audit log: one JSON line per gateway decision, each carrying the hash of the previous line.

Changing, deleting or reordering any past record breaks the chain from that point (verify() says where). Writes are
append + fsync. On AWS the file is shipped to S3 with Object Lock (M7), so even the gateway can't rewrite history;
locally the chain makes edits detectable.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

GENESIS = "0" * 64


def _digest(prev: str, body: dict) -> str:
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + canon).encode()).hexdigest()


def call_hash(server: str, tool: str, args: dict) -> str:
    """Stable id of one exact action (used by approvals and in audit records)."""
    canon = json.dumps({"server": server, "tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq, self._prev = 0, GENESIS
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    break
                self._seq, self._prev = r["seq"], r["hash"]

    def append(self, **fields) -> dict:
        with self._lock:
            body = {"seq": self._seq + 1, "ts": time.time(), "prev": self._prev, **fields}
            rec = {**body, "hash": _digest(self._prev, body)}
            with self.path.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq, self._prev = rec["seq"], rec["hash"]
            return rec


def verify(path: str | Path) -> tuple[bool, str]:
    """Walk the chain; (True, 'n records ok') or (False, where it breaks)."""
    prev, n = GENESIS, 0
    for i, line in enumerate(Path(path).read_text().splitlines(), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return False, f"line {i}: not JSON"
        body = {k: v for k, v in rec.items() if k != "hash"}
        if rec.get("prev") != prev:
            return False, f"line {i} (seq {rec.get('seq')}): previous-hash mismatch (record removed or reordered)"
        if _digest(prev, body) != rec.get("hash"):
            return False, f"line {i} (seq {rec.get('seq')}): content changed"
        prev, n = rec["hash"], n + 1
    return True, f"{n} records ok"
