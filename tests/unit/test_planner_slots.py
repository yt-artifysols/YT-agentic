"""Slot rules of the daily planner (pure functions)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

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
    assert [b - a for a, b in pairwise(slots)] == [timedelta(minutes=30)] * 4


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


# ------------------------------------------------------------------ slot allocator (Phase 5)

from ytcore.planner.daily import allocate_slots, jitter_minutes  # noqa: E402


def windowed(i: int, **kw: object) -> ChannelSchedule:
    return sched(channel_id=f"ch-{i:02}", cluster_rank=i, window_minutes=120, **kw)


def test_slots_stay_inside_the_audience_window_and_vary_by_day() -> None:
    s = windowed(0, timezone="Europe/Berlin")
    offsets = set()
    for d in range(10):
        day = date(2026, 5, 4) + timedelta(days=d)
        slot = allocate_slots([(s, day)])[s.channel_id]
        local = slot.slot_at.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Berlin"))
        assert 17 * 60 <= local.hour * 60 + local.minute < 19 * 60
        offsets.add(local.hour * 60 + local.minute)
    assert len(offsets) >= 6  # not the same minute every day


def test_allocation_is_deterministic() -> None:
    items = [(windowed(i), date(2026, 5, 4)) for i in range(8)]
    assert allocate_slots(items) == allocate_slots(list(reversed(items)))


def test_twenty_channels_in_one_cluster_respect_spacing_and_never_share_a_minute() -> None:
    items = [(windowed(i, slot_spacing_minutes=5), date(2026, 5, 4)) for i in range(20)]
    slots = sorted(s.slot_at for s in allocate_slots(items).values())
    assert len({s.replace(second=0) for s in slots}) == 20
    assert all(b - a >= timedelta(minutes=5) for a, b in pairwise(slots))


def test_channels_in_different_clusters_still_avoid_the_same_minute() -> None:
    items = [
        (sched(channel_id=f"c{i}", cluster_id=f"k{i}", window_minutes=60), date(2026, 5, 4))
        for i in range(12)
    ]
    slots = [s.slot_at for s in allocate_slots(items).values()]
    assert len(set(slots)) == 12


def test_a_new_channel_is_placed_around_slots_that_are_already_planned() -> None:
    day = date(2026, 5, 4)
    first = allocate_slots([(windowed(i, slot_spacing_minutes=20), day) for i in range(5)])
    taken = [("k", s.slot_at) for s in first.values()]
    newcomer = allocate_slots([(windowed(9, slot_spacing_minutes=20), day)], taken)["ch-09"]
    assert all(abs(newcomer.slot_at - at) >= timedelta(minutes=20) for _, at in taken)


def test_different_channels_get_different_offsets_on_the_same_day() -> None:
    offsets = {jitter_minutes(f"ch-{i:02}", date(2026, 5, 4), 120) for i in range(20)}
    assert len(offsets) >= 15


def test_zero_window_keeps_the_pilot_behaviour() -> None:
    s = sched(window_minutes=0, cluster_rank=2)
    assert allocate_slots([(s, date(2026, 5, 4))])["c1"] == compute_slot(s, date(2026, 5, 4))
