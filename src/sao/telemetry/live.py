"""Live-cluster telemetry: a recorder that turns the running OpenTelemetry Demo into an incident snapshot as it
happens, in the same layout the ITBench-AA ingest produces. The telemetry tools (sao.telemetry.snapshot) then run
unchanged on it, so agents use the same tool names, arguments and output on the benchmark and on live faults.

Sources (all read through the Kubernetes API server: no port-forwards):
- Kubernetes objects and events (kubectl): every spec change is stored as a new object version; status-only updates
  are not (same rule as the tools).
- Prometheus: service error rate and p95 latency from the collector's span metrics, container memory / CPU from
  cAdvisor, and the firing alerts (one JSON file per poll, like ITBench's alert snapshots).
- Jaeger: spans of recent traces per service (errors fetched separately so they are never sampled away).
- OpenSearch: log records.

The recorder polls every POLL_S seconds (20) and rewrites the Parquet files atomically after each poll, so a call
sees data at most one poll old. After the episode the folder is a frozen snapshot: replayable and auditable.

  uv run python -m sao.telemetry.live runs/live/<episode>/snapshot      (records until killed)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import duckdb

from sao import kube

POLL_S = 20
TRACE_BUDGET_S = 8
NS = kube.NAMESPACE
OBJECT_KINDS = {  # list path → Kind (raw API lists don't carry the kind on each item)
    f"/apis/apps/v1/namespaces/{NS}/deployments": "Deployment",
    f"/apis/apps/v1/namespaces/{NS}/statefulsets": "StatefulSet",
    f"/apis/apps/v1/namespaces/{NS}/daemonsets": "DaemonSet",
    f"/api/v1/namespaces/{NS}/services": "Service",
    f"/api/v1/namespaces/{NS}/configmaps": "ConfigMap",
    f"/api/v1/namespaces/{NS}/pods": "Pod",
    f"/apis/networking.k8s.io/v1/namespaces/{NS}/networkpolicies": "NetworkPolicy",
    f"/apis/autoscaling/v2/namespaces/{NS}/horizontalpodautoscalers": "HorizontalPodAutoscaler",
}
SKIP_OBJECT = re.compile(r"^(grafana-dashboard-|kube-root-ca)")  # huge dashboards, cluster CA: never a cause
PROM = ("prometheus", 9090)
JAEGER = ("jaeger", 16686)
OPENSEARCH = ("opensearch", 9200)
SKIP_TRACE_SERVICES = {"jaeger", "jaeger-all-in-one"}

METRIC_QUERIES = {
    # (metric_name, metric_type, by) → PromQL; error rate and p95 per service, as in ITBench's derived metrics.
    # Span metrics reach Prometheus about once a minute, so their rates use a 2-minute window.
    ("traces_span_metrics_calls_total", "error_rate", "service"):
        '(sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER",'
        'status_code="STATUS_CODE_ERROR"}[2m])) or 0 * sum by (service_name) (rate(traces_span_metrics_calls_total'
        '{span_kind="SPAN_KIND_SERVER"}[2m]))) / sum by (service_name) (rate(traces_span_metrics_calls_total'
        '{span_kind="SPAN_KIND_SERVER"}[2m]))',
    ("traces_span_metrics_duration_milliseconds_bucket", "duration_p95", "service"):
        'histogram_quantile(0.95, sum by (le, service_name) (rate(traces_span_metrics_duration_milliseconds_bucket'
        '{span_kind="SPAN_KIND_SERVER"}[2m])))',
    ("traces_span_metrics_calls_total", "request_rate", "service"):
        'sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER"}[2m]))',
    ("container_memory_working_set_bytes", None, "pod"):
        f'sum by (pod) (container_memory_working_set_bytes{{namespace="{NS}",container!=""}})',
    ("container_cpu_usage_seconds_total", None, "pod"):
        f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{NS}",container!=""}}[1m]))',
    ("container_cpu_cfs_throttled_periods_total", None, "pod"):
        f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{namespace="{NS}",container!=""}}[1m]))',
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _strip(o: dict) -> dict:
    o = json.loads(json.dumps(o))
    o.pop("status", None)
    m = o.get("metadata", {})
    for k in ("managedFields", "resourceVersion", "generation", "uid", "creationTimestamp", "selfLink"):
        m.pop(k, None)
    (m.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if o.get("kind") == "Pod":  # pod specs carry volatile node/IP fields; keep what matters for diagnosis
        m.pop("ownerReferences", None)
    return o


class Recorder:
    def __init__(self, out: str | Path, poll_s: int = POLL_S):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "alerts").mkdir(exist_ok=True)
        self.poll_s = poll_s
        self.t0 = time.time()
        self.rows: dict[str, list[dict]] = {t: [] for t in ("events", "objects", "logs", "traces", "metrics")}
        self._obj_hash: dict[tuple, str] = {}
        self.pod_metrics: list[dict] = []  # kube-state-metrics style, from pod status (restarts, OOMKilled)
        self._event_seen: dict[str, int] = {}
        self._span_seen: set[str] = set()
        self._log_seen: set[str] = set()
        self._last_traces = self.t0 - 60
        self._last_logs = self.t0 - 60
        self._last_metrics: float | None = None
        self._metric_rows: dict[tuple, dict] = {}
        self.errors: list[str] = []
        self._api: kube.ApiProxy | None = None

    @property
    def api(self) -> kube.ApiProxy:
        if self._api is None:
            self._api = kube.ApiProxy()
        return self._api

    # ---------- sources ----------
    def poll_objects(self, now: float) -> None:
        for path, kind in OBJECT_KINDS.items():
            for o in self.api.get_json(path)["items"]:
                name = o["metadata"]["name"]
                if SKIP_OBJECT.match(name):
                    continue
                o["kind"] = kind
                if kind == "Pod":
                    for c in o.get("status", {}).get("containerStatuses") or []:
                        term = (c.get("lastState") or {}).get("terminated") or {}
                        for mname, val in (("kube_pod_container_status_restarts_total", c.get("restartCount", 0)),
                                           ("kube_pod_container_status_last_terminated_reason_OOMKilled",
                                            1.0 if term.get("reason") == "OOMKilled" else 0.0),
                                           ("kube_pod_container_status_ready", 1.0 if c.get("ready") else 0.0)):
                            self.pod_metrics.append({"metric_name": mname, "timestamp": _iso(now), "value": float(val),
                                                     "pod_name": name, "namespace": NS, "service_name": "",
                                                     "metric_type": None})
                body = _strip(o)
                h = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
                key = (body["kind"], name)
                if self._obj_hash.get(key) != h:
                    self._obj_hash[key] = h
                    self.rows["objects"].append({"timestamp": _iso(now), "object_kind": body["kind"],
                                                 "object_name": name, "namespace": NS,
                                                 "body": json.dumps(body)})

    def poll_events(self, now: float) -> None:
        for e in self.api.get_json(f"/api/v1/namespaces/{NS}/events")["items"]:
            uid, count = e["metadata"]["uid"], int(e.get("count") or 1)
            if self._event_seen.get(uid) == count:
                continue
            self._event_seen[uid] = count
            ts = e.get("lastTimestamp") or e.get("eventTime") or e["metadata"].get("creationTimestamp")
            io = e.get("involvedObject", {})
            self.rows["events"].append({"timestamp": ts, "event_kind": e.get("type"), "reason": e.get("reason"),
                                        "object_kind": io.get("kind"), "object_name": io.get("name"),
                                        "object_namespace": io.get("namespace"), "message": e.get("message") or "",
                                        "event_count": count})

    def _prom(self, path: str, params: dict) -> dict:
        return json.loads(self.api.service(*PROM, f"{path}?{urlencode(params)}"))

    def poll_alerts(self, now: float) -> None:
        d = self._prom("api/v1/alerts", {})
        (self.out / "alerts" / f"alerts_at_{_iso(now).replace(':', '-')}.json").write_text(json.dumps(d))

    def collect_metrics(self, now: float) -> None:
        """Incremental: query only since the last poll (with a minute of overlap for late samples) and keep one row
        per (metric, series, timestamp). Re-reading the whole window every poll grew with the episode."""
        start = self._last_metrics - 60 if self._last_metrics else self.t0 - 120
        for (name, mtype, by), q in METRIC_QUERIES.items():
            d = self._prom("api/v1/query_range", {"query": q, "start": start, "end": now, "step": "15s"})
            for series in d.get("data", {}).get("result", []):
                lb = series["metric"]
                ent = lb.get("pod", "") if by == "pod" else lb.get("service_name", "")
                for ts, v in series["values"]:
                    try:
                        val = float(v)
                    except ValueError:
                        continue
                    if val != val:  # NaN (no requests in that step)
                        continue
                    self._metric_rows[(name, mtype, ent, float(ts))] = {
                        "metric_name": name, "timestamp": _iso(float(ts)), "value": val,
                        "pod_name": ent if by == "pod" else "", "namespace": NS,
                        "service_name": ent if by == "service" else "", "metric_type": mtype}
        self._last_metrics = now
        self.rows["metrics"] = list(self._metric_rows.values()) + self.pod_metrics

    def _span_rows(self, data: list[dict]) -> list[dict]:
        out = []
        for tr in data:
            procs = tr.get("processes", {})
            for s in tr.get("spans", []):
                tags = {t["key"]: t.get("value") for t in s.get("tags", [])}
                parent = next((r["spanID"] for r in s.get("references", []) if r.get("refType") == "CHILD_OF"), "")
                status = tags.get("otel.status_code") or ("ERROR" if tags.get("error") in (True, "true") else "UNSET")
                out.append({
                    "timestamp": _iso(s["startTime"] / 1e6), "trace_id": s["traceID"], "span_id": s["spanID"],
                    "parent_span_id": parent, "service_name": procs.get(s["processID"], {}).get("serviceName"),
                    "span_name": s["operationName"],
                    # same spelling as ITBench-AA's exporter rows: Server/Client/…, Error/Ok/Unset
                    "span_kind": str(tags.get("span.kind", "internal")).capitalize(),
                    "duration": str(int(s["duration"]) * 1000), "status_code": str(status).capitalize(),
                    "status_message": str(tags.get("otel.status_description") or tags.get("error.message") or "")})
        return out

    def poll_traces(self, now: float) -> None:
        """Error traces first (never sampled away), then a sample of all traces; queries run in parallel and the
        whole step has a time budget so a slow Jaeger can't stall the poll."""
        from concurrent.futures import ThreadPoolExecutor, wait
        start, end = int(self._last_traces * 1e6), int(now * 1e6)
        services = [s for s in json.loads(self.api.service(*JAEGER, "jaeger/ui/api/services")).get("data") or []
                    if s not in SKIP_TRACE_SERVICES]
        queries = [(svc, {"limit": 100, "tags": json.dumps({"error": "true"})}) for svc in services]
        queries += [(svc, {"limit": 30}) for svc in services]

        def fetch(svc_extra):
            svc, extra = svc_extra
            q = urlencode({"service": svc, "start": start, "end": end, **extra})
            return json.loads(self.api.service(*JAEGER, f"jaeger/ui/api/traces?{q}", timeout=TRACE_BUDGET_S))

        pool = ThreadPoolExecutor(max_workers=6)
        futs = [pool.submit(fetch, q) for q in queries]
        done, pending = wait(futs, timeout=TRACE_BUDGET_S)
        pool.shutdown(wait=False, cancel_futures=True)
        if pending:
            self.errors.append(f"jaeger: {len(pending)}/{len(futs)} queries over the {TRACE_BUDGET_S}s budget")
        for f in done:
            try:
                rows = self._span_rows(f.result().get("data") or [])
            except (OSError, ValueError) as e:
                self.errors.append(f"jaeger: {e}")
                continue
            for r in rows:
                if r["span_id"] not in self._span_seen:
                    self._span_seen.add(r["span_id"])
                    self.rows["traces"].append(r)
        self._last_traces = now - 30  # overlap: traces finish late; duplicates are skipped by span id

    def poll_logs(self, now: float) -> None:
        body = {"size": 3000, "sort": [{"observedTimestamp": "asc"}],
                "query": {"range": {"observedTimestamp": {"gte": _iso(self._last_logs), "lte": _iso(now)}}}}
        try:
            raw = self.api.service(*OPENSEARCH, "otel-logs-*/_search?" + urlencode(
                {"source": json.dumps(body), "source_content_type": "application/json"}))
        except OSError as e:
            self.errors.append(f"opensearch: {e}")
            return
        for h in json.loads(raw).get("hits", {}).get("hits", []):
            if h["_id"] in self._log_seen:
                continue
            self._log_seen.add(h["_id"])
            s = h["_source"]
            res = s.get("resource", {}) or {}
            self.rows["logs"].append({
                "timestamp": s.get("time") or s.get("observedTimestamp"),
                "service_name": res.get("service.name") or (res.get("service") or {}).get("name"),
                "severity_text": s.get("severityText") or s.get("severity", {}).get("text", ""),
                "body": s.get("body") if isinstance(s.get("body"), str) else json.dumps(s.get("body")),
                "k8s_pod_name": res.get("k8s.pod.name", ""), "k8s_namespace": res.get("k8s.namespace.name", NS)})
        self._last_logs = now - 20

    # ---------- loop ----------
    def poll(self) -> None:
        now = time.time()
        for fn in (self.poll_objects, self.poll_events, self.poll_alerts, self.collect_metrics, self.poll_traces,
                   self.poll_logs):
            try:
                fn(now)
            except Exception as e:  # one source down must not stop the others
                self.errors.append(f"{fn.__name__}: {type(e).__name__}: {str(e)[:200]}")
        self.flush()

    def flush(self) -> None:
        con = duckdb.connect()
        schemas = {
            "events": "timestamp VARCHAR, event_kind VARCHAR, reason VARCHAR, object_kind VARCHAR, object_name VARCHAR,"
                      " object_namespace VARCHAR, message VARCHAR, event_count INTEGER",
            "objects": "timestamp VARCHAR, object_kind VARCHAR, object_name VARCHAR, namespace VARCHAR, body VARCHAR",
            "logs": "timestamp VARCHAR, service_name VARCHAR, severity_text VARCHAR, body VARCHAR,"
                    " k8s_pod_name VARCHAR, k8s_namespace VARCHAR",
            "traces": "timestamp VARCHAR, trace_id VARCHAR, span_id VARCHAR, parent_span_id VARCHAR,"
                      " service_name VARCHAR, span_name VARCHAR, span_kind VARCHAR, duration VARCHAR,"
                      " status_code VARCHAR, status_message VARCHAR",
            "metrics": "metric_name VARCHAR, timestamp VARCHAR, value DOUBLE, pod_name VARCHAR, namespace VARCHAR,"
                       " service_name VARCHAR, metric_type VARCHAR",
        }
        for t, cols in schemas.items():
            names = [c.split()[0] for c in cols.split(",")]
            src = self.out / f".{t}.ndjson"
            with src.open("w") as fh:  # bulk load through NDJSON: executemany is row by row and grows with the episode
                for r in self.rows[t]:
                    fh.write(json.dumps({n: r.get(n) for n in names}) + "\n")
            col_types = ", ".join(f"'{c.split()[0]}': '{c.split()[1]}'" for c in cols.split(","))
            if self.rows[t]:
                con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_json('{src}', format='newline_delimited', "
                            f"columns={{{col_types}}})")
            else:
                con.execute(f"CREATE TABLE {t} ({cols})")
            ts_cast = "try_cast(timestamp AS TIMESTAMP) AS timestamp" if t != "traces" else "timestamp"
            sel = ", ".join(ts_cast if n == "timestamp" else n for n in names)
            tmp = self.out / f".{t}.parquet.tmp"
            con.execute(f"COPY (SELECT {sel} FROM {t}) TO '{tmp}' (FORMAT parquet)")
            os.replace(tmp, self.out / f"{t}.parquet")
        (self.out / "recorder.json").write_text(json.dumps(
            {"started": _iso(self.t0), "last_poll": _iso(time.time()),
             "rows": {k: len(v) for k, v in self.rows.items()}, "errors": self.errors[-20:]}, indent=1))
        (self.out / ".done").touch()

    def run(self, seconds: float | None = None) -> None:
        stop = False

        def _stop(*_):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        end = time.time() + seconds if seconds else None
        while not stop and (end is None or time.time() < end):
            t = time.time()
            self.poll()
            while not stop and time.time() - t < self.poll_s:
                time.sleep(0.5)
        self.poll()
        if self._api:
            self._api.close()


if __name__ == "__main__":
    Recorder(sys.argv[1]).run(float(sys.argv[2]) if len(sys.argv) > 2 else None)
