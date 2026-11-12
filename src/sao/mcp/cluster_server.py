"""Cluster actions MCP server: the executor's only write tools (everything else in the system is read-only).

Each tool is annotated destructive (the gateway's Cedar policy then requires a human approval), takes the target's
namespace and name explicitly (policies check them), and an idempotency key: a key that already ran returns the
recorded result instead of acting again, so a crashed or resumed episode never applies an action twice.

SAO_DRY_RUN=1 sends every change with `--dry-run=server`: the API server validates it fully (admission, schema)
without applying it. Results and keys are recorded in runs/actions/ledger.jsonl (append-only).

Run:  uv run python -m sao.mcp.cluster_server   (stdio)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from sao import kube

ROOT = Path(__file__).resolve().parents[3]
LEDGER = Path(os.environ.get("SAO_ACTION_LEDGER", ROOT / "runs" / "actions" / "ledger.jsonl"))
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
FLAG_CM, FLAG_KEY = "flagd-config", "demo.flagd.json"

server = MCPServer("cluster", instructions="Remediation actions on the application's Kubernetes namespace. Every "
                   "call needs an idempotency_key unique to the intended action, and a human approval.")


def _dry() -> list[str]:
    return ["--dry-run=server"] if os.environ.get("SAO_DRY_RUN") == "1" else []


def _restart(namespace: str, name: str) -> str:
    """What `kubectl rollout restart` does (bump the restartedAt annotation), as a patch so dry-run works."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    patch = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": ts}}}}}
    return kube.kubectl("patch", "deployment", name, "-n", namespace, "-p", json.dumps(patch), *_dry())


def _ledger() -> dict[str, dict]:
    out = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line cut by a crash
            if r.get("ok"):  # only a successful action consumes its key; a failure can be retried
                out[r["idempotency_key"]] = r
    return out


def _run(key: str, tool: str, args: dict, action) -> str:
    if not key or len(key) < 8:
        return "refused: idempotency_key must be at least 8 characters, unique to this action"
    prev = _ledger().get(key)
    if prev:
        if prev["tool"] != tool or prev["args"] != args:
            return f"refused: idempotency_key {key} was already used for a different action ({prev['tool']})"
        return f"already done (idempotency_key {key}): {prev['result']}"
    try:
        result = action()
        ok = True
    except kube.KubeError as e:
        result, ok = f"failed: {e}", False
    rec = {"ts": time.time(), "idempotency_key": key, "tool": tool, "args": args, "ok": ok,
           "dry_run": bool(_dry()), "result": result.strip()[:2000]}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return ("[dry run] " if rec["dry_run"] else "") + rec["result"]


@server.tool(annotations=WRITE)
def rollback_deployment(namespace: str, name: str, idempotency_key: str) -> str:
    """Roll a Deployment back to its previous revision (undo the last rollout: config, image, env, limits)."""
    return _run(idempotency_key, "rollback_deployment", {"namespace": namespace, "name": name},
                lambda: kube.kubectl("rollout", "undo", f"deployment/{name}", "-n", namespace, *_dry()))


@server.tool(annotations=WRITE)
def restart_deployment(namespace: str, name: str, idempotency_key: str) -> str:
    """Restart a Deployment's pods (rolling restart; the spec does not change)."""
    return _run(idempotency_key, "restart_deployment", {"namespace": namespace, "name": name},
                lambda: _restart(namespace, name))


@server.tool(annotations=WRITE)
def scale_deployment(namespace: str, name: str, replicas: int, idempotency_key: str) -> str:
    """Set a Deployment's replica count."""
    return _run(idempotency_key, "scale_deployment", {"namespace": namespace, "name": name, "replicas": replicas},
                lambda: kube.kubectl("scale", f"deployment/{name}", f"--replicas={replicas}", "-n", namespace,
                                     *_dry()))


@server.tool(annotations=WRITE)
def set_flag(namespace: str, flag: str, variant: str, idempotency_key: str) -> str:
    """Set a feature flag's default variant in the flagd-config ConfigMap, then restart flagd to load it."""
    def act() -> str:
        cm = kube.get("configmap", FLAG_CM, namespace=namespace)
        doc = json.loads(cm["data"][FLAG_KEY])
        if flag not in doc["flags"]:
            raise kube.KubeError(f"unknown flag {flag}")
        if variant not in doc["flags"][flag]["variants"]:
            raise kube.KubeError(f"flag {flag} has no variant {variant} (has {list(doc['flags'][flag]['variants'])})")
        doc["flags"][flag]["defaultVariant"] = variant
        out = kube.kubectl("patch", "configmap", FLAG_CM, "-n", namespace, "--type", "merge", "-p",
                           json.dumps({"data": {FLAG_KEY: json.dumps(doc, indent=2)}}), *_dry())
        return out + _restart(namespace, "flagd")
    return _run(idempotency_key, "set_flag", {"namespace": namespace, "flag": flag, "variant": variant}, act)


@server.tool(annotations=WRITE)
def delete_networkpolicy(namespace: str, name: str, idempotency_key: str) -> str:
    """Delete a NetworkPolicy."""
    return _run(idempotency_key, "delete_networkpolicy", {"namespace": namespace, "name": name},
                lambda: kube.kubectl("delete", "networkpolicy", name, "-n", namespace, *_dry()))


if __name__ == "__main__":
    server.run("stdio")
