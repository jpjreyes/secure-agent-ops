"""Paired difference between two setups on the same scenarios, with a bootstrap 95% interval over scenarios.

  uv run python -m evals.paired_ci A-sonnet-t1 C-t1 --split test      # exit 0: interval excludes zero, 1: includes

Per scenario: mean score over its repeats in each setup; the difference is setup2 - setup1. The interval resamples
scenarios (the unit the benchmark generalises over), 10,000 times, fixed seed.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs" / "itbench"


def per_scenario(name: str, ids: list[str]) -> dict[str, float]:
    out = {}
    for sid in ids:
        reps = [json.loads(f.read_text())["score"] for f in sorted((RUNS / name / sid).glob("rep*.json"))]
        if reps:
            out[sid] = mean(reps)
    return out


def bootstrap(xs: list[float], n: int = 10_000, seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(mean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("first")
    ap.add_argument("second")
    ap.add_argument("--split", default="test", choices=["dev", "test"])
    a = ap.parse_args()
    ids = json.loads((ROOT / "evals/split.json").read_text())[a.split]
    s1, s2 = per_scenario(a.first, ids), per_scenario(a.second, ids)
    common = [s for s in ids if s in s1 and s in s2]
    d = [s2[s] - s1[s] for s in common]
    lo, hi = bootstrap(d)
    lo1, hi1 = bootstrap([s1[s] for s in common])
    lo2, hi2 = bootstrap([s2[s] for s in common])
    print(f"{len(common)} scenarios")
    print(f"{a.first}: {mean(s1[s] for s in common):.3f} [{lo1:.3f}, {hi1:.3f}]")
    print(f"{a.second}: {mean(s2[s] for s in common):.3f} [{lo2:.3f}, {hi2:.3f}]")
    print(f"difference ({a.second} - {a.first}): {mean(d):+.3f} [{lo:+.3f}, {hi:+.3f}]"
          f"  better/same/worse scenarios: {sum(x > 0 for x in d)}/{sum(x == 0 for x in d)}/{sum(x < 0 for x in d)}")
    return 1 if lo <= 0 <= hi else 0


if __name__ == "__main__":
    sys.exit(main())
