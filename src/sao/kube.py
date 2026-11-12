"""Small kubectl wrapper for the local kind cluster (context kind-sao).

kubectl is used instead of a client library: it is already required for the cluster, it reads the same kubeconfig,
and its JSON output is what the telemetry recorder stores.
"""

from __future__ import annotations

import json
import os
import subprocess

CONTEXT = os.environ.get("SAO_KUBE_CONTEXT", "kind-sao")
NAMESPACE = "otel-demo"


class KubeError(RuntimeError):
    pass


def kubectl(*args: str, stdin: str | None = None, timeout: int = 60, check: bool = True) -> str:
    cmd = ["kubectl", "--context", CONTEXT, *args]
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise KubeError(f"{' '.join(args[:4])}…: {p.stderr.strip()[:500]}")
    return p.stdout


def get(kind: str, name: str | None = None, namespace: str | None = NAMESPACE, selector: str | None = None) -> dict:
    args = ["get", kind] + ([name] if name else []) + ["-o", "json"]
    args += ["-n", namespace] if namespace else ["-A"]
    if selector:
        args += ["-l", selector]
    return json.loads(kubectl(*args))


def exists(kind: str, name: str, namespace: str = NAMESPACE) -> bool:
    return bool(kubectl("get", kind, name, "-n", namespace, "--ignore-not-found", "-o", "name").strip())


def apply(obj: dict) -> None:
    kubectl("apply", "-f", "-", stdin=json.dumps(obj))


def replace(obj: dict) -> None:
    """Put an object back exactly as saved (resourceVersion dropped so the server accepts it)."""
    obj = json.loads(json.dumps(obj))
    meta = obj.get("metadata", {})
    for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields", "generation"):
        meta.pop(k, None)
    obj.pop("status", None)
    kubectl("replace", "-f", "-", stdin=json.dumps(obj))


def patch(kind: str, name: str, patch_obj: dict | list, ptype: str = "strategic",
          namespace: str = NAMESPACE) -> None:
    kubectl("patch", kind, name, "-n", namespace, "--type", ptype, "-p", json.dumps(patch_obj))


def delete(kind: str, name: str, namespace: str = NAMESPACE) -> None:
    kubectl("delete", kind, name, "-n", namespace, "--ignore-not-found", "--wait=false")


def rollout_restart(deployment: str, namespace: str = NAMESPACE) -> None:
    kubectl("rollout", "restart", f"deployment/{deployment}", "-n", namespace)


def rollout_wait(deployment: str, timeout_s: int = 120, namespace: str = NAMESPACE) -> bool:
    p = subprocess.run(["kubectl", "--context", CONTEXT, "rollout", "status", f"deployment/{deployment}", "-n",
                        namespace, f"--timeout={timeout_s}s"], capture_output=True, text=True,
                       timeout=timeout_s + 15)
    return p.returncode == 0


def raw(path: str, timeout: int = 30) -> str:
    """GET through the API server (e.g. a service proxy: /api/v1/namespaces/ns/services/prometheus:9090/proxy/...)."""
    return kubectl("get", "--raw", path, timeout=timeout)


def service_proxy(service: str, port: int | str, path: str, namespace: str = NAMESPACE) -> str:
    return raw(f"/api/v1/namespaces/{namespace}/services/{service}:{port}/proxy/{path.lstrip('/')}")


class ApiProxy:
    """Fast read path for the recorder: one `kubectl proxy` on loopback, then plain HTTP GETs (a kubectl process
    per request costs ~0.3 s; a poll makes ~50 requests). Read-only use: the recorder never writes through it."""

    def __init__(self, port: int = 18001):
        import urllib.request
        self.base = f"http://127.0.0.1:{port}"
        self._open = urllib.request.urlopen
        self.proc = subprocess.Popen(["kubectl", "--context", CONTEXT, "proxy", f"--port={port}",
                                      "--address=127.0.0.1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        import time
        for _ in range(50):
            try:
                self.get("/version")
                return
            except OSError:
                time.sleep(0.2)
        raise KubeError("kubectl proxy did not start")

    def get(self, path: str, timeout: int = 20) -> str:
        with self._open(self.base + path, timeout=timeout) as r:
            return r.read().decode()

    def get_json(self, path: str, timeout: int = 20) -> dict:
        return json.loads(self.get(path, timeout))

    def service(self, service: str, port: int | str, path: str, namespace: str = NAMESPACE, timeout: int = 20) -> str:
        return self.get(f"/api/v1/namespaces/{namespace}/services/{service}:{port}/proxy/{path.lstrip('/')}", timeout)

    def close(self) -> None:
        self.proc.terminate()
