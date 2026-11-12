"""Fault CLI for the live cluster.

  uv run python -m sao.faults list
  uv run python -m sao.faults inject flag-payment-failure
  uv run python -m sao.faults status
  uv run python -m sao.faults revert          # reverts whatever is active (safe to run any time)
"""

from __future__ import annotations

import sys

from sao.faults import control
from sao.faults.catalog import FAULTS


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    if cmd == "list":
        for f in FAULTS.values():
            print(f"{f.id:36} {f.family:7} root={f.root[0]}/{f.root[1]:22} {f.description}")
    elif cmd == "inject":
        rec = control.inject(argv[1])
        print(f"injected {rec['id']}")
    elif cmd == "status":
        print("active:", list(control.active()) or "none")
        ok, bad = control.healthy()
        print("pods healthy" if ok else f"pods not ready: {bad}")
    elif cmd == "revert":
        print("reverted:", control.revert_all() or "nothing active")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
