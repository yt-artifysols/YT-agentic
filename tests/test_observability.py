"""Traces, metrics, dashboards-as-code (fake backend; Postgres checks live in integration)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.testing import WorkflowEnvironment

from tests.test_workflow import Harness, _await_state
from ytcore.observability import dashboards
from ytcore.observability.metrics import Snapshot, render_prometheus
from ytcore.observability.tracing import job_span, trace_id_for
from ytcore.workflow.models import ApprovalDecision
from ytcore.workflow.states import JobState
from ytcore.workflow.video_job import VideoJobWorkflow


@pytest.fixture(scope="module")
def exporter() -> Iterator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exp))  # type: ignore[attr-defined]
    yield exp


async def test_one_trace_per_job_across_workflow_gateway_and_hub_steps(
    exporter: InMemorySpanExporter,
) -> None:
    exporter.clear()
    h = Harness()
    async with await WorkflowEnvironment.start_time_skipping() as env, h.worker(env):
        handle, inp = await h.start(env)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        await handle.signal(
            VideoJobWorkflow.submit_approval, ApprovalDecision(True, "owner", h.token(inp))
        )
        await handle.result()
    spans = [s for s in exporter.get_finished_spans() if s.attributes.get("job.id") == inp.job_id]
    names = {s.name for s in spans}
    assert {"activity.run_stage", "activity.run_qa", "activity.approve", "gateway.publish"} <= names
    assert {s.context.trace_id for s in spans} == {trace_id_for(inp.job_id)}
    gateway = next(s for s in spans if s.name == "gateway.publish")
    assert gateway.attributes["channel.id"] == "chan-a"


def test_span_attributes_are_scrubbed_and_errors_record_the_type_only(
    exporter: InMemorySpanExporter,
) -> None:
    exporter.clear()
    secret = "sk-ant-" + "a1B2c3D4" * 4
    with pytest.raises(RuntimeError), job_span("t", "job-x", None, note=f"key {secret}"):
        raise RuntimeError(f"boom {secret}")
    (span,) = exporter.get_finished_spans()
    assert secret not in str(dict(span.attributes))
    assert span.attributes["error.type"] == "RuntimeError"
    assert not span.events  # exception messages are never attached


def test_different_jobs_get_different_traces() -> None:
    assert trace_id_for("a") != trace_id_for("b")
    assert trace_id_for("a") == trace_id_for("a")


def test_prometheus_rendering_has_every_metric_family_and_escapes_labels() -> None:
    snap = Snapshot(
        queue_depth={"qa": 2},
        stage_latency_seconds={"rendering": {"p50": 1.0, "p95": 3.0}},
        cost_per_video_cents={'we"ird\nchan': 12.5},
        quota_used_ratio={"proj": 0.25},
    )
    text = render_prometheus(snap)
    for family in (
        "ytcore_queue_depth",
        "ytcore_stage_latency_seconds",
        "ytcore_rework_rate",
        "ytcore_approval_seconds_avg",
        "ytcore_cost_per_video_cents",
        "ytcore_quota_used_ratio",
        "ytcore_uploads_failed_1h",
    ):
        assert f"# TYPE {family} gauge" in text
    assert 'ytcore_queue_depth{state="qa"} 2.0' in text
    assert 'channel="we\\"ird chan"' in text


def test_committed_dashboards_and_alert_rules_match_the_code() -> None:
    assert dashboards.main(["--check"]) == 0


def test_every_alert_expression_uses_an_exported_metric() -> None:
    import re

    exported = render_prometheus(Snapshot())
    for alert in dashboards.ALERTS:
        names = set(re.findall(r"ytcore_[a-z0-9_]+", alert.expr))
        assert names, alert.name
        assert all(f"# TYPE {n} gauge" in exported for n in names), alert.name
