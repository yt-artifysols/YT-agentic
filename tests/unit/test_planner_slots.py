"""Slot rules of the daily planner (pure functions)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from ytcore.planner.daily import ChannelSchedule, compute_slot, target_date


def sched(**kw: object) -> ChannelSchedule:
    base: dict[str, object] = {
        "channel_id": "c1",
        "cluster_id": "k",
        "timezone": "UTC",
        "publish_hour": 17,
        "approval_lead_minutes": 60,
        "cluster_rank": 0,
        "slot_spacing_minutes": 30,
        "job_budget_cents": 500,
        "daily_budget_cents": 2000,
    }
    return ChannelSchedule(**{**base, **kw})  # type: ignore[arg-type]


def test_slot_is_the_local_publish_hour_in_the_audience_time_zone() -> None:
    slot = compute_slot(sched(timezone="America/New_York"), date(2026, 1, 15))
    assert slot.slot_at == datetime(2026, 1, 15, 22, 0, tzinfo=UTC)  # 17:00 EST
    summer = compute_slot(sched(timezone="America/New_York"), date(2026, 7, 15))
    assert summer.slot_at == datetime(2026, 7, 15, 21, 0, tzinfo=UTC)  # 17:00 EDT


def test_slot_follows_dst_changes() -> None:
    before = compute_slot(sched(timezone="Europe/Berlin"), date(2026, 3, 28))
    after = compute_slot(sched(timezone="Europe/Berlin"), date(2026, 3, 29))
    assert before.slot_at.hour == 16 and after.slot_at.hour == 15


def test_channels_of_one_cluster_are_staggered_and_never_share_a_minute() -> None:
    day = date(2026, 5, 4)
    slots = [compute_slot(sched(channel_id=f"c{i}", cluster_rank=i), day).slot_at for i in range(5)]
    assert len(set(slots)) == 5
    assert [b - a for a, b in zip(slots, slots[1:], strict=False)] == [timedelta(minutes=30)] * 4


def test_zero_spacing_collapses_the_stagger() -> None:
    a = compute_slot(sched(cluster_rank=0, slot_spacing_minutes=0), date(2026, 5, 4))
    b = compute_slot(sched(cluster_rank=3, slot_spacing_minutes=0), date(2026, 5, 4))
    assert a.slot_at == b.slot_at


def test_stagger_past_midnight_moves_the_local_slot_date() -> None:
    s = compute_slot(
        sched(publish_hour=23, cluster_rank=4, slot_spacing_minutes=30), date(2026, 5, 4)
    )
    assert s.slot_date == date(2026, 5, 5)


def test_approval_deadline_is_the_lead_time_before_the_slot() -> None:
    s = compute_slot(sched(approval_lead_minutes=180), date(2026, 5, 4))
    assert s.slot_at - s.approval_deadline == timedelta(hours=3)


def test_target_date_is_tomorrow_in_the_channels_time_zone() -> None:
    now = datetime(2026, 5, 4, 23, 30, tzinfo=UTC)
    assert target_date(now, "UTC") == date(2026, 5, 5)
    assert target_date(now, "Asia/Tokyo") == date(2026, 5, 6)  # already May 5th there
    assert target_date(now, "America/Los_Angeles") == date(2026, 5, 5)
