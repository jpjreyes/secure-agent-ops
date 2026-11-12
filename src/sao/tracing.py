"""Tracing (OpenTelemetry): one trace per incident, across the orchestrator, the agents and the gateway.

    incident ─┬─ node commander_plan ── agent commander ── gateway call telemetry/overview (allow, read-only-tools)
              ├─ node investigate:changes ── agent investigator:changes ── gateway call … × n
              ├─ …
              └─ node execute_fix ── gateway call cluster/rollback_deployment (allow, approved by …)

Agents run inside the Claude CLI, which doesn't forward trace context; the orchestrator therefore puts a W3C
`traceparent` header (the agent's span) into the agent's MCP connection headers, and the gateway makes its decision
spans children of it. Spans follow the OpenTelemetry GenAI conventions (gen_ai.*) and OpenInference span kinds so
LLM-aware viewers (Arize Phoenix) lay them out; any OTLP backend works. Off unless SAO_TRACE_ENDPOINT (or the
standard OTEL variable) is set. Spans carry ids, decisions, counts, tokens, cost and timing, never telemetry text or
prompts (a trace store must not become a copy of the incident data).

Local viewer:  uvx --python 3.12 --from "arize-phoenix==21.0.0" phoenix serve   (UI on http://localhost:6006)
Then:          SAO_TRACE_ENDPOINT=http://localhost:6006/v1/traces uv run python -m sao.incident …
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

tracer = trace.get_tracer("secure_agent_ops")
_propagator = TraceContextTextMapPropagator()
_configured = False


def setup(service: str = "secure-agent-ops", exporter=None) -> bool:
    """Install an exporter (OTLP if SAO_TRACE_ENDPOINT is set, or the one given, e.g. in tests). Idempotent."""
    global _configured
    if _configured:
        return True
    endpoint = os.environ.get("SAO_TRACE_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if exporter is None and not endpoint:
        return False
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _configured = True
    return True


@contextmanager
def span(name: str, kind: str = "CHAIN", parent_headers: dict | None = None, **attrs) -> Iterator[trace.Span]:
    """A span with an OpenInference kind; `parent_headers` (a traceparent) attaches it to a span in another
    process."""
    ctx = _propagator.extract(parent_headers) if parent_headers else None
    with tracer.start_as_current_span(name, context=ctx) as s:
        s.set_attribute("openinference.span.kind", kind)
        set_attrs(s, **attrs)
        yield s


def set_attrs(s: trace.Span, **attrs) -> None:
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            v = [str(x) for x in v]
        elif not isinstance(v, (str, bool, int, float)):
            v = str(v)
        s.set_attribute(k, v)


def traceparent() -> dict[str, str]:
    """W3C trace context of the current span, as HTTP headers (empty when tracing is off)."""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier, context=otel_context.get_current())
    return carrier
