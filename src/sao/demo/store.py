"""Where the public demo keeps state: run documents, rate limits and per-run kill switches.

AWS (SAO_TABLE / SAO_RUNS_BUCKET set): DynamoDB for counters and kill switches, S3 for run documents, and the final
audit log copied to a bucket with Object Lock (SAO_AUDIT_BUCKET). Locally: a folder and in-memory counters, so the
whole demo runs on a laptop with the same code.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

TABLE = os.environ.get("SAO_TABLE")
RUNS_BUCKET = os.environ.get("SAO_RUNS_BUCKET")
AUDIT_BUCKET = os.environ.get("SAO_AUDIT_BUCKET")
LOCAL = Path(os.environ.get("SAO_DEMO_DIR", "/tmp/sao-demo"))
PER_HOUR = int(os.environ.get("SAO_RUNS_PER_HOUR", "6"))
PER_DAY = int(os.environ.get("SAO_RUNS_PER_DAY", "20"))

_clients: dict = {}


def _aws(name: str):
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name)
    return _clients[name]


# ---------- run documents ----------
def put_run(run_id: str, doc: dict) -> None:
    body = json.dumps(doc, default=str)
    if RUNS_BUCKET:
        _aws("s3").put_object(Bucket=RUNS_BUCKET, Key=f"runs/{run_id}.json", Body=body.encode(),
                              ContentType="application/json")
    else:
        LOCAL.mkdir(parents=True, exist_ok=True)
        tmp = LOCAL / f"{run_id}.json.tmp"
        tmp.write_text(body)
        tmp.replace(LOCAL / f"{run_id}.json")


def get_run(run_id: str) -> dict | None:
    if RUNS_BUCKET:
        try:
            return json.loads(_aws("s3").get_object(Bucket=RUNS_BUCKET, Key=f"runs/{run_id}.json")["Body"].read())
        except _aws("s3").exceptions.NoSuchKey:
            return None
    f = LOCAL / f"{run_id}.json"
    return json.loads(f.read_text()) if f.exists() else None


def lock_audit(run_id: str, audit_path: Path) -> str | None:
    """Copy the run's final audit log to the Object Lock bucket (retention set by the bucket default). Returns the
    object version id, or None locally."""
    if not AUDIT_BUCKET:
        return None
    r = _aws("s3").put_object(Bucket=AUDIT_BUCKET, Key=f"audit/{run_id}.jsonl", Body=audit_path.read_bytes(),
                              ContentType="application/x-ndjson", ChecksumAlgorithm="SHA256")
    return r.get("VersionId")


# ---------- rate limits ----------
_mem: dict[str, int] = {}
_mem_lock = threading.Lock()


def take_run_slot(now: float | None = None) -> tuple[bool, str]:
    """Count one live run against the hourly and daily limits. (allowed, reason)."""
    now = now or time.time()
    hour, day = time.strftime("%Y%m%d%H", time.gmtime(now)), time.strftime("%Y%m%d", time.gmtime(now))
    for key, cap, ttl in ((f"rate#h#{hour}", PER_HOUR, 7200), (f"rate#d#{day}", PER_DAY, 172800)):
        if TABLE:
            ddb = _aws("dynamodb")
            try:
                ddb.update_item(TableName=TABLE, Key={"pk": {"S": key}},
                                UpdateExpression="ADD n :one SET expires_at = if_not_exists(expires_at, :ttl)",
                                ConditionExpression="attribute_not_exists(n) OR n < :cap",
                                ExpressionAttributeValues={":one": {"N": "1"}, ":cap": {"N": str(cap)},
                                                           ":ttl": {"N": str(int(now) + ttl)}})
            except ddb.exceptions.ConditionalCheckFailedException:
                return False, "hourly" if key.startswith("rate#h") else "daily"
        else:
            with _mem_lock:
                if _mem.get(key, 0) >= cap:
                    return False, "hourly" if key.startswith("rate#h") else "daily"
                _mem[key] = _mem.get(key, 0) + 1
    return True, "ok"


# ---------- per-run kill switch ----------
def kill(run_id: str, by: str = "visitor") -> float:
    at = time.time()
    if TABLE:
        _aws("dynamodb").put_item(TableName=TABLE, Item={
            "pk": {"S": f"kill#{run_id}"}, "at": {"N": repr(at)}, "by": {"S": by},
            "expires_at": {"N": str(int(at) + 86400)}})
    else:
        LOCAL.mkdir(parents=True, exist_ok=True)
        (LOCAL / f"kill-{run_id}.json").write_text(json.dumps({"at": at, "by": by}))
    return at


def killed_at(run_id: str) -> float | None:
    if TABLE:
        it = _aws("dynamodb").get_item(TableName=TABLE, Key={"pk": {"S": f"kill#{run_id}"}},
                                       ConsistentRead=True).get("Item")
        return float(it["at"]["N"]) if it else None
    f = LOCAL / f"kill-{run_id}.json"
    return json.loads(f.read_text())["at"] if f.exists() else None


class RunKillSwitch:
    """The gateway's kill-switch interface (sao.gateway.killswitch.KillSwitch), scoped to one demo run: once the
    visitor presses the switch, every token of that run is revoked on its next request. Checked on every request
    (a consistent DynamoDB read, a few ms), so nothing outlives the revocation."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.first_check_after_kill: float | None = None

    def is_revoked(self, token_id: str, agents: list[str]) -> bool:
        at = killed_at(self.run_id)
        if at is not None and self.first_check_after_kill is None:
            self.first_check_after_kill = time.time()
        return at is not None

    def tool_disabled(self, tool_uid: str) -> bool:
        return False
