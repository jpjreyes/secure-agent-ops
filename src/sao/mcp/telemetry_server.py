"""Read-only telemetry MCP server (investigators' tools).

Serves the tools in sao.telemetry over MCP, for one incident: an ITBench-AA snapshot today (SAO_SNAPSHOT=<folder>),
the live cluster later (same tool names). Every tool is annotated read-only; the gateway's default policy uses those
annotations, so investigators can never reach a tool that changes anything.

Run:  SAO_SNAPSHOT=data/itbench-aa/snapshots/Scenario-24 uv run python -m sao.mcp.telemetry_server   (stdio)
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from sao.telemetry.snapshot import Snapshot

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

server = MCPServer("telemetry", instructions="Read-only incident telemetry: alerts, Kubernetes events and object "
                   "changes, logs, traces and metrics for the incident under investigation.")
_snap: Snapshot | None = None


def snap() -> Snapshot:
    global _snap
    if _snap is None:
        _snap = Snapshot(os.environ["SAO_SNAPSHOT"])
    return _snap


@server.tool(annotations=READ_ONLY)
def overview() -> str:
    """Time window, which data exists, and the services seen. Start here."""
    return snap().overview()


@server.tool(annotations=READ_ONLY)
def alerts() -> str:
    """Firing alerts: name, entity, first time active, how many snapshots each fired in. Some fire all the time
    (e.g. Watchdog); compare first-active times with the incident window."""
    return snap().alerts()


@server.tool(annotations=READ_ONLY)
def events(warnings_only: bool = False, namespace: str | None = None, name_contains: str | None = None,
           limit: int = 40) -> str:
    """Kubernetes events grouped by object and reason (Normal and Warning: chaos experiments, rollouts and scaling
    are Normal events), with counts, first/last time and a sample message. Filter by namespace or object name."""
    return snap().events(warnings_only, namespace, name_contains, limit)


@server.tool(annotations=READ_ONLY)
def resources(kind: str | None = None, namespace: str | None = None, limit: int = 60) -> str:
    """Which Kubernetes objects exist: counts per kind, or the names of one kind."""
    return snap().resources(kind, namespace, limit)


@server.tool(annotations=READ_ONLY)
def changes(kind: str | None = None, limit: int = 40) -> str:
    """Objects whose spec or config changed during the window (status-only updates ignored) and objects created
    during it, with the changed fields."""
    return snap().changes(kind, limit)


@server.tool(annotations=READ_ONLY)
def object_diff(kind: str, name: str, namespace: str | None = None) -> str:
    """Field-by-field difference between the first and last version of one object (JSON inside ConfigMaps opened)."""
    return snap().object_diff(kind, name, namespace)


@server.tool(annotations=READ_ONLY)
def get_object(kind: str, name: str, namespace: str | None = None) -> str:
    """The latest version of one object (spec, volatile metadata removed)."""
    return snap().get_object(kind, name, namespace)


@server.tool(annotations=READ_ONLY)
def log_errors(service: str | None = None, limit: int = 30) -> str:
    """Error and warning log lines grouped by service and message pattern (numbers and ids collapsed)."""
    return snap().log_errors(service, limit)


@server.tool(annotations=READ_ONLY)
def search_logs(pattern: str, service: str | None = None, limit: int = 20) -> str:
    """Log lines matching a regular expression (case-insensitive), newest first."""
    return snap().search_logs(pattern, service, limit)


@server.tool(annotations=READ_ONLY)
def trace_summary() -> str:
    """Per service: spans, errors, error rate, p50/p95 latency of server spans."""
    return snap().trace_summary()


@server.tool(annotations=READ_ONLY)
def trace_errors(service: str | None = None, limit: int = 25) -> str:
    """Error spans grouped by service, operation and status message."""
    return snap().trace_errors(service, limit)


@server.tool(annotations=READ_ONLY)
def dependencies(service: str | None = None, limit: int = 40) -> str:
    """Service-to-service calls from traces: caller → callee, calls, callee errors, callee p95 latency."""
    return snap().dependencies(service, limit)


@server.tool(annotations=READ_ONLY)
def metric_anomalies(limit: int = 20) -> str:
    """Biggest changes between the first and last third of the window (service error rate and p95 latency, container
    memory, CPU throttling). A starting point, not proof."""
    return snap().metric_anomalies(limit)


@server.tool(annotations=READ_ONLY)
def metric_series(metric: str, entity: str, buckets: int = 12) -> str:
    """One metric over time for a service or pod (substring match), averaged into equal time buckets."""
    return snap().metric_series(metric, entity, buckets)


@server.tool(annotations=READ_ONLY)
def list_metrics(entity: str | None = None, limit: int = 60) -> str:
    """Metric names available, optionally for one service or pod."""
    return snap().list_metrics(entity, limit)


if __name__ == "__main__":
    server.run("stdio")
