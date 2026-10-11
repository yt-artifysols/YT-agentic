"""Circuit breaker decision logic (database behaviour is covered in the integration tests)."""

from __future__ import annotations

from datetime import UTC, datetime

from ytcore.safety.breaker import BreakerThresholds, ClusterStats, evaluate

NOW = datetime(2026, 1, 1, tzinfo=UTC)
THR = BreakerThresholds()


def stats(**kw: float) -> ClusterStats:
    return ClusterStats("c", NOW, **kw)  # type: ignore[arg-type]


def codes(s: ClusterStats, thr: BreakerThresholds = THR) -> list[str]:
    return [r.code for r in evaluate(s, thr)]


def test_quiet_cluster_is_left_alone() -> None:
    assert codes(stats(approvals=9, rejections=1, qa_reports=10, flags=3)) == []


def test_rejection_rate_needs_enough_decisions() -> None:
    assert codes(stats(rejections=3)) == []
    assert codes(stats(approvals=1, rejections=5)) == ["rejection_rate"]


def test_policy_flags_per_job() -> None:
    assert codes(stats(qa_reports=5, flags=20)) == ["policy_flags"]
    assert codes(stats(qa_reports=2, flags=20)) == []


def test_cost_anomaly_needs_both_windows() -> None:
    spike = stats(recent_jobs=6, recent_cost_avg=900, baseline_jobs=20, baseline_cost_avg=100)
    assert codes(spike) == ["cost_anomaly"]
    assert (
        codes(stats(recent_jobs=6, recent_cost_avg=900, baseline_jobs=1, baseline_cost_avg=1)) == []
    )
    assert codes(stats(recent_jobs=6, recent_cost_avg=900)) == []  # no baseline: no judgement


def test_thresholds_are_configurable() -> None:
    strict = BreakerThresholds(max_rejection_rate=0.1, min_decisions=2)
    assert codes(stats(approvals=8, rejections=2), strict) == ["rejection_rate"]
    assert codes(stats(approvals=8, rejections=2)) == []


def test_several_reasons_are_all_reported() -> None:
    s = stats(
        approvals=1, rejections=9, qa_reports=6, flags=30,
        recent_jobs=6, recent_cost_avg=500, baseline_jobs=9, baseline_cost_avg=50,
    )  # fmt: skip
    assert codes(s) == ["rejection_rate", "policy_flags", "cost_anomaly"]
