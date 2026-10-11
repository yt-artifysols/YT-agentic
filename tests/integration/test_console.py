"""Operator console read-models: overview, pipeline, timeline, channels and users."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.integration.hubkit import Hub, build_hub

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def hub(database: str) -> Hub:
    return build_hub(database)


async def test_overview_pipeline_timeline_and_channels(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    waiting, _ = hub.awaiting_job(ch, risk=12)
    early = hub.worker_store.create_job(ch, "owner", actor="planner")  # still planned
    async with hub.client() as c:
        await hub.register(c, "viewer", "approver")
        ov = (await c.get("/api/overview")).json()
        assert ov["awaiting_approval"]["count"] >= 1 and ov["in_production"] >= 1
        assert ov["kill_switch"]["global_paused"] is False
        assert {"generated_at", "alerts", "activity", "clusters", "tiers", "last_24h"} <= set(ov)
        assert any(a["job_id"] == waiting for a in ov["activity"])

        pipe = (await c.get("/api/pipeline", params={"channel": ch})).json()
        states = {i["job_id"]: i["state"] for i in pipe["items"]}
        assert states[waiting] == "awaiting_approval" and states[early] == "planned"
        assert pipe["counts"]["awaiting_approval"] >= 1
        only = (await c.get("/api/pipeline", params={"channel": ch, "state": "planned"})).json()
        assert [i["job_id"] for i in only["items"]] == [early]
        assert (await c.get("/api/pipeline", params={"state": "nonsense"})).status_code == 422

        line = (await c.get(f"/api/jobs/{waiting}/timeline")).json()
        assert line[0]["to_state"] == "planned"
        assert line[-1]["to_state"] == "awaiting_approval"
        assert (await c.get("/api/jobs/not-a-uuid/timeline")).status_code == 404

        chans = {x["id"]: x for x in (await c.get("/api/channels")).json()}
        assert chans[ch]["awaiting_approval"] == 1 and chans[ch]["approval_tier"] == 1
        detail = (await c.get(f"/api/channels/{ch}")).json()
        assert detail["id"] == ch and "recent_jobs" in detail and "knowledge" in detail
        assert (await c.get("/api/channels/nope")).status_code == 404


async def test_the_user_list_is_for_admins_only(hub: Hub) -> None:
    async with hub.client() as c:
        await hub.register(c, "plain-approver", "approver")
        assert (await c.get("/api/admin/users")).status_code == 403
    async with hub.client() as c:
        await hub.register(c, "the-admin", "admin")
        users = (await c.get("/api/admin/users")).json()
        assert any(u["username"] == "the-admin" and u["passkeys"] == 1 for u in users)
        assert all("public_key" not in u for u in users)


async def test_console_views_need_a_session(hub: Hub) -> None:
    async with hub.client() as c:
        for path in ("/api/overview", "/api/pipeline", "/api/channels"):
            assert (await c.get(path)).status_code == 401
