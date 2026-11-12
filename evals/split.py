"""Fix the ITBench-AA dev/test split once, before any prompt work (D3).

10 development scenarios (iterate freely) and 30 test scenarios (scored once). Stratified by fault family so both
halves contain feature-flag faults, chaos-mesh faults and Kubernetes configuration faults in the same proportions.
Deterministic (fixed seed); the result is committed as evals/split.json and never regenerated after scoring starts.

Run:  uv run python evals/split.py
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "itbench-aa" / "sre" / "data.jsonl"
OUT = ROOT / "evals" / "split.json"
SEED = 20261111
N_DEV = 10

CHAOS = ("chaos", "network_partition", "memory_stress", "jvm_heap_stress")


def family(gt: dict) -> str:
    """flagd (OTel demo feature flags), chaos (chaos-mesh experiments) or k8s (config/manifest changes)."""
    spec = gt.get("spec", gt)  # some scenarios wrap the ground truth in a Kubernetes-style object
    mechs = {f.get("fault_mechanism", "") for f in spec.get("fault", [])}
    if any("flagd" in m for m in mechs):
        return "flagd"
    if any(m in CHAOS for m in mechs):
        return "chaos"
    return "k8s"


def main() -> None:
    rows = [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]
    by_family: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        by_family[family(yaml.safe_load(r["ground_truth_yaml"]))].append(r["scenario_id"])

    rng = random.Random(SEED)
    total = len(rows)
    # largest-remainder allocation of the 10 dev slots across families
    quota = {f: N_DEV * len(ids) / total for f, ids in by_family.items()}
    alloc = {f: int(q) for f, q in quota.items()}
    for f in sorted(quota, key=lambda f: quota[f] - alloc[f], reverse=True)[: N_DEV - sum(alloc.values())]:
        alloc[f] += 1

    dev, test = [], []
    for f in sorted(by_family):
        ids = sorted(by_family[f], key=lambda s: int(s.split("-")[1]))
        rng.shuffle(ids)
        dev += ids[: alloc[f]]
        test += ids[alloc[f]:]

    key = lambda s: int(s.split("-")[1])  # noqa: E731
    out = {
        "seed": SEED,
        "families": {f: sorted(ids, key=key) for f, ids in sorted(by_family.items())},
        "dev": sorted(dev, key=key),
        "test": sorted(test, key=key),
    }
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    for f, ids in sorted(by_family.items()):
        print(f"{f:6} {len(ids):2} scenarios -> dev {sum(i in dev for i in ids)}, test {sum(i in test for i in ids)}")
    print(f"dev  ({len(dev)}): {out['dev']}")
    print(f"test ({len(test)}): {len(out['test'])} scenarios -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
