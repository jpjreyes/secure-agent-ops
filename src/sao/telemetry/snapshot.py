"""Read-only telemetry tools over one ITBench-AA incident snapshot (Parquet, see evals/ingest.py).

The same tool names and outputs are implemented for the live cluster (Prometheus, Jaeger, OpenSearch, Kubernetes
API), so agents investigate the benchmark and live faults the same way. Every tool returns a short text table, not a
raw dump: fewer tokens, and less noise to mistake for a cause.

Two snapshot formats exist (older plain columns, newer OpenTelemetry-exporter rows with JSON bodies); both are mapped
to the same views here: ev (Kubernetes events), obj (object versions), logs, spans, metrics.
"""

from __future__ import annotations

import glob
import hashlib
import json
import re
from pathlib import Path

import duckdb

MAX_CELL = 160
ERROR_WORDS = r"(?i)(error|exception|fail|refused|timeout|timed out|unavailable|denied|panic|oom|killed|crash|unreachable)"
VOLATILE_META = ("managedFields", "resourceVersion", "generation", "uid", "creationTimestamp", "selfLink")


def _cell(v) -> str:
    s = "" if v is None else (f"{v:.3g}" if isinstance(v, float) else str(v))
    s = s.replace("\n", " ").replace("|", "/")
    return s if len(s) <= MAX_CELL else s[: MAX_CELL - 1] + "…"


def table(cols: list[str], rows: list[tuple], limit: int = 40, total: int | None = None) -> str:
    if not rows:
        return "(no rows)"
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(_cell(v) for v in r) + " |" for r in rows[:limit]]
    n = total if total is not None else len(rows)
    if n > limit:
        out.append(f"({n - limit} more rows not shown; narrow the query)")
    return "\n".join(out)


def _strip_volatile(o: dict) -> dict:
    o = json.loads(json.dumps(o))
    meta = o.get("metadata", {})
    for k in VOLATILE_META:
        meta.pop(k, None)
    (meta.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    o.pop("status", None)
    return o


def _flatten(o, prefix: str = "") -> dict[str, str]:
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
        return out
    if isinstance(o, str) and o.lstrip().startswith(("{", "[")):  # JSON stored in a string (e.g. ConfigMap data)
        try:
            return _flatten(json.loads(o), prefix)
        except json.JSONDecodeError:
            pass
    if isinstance(o, list):
        out = {}
        for i, v in enumerate(o):
            key = v.get("name") if isinstance(v, dict) and "name" in v else i  # env vars, containers: by name
            out.update(_flatten(v, f"{prefix}[{key}]"))
        return out
    return {prefix: json.dumps(o) if not isinstance(o, str) else o}


class Snapshot:
    def __init__(self, path: str | Path):
        self.dir = Path(path)
        if not (self.dir / ".done").exists():
            raise FileNotFoundError(f"not an ingested snapshot: {self.dir}")
        self.con = duckdb.connect()
        self._views()

    # ---------- normalization ----------
    def _cols(self, table_name: str) -> set[str]:
        f = self.dir / f"{table_name}.parquet"
        return {c[0] for c in self.con.execute(f"DESCRIBE SELECT * FROM '{f}'").fetchall()}

    def _views(self) -> None:
        p = lambda t: f"read_parquet('{self.dir / (t + '.parquet')}')"  # noqa: E731
        ev, ob, lg, sp = self._cols("events"), self._cols("objects"), self._cols("logs"), self._cols("traces")
        if "Body" in ev:
            self.con.execute(f"""CREATE VIEW ev AS SELECT Timestamp AS ts,
                json_extract_string(Body,'$.object.type') AS type, json_extract_string(Body,'$.object.reason') AS reason,
                coalesce(json_extract_string(Body,'$.object.involvedObject.kind'),
                         json_extract_string(Body,'$.object.regarding.kind')) AS kind,
                coalesce(json_extract_string(Body,'$.object.involvedObject.name'),
                         json_extract_string(Body,'$.object.regarding.name')) AS name,
                coalesce(json_extract_string(Body,'$.object.involvedObject.namespace'),
                         json_extract_string(Body,'$.object.regarding.namespace'),
                         json_extract_string(Body,'$.object.metadata.namespace')) AS namespace,
                coalesce(json_extract_string(Body,'$.object.message'), json_extract_string(Body,'$.object.note')) AS message,
                try_cast(coalesce(json_extract_string(Body,'$.object.count'),
                                  json_extract_string(Body,'$.object.deprecatedCount')) AS INTEGER) AS count
                FROM {p('events')} WHERE json_valid(Body)""")
        else:
            self.con.execute(f"""CREATE VIEW ev AS SELECT timestamp AS ts, event_kind AS type, reason,
                object_kind AS kind, object_name AS name, object_namespace AS namespace, message,
                try_cast(event_count AS INTEGER) AS count
                FROM {p('events')} WHERE message IS NOT NULL AND message <> ''""")
        if "Body" in ob:
            self.con.execute(f"""CREATE VIEW obj AS WITH b AS (
                SELECT Timestamp AS ts, CASE WHEN json_exists(Body,'$.object.kind') THEN json_extract(Body,'$.object')
                                             ELSE json(Body) END AS o FROM {p('objects')} WHERE json_valid(Body))
                SELECT ts, json_extract_string(o,'$.kind') AS kind, json_extract_string(o,'$.metadata.name') AS name,
                       coalesce(json_extract_string(o,'$.metadata.namespace'), '') AS namespace, o::VARCHAR AS body FROM b""")
        else:
            self.con.execute(f"""CREATE VIEW obj AS SELECT timestamp AS ts, object_kind AS kind, object_name AS name,
                coalesce(namespace, '') AS namespace, body FROM {p('objects')} WHERE json_valid(body)""")
        if "Body" in lg:
            self.con.execute(f"""CREATE VIEW logs AS SELECT Timestamp AS ts, ServiceName AS service,
                lower(coalesce(SeverityText, '')) AS severity, Body AS body,
                regexp_extract(ResourceAttributes, '''k8s\\.pod\\.name'': ''([^'']+)''', 1) AS pod,
                regexp_extract(ResourceAttributes, '''k8s\\.namespace\\.name'': ''([^'']+)''', 1) AS namespace
                FROM {p('logs')}""")
        else:
            self.con.execute(f"""CREATE VIEW logs AS SELECT timestamp AS ts, service_name AS service,
                lower(coalesce(severity_text, '')) AS severity, body, k8s_pod_name AS pod, k8s_namespace AS namespace
                FROM {p('logs')}""")
        if "TraceId" in sp:
            self.con.execute(f"""CREATE VIEW spans AS SELECT Timestamp AS ts, TraceId AS trace_id, SpanId AS span_id,
                ParentSpanId AS parent_id, ServiceName AS service, SpanName AS name, SpanKind AS kind,
                Duration / 1e6 AS ms, lower(coalesce(StatusCode, '')) AS status, StatusMessage AS message
                FROM {p('traces')}""")
        else:
            self.con.execute(f"""CREATE VIEW spans AS SELECT try_cast(timestamp AS TIMESTAMP) AS ts, trace_id,
                span_id, parent_span_id AS parent_id, service_name AS service, span_name AS name, span_kind AS kind,
                try_cast(duration AS DOUBLE) / 1e6 AS ms, lower(coalesce(status_code, '')) AS status,
                status_message AS message FROM {p('traces')}""")
        self.con.execute(f"""CREATE VIEW metrics AS SELECT timestamp AS ts, metric_name AS metric, value, pod_name AS pod,
            service_name AS service, namespace, metric_type AS mtype FROM {p('metrics')}""")

    def _q(self, sql: str, params: list | None = None) -> list[tuple]:
        return self.con.execute(sql, params or []).fetchall()

    # ---------- tools ----------
    def overview(self) -> str:
        """Time window, what data exists, and the services seen. Start here."""
        rows = []
        for v in ("ev", "obj", "logs", "spans", "metrics"):
            n, t0, t1 = self._q(f"SELECT count(*), min(ts), max(ts) FROM {v}")[0]
            rows.append((v, n, t0, t1))
        svc = self._q("SELECT service, count(*) FROM spans GROUP BY 1 ORDER BY 2 DESC")
        ns = self._q("SELECT namespace, count(*) FROM obj GROUP BY 1 ORDER BY 2 DESC")
        return ("Data sources:\n" + table(["source", "rows", "from", "to"], rows)
                + "\n\nServices with traces: " + ", ".join(f"{s} ({n})" for s, n in svc)
                + "\nNamespaces with objects: " + ", ".join(f"{s or '(cluster)'} ({n})" for s, n in ns))

    def alerts(self) -> str:
        """Firing alerts across the incident: name, service, first time active, how many snapshots it fired in."""
        seen: dict[tuple, dict] = {}
        files = sorted(glob.glob(str(self.dir / "alerts" / "*.json")))
        for f in files:
            try:
                d = json.loads(Path(f).read_text())
            except json.JSONDecodeError:
                continue
            alerts = (d.get("data") or {}).get("alerts", []) if isinstance(d, dict) else d
            for a in alerts or []:
                if a.get("state") != "firing":
                    continue
                lb = a.get("labels", {})
                key = (lb.get("alertname"), lb.get("service_name") or lb.get("service") or lb.get("pod") or "",
                       lb.get("namespace", ""), lb.get("severity", ""))
                s = seen.setdefault(key, {"first": a.get("activeAt"), "n": 0, "desc": ""})
                s["n"] += 1
                s["desc"] = (a.get("annotations") or {}).get("description", "") or s["desc"]
                if a.get("activeAt") and (not s["first"] or a["activeAt"] < s["first"]):
                    s["first"] = a["activeAt"]
        rows = sorted(((*k, v["first"], v["n"], v["desc"]) for k, v in seen.items()), key=lambda r: str(r[4]))
        return (f"{len(files)} alert snapshots.\n"
                + table(["alert", "entity", "namespace", "severity", "first active", "snapshots firing", "description"],
                        rows))

    def events(self, warnings_only: bool = False, namespace: str | None = None, name_contains: str | None = None,
               limit: int = 40) -> str:
        """Kubernetes events grouped by object and reason (Normal and Warning; chaos experiments, rollouts and
        scaling are Normal events), with counts, first/last time and a sample message."""
        where, args = ["1=1"], []
        if warnings_only:
            where.append("lower(type) = 'warning'")
        if namespace:
            where.append("namespace = ?")
            args.append(namespace)
        if name_contains:
            where.append("name ILIKE ?")
            args.append(f"%{name_contains}%")
        rows = self._q(f"""SELECT namespace, kind, name, reason, count(*) AS n, min(ts), max(ts), any_value(message)
                           FROM ev WHERE {' AND '.join(where)} GROUP BY 1,2,3,4 ORDER BY min(ts)""", args)
        return table(["namespace", "kind", "name", "reason", "events", "first", "last", "message"], rows, limit)

    def resources(self, kind: str | None = None, namespace: str | None = None, limit: int = 60) -> str:
        """Which Kubernetes objects exist (kind counts, or names for one kind)."""
        if not kind:
            rows = self._q("""SELECT kind, namespace, count(DISTINCT name) FROM obj
                              WHERE (? IS NULL OR namespace = ?) GROUP BY 1,2 ORDER BY 1,2""", [namespace, namespace])
            return table(["kind", "namespace", "objects"], rows, limit)
        rows = self._q("""SELECT namespace, name, min(ts), max(ts), count(*) FROM obj WHERE lower(kind) = lower(?)
                          AND (? IS NULL OR namespace = ?) GROUP BY 1,2 ORDER BY 1,2""", [kind, namespace, namespace])
        return table(["namespace", "name", "first seen", "last seen", "versions recorded"], rows, limit)

    def _versions(self, kind: str | None = None, name: str | None = None, namespace: str | None = None):
        rows = self._q("""SELECT kind, namespace, name, ts, body FROM obj
                          WHERE (? IS NULL OR lower(kind) = lower(?)) AND (? IS NULL OR name = ?)
                            AND (? IS NULL OR namespace = ?) ORDER BY ts""",
                       [kind, kind, name, name, namespace, namespace])
        by: dict[tuple, list] = {}
        for k, ns, nm, ts, body in rows:
            try:
                o = _strip_volatile(json.loads(body))
            except (json.JSONDecodeError, TypeError):
                continue
            h = hashlib.md5(json.dumps(o, sort_keys=True).encode()).hexdigest()
            vs = by.setdefault((k, ns, nm), [])
            if not vs or vs[-1][1] != h:
                vs.append((ts, h, o))
        return by

    def changes(self, kind: str | None = None, limit: int = 40) -> str:
        """Objects whose spec/config changed during the window (status-only updates ignored), and objects first seen
        after the window started (created during the incident)."""
        by = self._versions(kind)
        if not by:
            return "(no objects)"
        start = min(v[0][0] for v in by.values())
        rows = []
        for (k, ns, nm), vs in by.items():
            if len(vs) > 1:
                diff = _flatten(vs[0][2]), _flatten(vs[-1][2])
                keys = sorted(k2 for k2 in set(diff[0]) | set(diff[1]) if diff[0].get(k2) != diff[1].get(k2))
                rows.append((vs[-1][0], k, ns, nm, f"{len(vs)} versions", ", ".join(keys[:6])))
            elif vs[0][0] > start and (vs[0][0] - start).total_seconds() > 120:
                rows.append((vs[0][0], k, ns, nm, "created during window", ""))
        rows.sort(key=lambda r: r[0])
        return table(["when", "kind", "namespace", "name", "change", "changed fields"], rows, limit)

    def object_diff(self, kind: str, name: str, namespace: str | None = None) -> str:
        """Field-by-field difference between the first and last recorded version of one object."""
        by = self._versions(kind, name, namespace)
        if not by:
            return f"(no {kind} named {name})"
        out = []
        for (k, ns, nm), vs in by.items():
            a, b = _flatten(vs[0][2]), _flatten(vs[-1][2])
            keys = sorted(x for x in set(a) | set(b) if a.get(x) != b.get(x))
            out.append(f"{ns}/{k}/{nm}: {len(vs)} versions, {vs[0][0]} → {vs[-1][0]}")
            out.append(table(["field", "first", "last"], [(x, a.get(x, "(absent)"), b.get(x, "(absent)")) for x in keys]))
        return "\n".join(out)

    def get_object(self, kind: str, name: str, namespace: str | None = None, max_chars: int = 3000) -> str:
        """The latest recorded version of one object (spec only, volatile metadata removed)."""
        by = self._versions(kind, name, namespace)
        if not by:
            return f"(no {kind} named {name})"
        (k, ns, nm), vs = next(iter(by.items()))
        text = json.dumps(vs[-1][2], separators=(",", ":"))
        return f"{ns}/{k}/{nm} (latest of {len(vs)} versions)\n" + (text if len(text) <= max_chars
                                                                    else text[:max_chars] + " …(truncated)")

    def log_errors(self, service: str | None = None, limit: int = 30) -> str:
        """Error and warning log lines grouped by service and message pattern (numbers and ids collapsed)."""
        rows = self._q(f"""
            WITH e AS (SELECT service, pod, ts, body,
                         regexp_replace(regexp_replace(left(body, 300), '[0-9a-f]{{8,}}|[0-9a-f-]{{36}}', '<id>', 'g'),
                                        '[0-9]+', '#', 'g') AS pattern
                       FROM logs WHERE (? IS NULL OR service = ?)
                         AND (severity IN ('error','err','fatal','critical','warn','warning')
                              OR regexp_matches(body, '{ERROR_WORDS}')))
            SELECT service, pattern, count(*) AS n, count(DISTINCT pod), min(ts), max(ts)
            FROM e GROUP BY 1,2 ORDER BY n DESC""", [service, service])
        return table(["service", "message pattern", "lines", "pods", "first", "last"], rows, limit)

    def search_logs(self, pattern: str, service: str | None = None, limit: int = 20) -> str:
        """Log lines matching a regular expression (case-insensitive), newest first."""
        rows = self._q("""SELECT ts, service, pod, left(body, 300) FROM logs
                          WHERE regexp_matches(body, ?) AND (? IS NULL OR service = ?) ORDER BY ts DESC LIMIT ?""",
                       [f"(?i){pattern}", service, service, limit + 1])
        return table(["time", "service", "pod", "line"], rows, limit)

    def trace_summary(self) -> str:
        """Per service: spans, errors, error rate, p50/p95 latency of server spans."""
        rows = self._q("""SELECT service, count(*) AS spans, count(*) FILTER (WHERE status = 'error') AS errors,
                                 round(100.0 * count(*) FILTER (WHERE status = 'error') / count(*), 2) AS err_pct,
                                 quantile_cont(ms, 0.5) FILTER (WHERE kind ILIKE '%server%'),
                                 quantile_cont(ms, 0.95) FILTER (WHERE kind ILIKE '%server%')
                          FROM spans GROUP BY 1 ORDER BY errors DESC, spans DESC""")
        return table(["service", "spans", "errors", "error %", "p50 ms (server)", "p95 ms (server)"], rows)

    def trace_errors(self, service: str | None = None, limit: int = 25) -> str:
        """Error spans grouped by service, operation and status message."""
        rows = self._q("""SELECT service, name, left(coalesce(message, ''), 200), count(*), min(ts), max(ts) FROM spans
                          WHERE status = 'error' AND (? IS NULL OR service = ?) GROUP BY 1,2,3 ORDER BY 4 DESC""",
                       [service, service])
        return table(["service", "operation", "status message", "spans", "first", "last"], rows, limit)

    def dependencies(self, service: str | None = None, limit: int = 40) -> str:
        """Service-to-service calls seen in traces: caller → callee, calls, callee errors, callee p95."""
        rows = self._q("""SELECT p.service, c.service, count(*), count(*) FILTER (WHERE c.status = 'error'),
                                 quantile_cont(c.ms, 0.95)
                          FROM spans c JOIN spans p ON c.parent_id = p.span_id AND c.trace_id = p.trace_id
                          WHERE c.service <> p.service AND (? IS NULL OR ? IN (p.service, c.service))
                          GROUP BY 1,2 ORDER BY 4 DESC, 3 DESC""", [service, service])
        return table(["caller", "callee", "calls", "callee errors", "callee p95 ms"], rows, limit)

    def metric_anomalies(self, limit: int = 20) -> str:
        """Biggest changes between the first and last third of the window: service error rate and p95 latency,
        container memory, CPU throttling. A starting point, not proof."""
        rows = self._q("""
            WITH w AS (SELECT min(ts) t0, max(ts) t1 FROM metrics),
            m AS (SELECT CASE WHEN mtype IN ('error_rate', 'duration_p95') THEN mtype ELSE metric END AS signal,
                         coalesce(nullif(service, ''), pod) AS entity, ts, value
                  FROM metrics WHERE mtype IN ('error_rate', 'duration_p95')
                     OR metric IN ('container_memory_working_set_bytes', 'container_cpu_cfs_throttled_periods_total')),
            s AS (SELECT signal, entity,
                         avg(value) FILTER (WHERE ts < t0 + (t1 - t0) / 3) AS early,
                         avg(value) FILTER (WHERE ts > t1 - (t1 - t0) / 3) AS late, max(value) AS peak
                  FROM m, w GROUP BY 1,2)
            SELECT signal, entity, early, late, peak,
                   CASE WHEN coalesce(early, 0) = 0 THEN CASE WHEN late > 0 THEN 999 ELSE 0 END
                        ELSE late / early END AS ratio
            FROM s WHERE late IS NOT NULL AND (late > coalesce(early, 0) * 1.5 OR (coalesce(early,0)=0 AND late>0))
            ORDER BY ratio DESC, peak DESC""")
        return table(["signal", "service / pod", "early avg", "late avg", "peak", "late/early"], rows, limit)

    def metric_series(self, metric: str, entity: str, buckets: int = 12) -> str:
        """One metric over time for a service or pod (substring match), averaged into equal time buckets."""
        rows = self._q("""
            WITH w AS (SELECT min(ts) t0, max(ts) t1 FROM metrics),
                 m AS (SELECT ts, value FROM metrics WHERE (metric = ? OR mtype = ?)
                         AND (service ILIKE ? OR pod ILIKE ?))
            SELECT least(floor(epoch(ts - t0) / greatest(epoch(t1 - t0), 1) * ?), ? - 1) AS b,
                   min(ts), avg(value), max(value), count(*) FROM m, w GROUP BY 1 ORDER BY 1""",
                       [metric, metric, f"%{entity}%", f"%{entity}%", buckets, buckets])
        return table(["bucket", "from", "avg", "max", "samples"], [r[1:] for r in rows], buckets)

    def list_metrics(self, entity: str | None = None, limit: int = 60) -> str:
        """Metric names available (optionally for one service/pod substring)."""
        rows = self._q("""SELECT coalesce(mtype, metric) AS m, count(*) FROM metrics
                          WHERE (? IS NULL OR service ILIKE ? OR pod ILIKE ?) GROUP BY 1 ORDER BY 2 DESC""",
                       [entity, f"%{entity}%", f"%{entity}%"])
        return table(["metric", "samples"], rows, limit)


TOOLS = ["overview", "alerts", "events", "resources", "changes", "object_diff", "get_object", "log_errors",
         "search_logs", "trace_summary", "trace_errors", "dependencies", "metric_anomalies", "metric_series",
         "list_metrics"]


def describe() -> dict[str, str]:
    """Tool name → first line of its docstring (for tool lists)."""
    return {t: (getattr(Snapshot, t).__doc__ or "").strip().split("\n")[0] for t in TOOLS}


if __name__ == "__main__":
    import sys
    snap = Snapshot(sys.argv[1])
    tool = sys.argv[2] if len(sys.argv) > 2 else "overview"
    kwargs = dict(a.split("=", 1) for a in sys.argv[3:])
    print(getattr(snap, tool)(**kwargs))
    _ = re  # kept for callers that build patterns
