"""OpenTelemetry traces per job across workflow, agents, gateway and hub.

Every component derives the SAME trace id from the job id, so a job's activities, agent stages,
gateway publish and hub approval all land in one trace without passing context between
processes. Spans carry ids, states and counts only: attribute values are scrubbed and content
(scripts, prompts, tokens, titles) is never attached.

The API is always importable (a core dependency) and a no-op until `configure_tracing` installs
an SDK provider in the process.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from ytcore.safety.scrub import scrub

TRACER_NAME = "ytcore"


def trace_id_for(job_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"trace:{job_id}".encode()).digest()[:16], "big") or 1


def _root_span_id(job_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"root:{job_id}".encode()).digest()[:8], "big") or 1


def _safe(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    return scrub(str(value))[:200]


@contextmanager
def job_span(
    name: str, job_id: str, channel_id: str | None = None, **attrs: Any
) -> Iterator[trace.Span]:
    """A span in the job's trace. Exceptions are recorded (type only) and re-raised."""
    parent = NonRecordingSpan(
        SpanContext(
            trace_id=trace_id_for(job_id),
            span_id=_root_span_id(job_id),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    )
    ctx = trace.set_span_in_context(parent)
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(name, context=ctx, record_exception=False) as span:
        span.set_attribute("job.id", job_id)
        if channel_id:
            span.set_attribute("channel.id", channel_id)
        for key, value in attrs.items():
            span.set_attribute(key, _safe(value))
        try:
            yield span
        except Exception as exc:
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(trace.StatusCode.ERROR)
            raise


def configure_tracing(service_name: str, endpoint: str | None = None) -> None:  # pragma: no cover
    """Install an SDK tracer provider (OTLP over HTTP when an endpoint is given).

    Call once at process start in each service. Needs the `otel` extra."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
