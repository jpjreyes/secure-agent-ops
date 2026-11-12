"""Per-agent rate limit and tool-output size cap.

- Rate: a token bucket per agent identity (calls per minute, with a burst). A runaway or hijacked agent looping on
  tools is slowed, and every refusal is audited. Defaults sized from measured use: a team episode makes ~70 tool
  calls in ~90 s across 6 agents, a single investigator ~25 in ~60 s.
- Size: tool results longer than MAX_RESULT_CHARS are cut, with a notice telling the agent to narrow the query (the
  telemetry tools take filters and limits). Protects the model's context and caps cost per call.
"""

from __future__ import annotations

import threading
import time

RATE_PER_MIN = 120
BURST = 40
MAX_RESULT_CHARS = 20_000


class RateLimiter:
    def __init__(self, per_min: float = RATE_PER_MIN, burst: int = BURST):
        self.rate, self.burst = per_min / 60.0, burst
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            ok = tokens >= 1.0
            self._buckets[key] = (tokens - 1.0 if ok else tokens, now)
            return ok


def cap(text: str, limit: int = MAX_RESULT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return (text[:limit] + f"\n… [cut by the gateway at {limit} characters of {len(text)}; narrow the query with "
            f"filters or a smaller limit]"), True
