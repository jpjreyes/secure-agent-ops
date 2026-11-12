"""Compare setups on ITBench-AA, paired by scenario.

  uv run python -m evals.compare A-sonnet C-dev1 [B-dev1 …]

Per setup: mean score, root cause found (recall), mean false positives, API-equivalent cost and wall time per run.
Per scenario: the score of each setup (mean over repeats), and for setup C the draft's score (before the verifier).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean

RUNS = Path(__file__).resolve().parents[1] / "runs" / "itbench"


def load(name: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in sorted((RUNS / name).glob("*/rep*.json")):
        r = json.loads(f.read_text())
        out.setdefault(r["scenario"], []).append(r)
    return out


def main(names: list[str]) -> None:
    data = {n: load(n) for n in names}
    common = sorted(set.intersection(*(set(d) for d in data.values())), key=lambda s: int(s.split("-")[1]))
    print(f"{len(common)} scenarios in common\n")
    print("| setup | runs | mean score | root cause found | mean FP | $/run API-eq | s/run |")
    print("|---|---|---|---|---|---|---|")
    for n, d in data.items():
        rs = [r for s in common for r in d[s]]
        print(f"| {n} | {len(rs)} | {mean(r['score'] for r in rs):.3f} | "
              f"{sum(r['recall_ok'] for r in rs)}/{len(rs)} | {mean(r['fp'] for r in rs):.2f} | "
              f"{mean(r['cost_usd_api_equivalent'] or 0 for r in rs):.3f} | {mean(r['wall_s'] for r in rs):.0f} |")
    print("\n| scenario | " + " | ".join(names) + " |")
    print("|---|" + "---|" * len(names))
    for s in common:
        cells = []
        for n in names:
            rs = data[n][s]
            c = f"{mean(r['score'] for r in rs):.2f}"
            if "draft_score" in rs[0]:
                c += f" (draft {mean(r['draft_score'] for r in rs):.2f})"
            cells.append(c)
        print(f"| {s} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main(sys.argv[1:])
