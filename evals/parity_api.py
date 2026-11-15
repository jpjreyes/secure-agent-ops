"""API parity check (evals/PLAN.md "API parity check"): the deployed worker on 5 dev scenarios, no attack.

  uv run python -m evals.parity_api            # needs AWS credentials for the demo account; results in runs/parity_api/
Invokes the worker Lambda directly (clean mode is not reachable from the public API), polls the run documents in S3,
scores them with evals/score.py and checks each run's audit chain with the gateway's own verifier.
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from pathlib import Path

import boto3

from evals.run_itbench import _truth
from evals.score import score
from sao.gateway import audit as audit_mod

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runs" / "parity_api"
REGION, WORKER, BUCKET = "us-west-2", "sao-demo-worker", "sao-demo-runs-054896499172"
SUBSCRIPTION = {"Scenario-1": 0.0, "Scenario-15": 1.0, "Scenario-24": 1.0, "Scenario-33": 0.5, "Scenario-80": 1.0}


def main() -> None:
    lam, s3 = boto3.client("lambda", region_name=REGION), boto3.client("s3", region_name=REGION)
    OUT.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:6]
    runs = {sid: f"parity-{tag}-{sid.split('-')[1]}" for sid in SUBSCRIPTION}
    for sid, rid in runs.items():
        lam.invoke(FunctionName=WORKER, InvocationType="Event",
                   Payload=json.dumps({"run_id": rid, "incident": sid, "clean": True}).encode())
    print(f"started {len(runs)} runs ({tag})")
    docs, t0 = {}, time.time()
    while len(docs) < len(runs) and time.time() - t0 < 900:
        time.sleep(10)
        for sid, rid in runs.items():
            if sid in docs:
                continue
            try:
                d = json.loads(s3.get_object(Bucket=BUCKET, Key=f"runs/{rid}.json")["Body"].read())
            except s3.exceptions.NoSuchKey:
                continue
            if d.get("status") in ("done", "error", "killed"):
                docs[sid] = d
                print(f"{len(docs)}/{len(runs)} {sid} {d['status']} ({time.time() - t0:.0f} s)")
    rows = []
    for sid, d in sorted(docs.items()):
        sc = score(_truth(sid), (d.get("result") or {}).get("factors") or [])
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.jsonl"
            f.write_text("".join(json.dumps(r) + "\n" for r in d.get("audit") or []))
            chain_ok, chain_msg = audit_mod.verify(f)
        rows.append({"scenario": sid, "run_id": d["id"], "status": d["status"], "error": d.get("error"),
                     "score": sc.score, "subscription": SUBSCRIPTION[sid], "cost": (d.get("result") or {}).get("cost"),
                     "wall_s": round(d.get("finished", 0) - d.get("started", 0), 1), "audit_ok": chain_ok,
                     "audit": chain_msg, "audit_locked_version": d.get("audit_locked_version")})
        (OUT / f"{d['id']}.json").write_text(json.dumps(d, indent=1, default=str))
    mean = sum(r["score"] for r in rows) / max(len(rows), 1)
    summary = {"tag": tag, "rows": rows, "deployed_mean": round(mean, 3), "subscription_mean": 0.70,
               "cost": round(sum(r["cost"] or 0 for r in rows), 4), "finished": len(rows), "of": len(runs)}
    (OUT / f"suite-{tag}.json").write_text(json.dumps(summary, indent=1))
    for r in rows:
        print(f"{r['scenario']:12} deployed {r['score']:.2f}  subscription {r['subscription']:.2f}  "
              f"${r['cost'] or 0:.3f}  {r['wall_s']} s  audit {r['audit']}")
    print(f"mean deployed {mean:.3f} vs subscription 0.70 · cost ${summary['cost']}")


if __name__ == "__main__":
    main()
