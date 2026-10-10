"""Knowledge base and owner inputs end to end against real Postgres: agent drafts, human-only
approval in the hub (role, stale-draft and hash checks, diff data, audit), owner input intake
through the hub API and `ytctl`."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.contract import validate
from tests.integration.hubkit import Hub, build_hub
from ytcore import cli
from ytcore.inputs.store import PostgresInputs
from ytcore.knowledge.models import KnowledgeKind
from ytcore.knowledge.store import PostgresKnowledge
from ytcore.store.errors import KnowledgeForbidden

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def hub(database: str) -> Hub:
    return build_hub(database)


async def test_agent_draft_needs_a_human_approver(
    hub: Hub, database: str, channel: Callable[..., str], sql: SQL
) -> None:
    chan = channel()
    kb = PostgresKnowledge.from_conninfo(database).channel(chan)
    v1 = kb.propose(KnowledgeKind.BANNED_CLAIMS, "guaranteed income", proposed_by="owner",
                    base_version=None)  # fmt: skip
    draft = kb.propose(
        KnowledgeKind.BANNED_CLAIMS,
        "guaranteed income\ncures any disease",
        proposed_by="agent:qa",
        base_version=None,
    )
    async with hub.client() as op, hub.client() as ap:
        op_csrf = await hub.register(op, "kb-op", "operator")
        ap_csrf = await hub.register(ap, "kb-ap", "approver")

        # The first version needs approval too; approve it so the agent draft can build on it.
        r = await ap.post(f"/api/knowledge/drafts/{v1.id}/approve",
                          json={"content_sha256": v1.content_sha256},
                          headers={"x-csrf-token": ap_csrf})  # fmt: skip
        assert r.status_code == 200, r.text
        # The agent's draft was written without a base version: it is stale now.
        stale = await ap.get(f"/api/knowledge/drafts/{draft.id}")
        assert stale.json()["stale"] is True
        r = await ap.post(f"/api/knowledge/drafts/{draft.id}/approve",
                          json={"content_sha256": draft.content_sha256},
                          headers={"x-csrf-token": ap_csrf})  # fmt: skip
        assert r.status_code == 409

        fresh = kb.propose(
            KnowledgeKind.BANNED_CLAIMS,
            "guaranteed income\ncures any disease",
            proposed_by="agent:qa",
            base_version=1,
        )
        listed = await ap.get("/api/knowledge/drafts", params={"channel": chan})
        validate("/api/knowledge/drafts", "get", listed.json())
        assert {d["id"] for d in listed.json()} == {draft.id, fresh.id}
        detail = await ap.get(f"/api/knowledge/drafts/{fresh.id}")
        validate("/api/knowledge/drafts/{doc_id}", "get", detail.json())
        body = detail.json()
        assert body["proposed_by"] == "agent:qa" and body["stale"] is False
        assert {"op": "add", "text": "cures any disease"} in body["diff"]
        assert body["base_content"] == "guaranteed income"

        # Operators cannot approve; a wrong hash is refused; the approver approves.
        r = await op.post(f"/api/knowledge/drafts/{fresh.id}/approve",
                          json={"content_sha256": fresh.content_sha256},
                          headers={"x-csrf-token": op_csrf})  # fmt: skip
        assert r.status_code == 403
        r = await ap.post(f"/api/knowledge/drafts/{fresh.id}/approve",
                          json={"content_sha256": "sha256:" + "0" * 64},
                          headers={"x-csrf-token": ap_csrf})  # fmt: skip
        assert r.status_code == 409
        r = await ap.post(f"/api/knowledge/drafts/{fresh.id}/approve",
                          json={"content_sha256": fresh.content_sha256},
                          headers={"x-csrf-token": ap_csrf})  # fmt: skip
        assert r.status_code == 200, r.text
        validate("/api/knowledge/drafts/{doc_id}/approve", "post", r.json())
        assert r.json()["approved_by"] == "kb-ap"
        r = await ap.post(f"/api/knowledge/drafts/{draft.id}/reject",
                          json={"content_sha256": draft.content_sha256, "reason": "stale"},
                          headers={"x-csrf-token": ap_csrf})  # fmt: skip
        assert r.status_code == 200 and r.json()["status"] == "rejected"

        current = await op.get(f"/api/channels/{chan}/knowledge")
        assert [(d["kind"], d["version"]) for d in current.json()] == [("banned_claims", 3)]

    snap = kb.latest_approved()
    assert snap.versions() == {"banned_claims": 3}
    assert snap.banned_claims == ["guaranteed income", "cures any disease"]
    audit = sql("SELECT payload->>'by' FROM audit_events WHERE action = 'knowledge.approved' "
                "AND subject = %s ORDER BY id", f"channel:{chan}")  # fmt: skip
    assert audit == [("kb-ap",), ("kb-ap",)]


def test_agents_cannot_approve_even_through_the_hub_repository(
    hub: Hub, database: str, channel: Callable[..., str]
) -> None:
    chan = channel()
    kb = PostgresKnowledge.from_conninfo(database).channel(chan)
    draft = kb.propose(KnowledgeKind.HOOK_LIBRARY, "Stop scrolling", proposed_by="agent:script",
                       base_version=None)  # fmt: skip
    for who in ("agent:script", "nobody"):
        with pytest.raises(KnowledgeForbidden):
            hub.repo.decide_knowledge(draft.id, draft.content_sha256, username=who, approve=True)
    assert kb.latest_approved().versions() == {}


async def test_owner_inputs_through_the_hub(hub: Hub, channel: Callable[..., str]) -> None:
    chan = channel()
    async with hub.client() as op, hub.client() as ap:
        op_csrf = await hub.register(op, "in-op", "operator")
        ap_csrf = await hub.register(ap, "in-ap", "approver")
        idea = {"kind": "idea", "body": "Why compound interest beats timing the market"}
        r = await op.post(f"/api/channels/{chan}/inputs", json=idea,
                          headers={"x-csrf-token": op_csrf})  # fmt: skip
        assert r.status_code == 200, r.text
        validate("/api/channels/{channel_id}/inputs", "post", r.json())
        assert r.json()["status"] == "new" and r.json()["submitted_by"] == "in-op"
        dup = await op.post(f"/api/channels/{chan}/inputs", json=idea,
                            headers={"x-csrf-token": op_csrf})  # fmt: skip
        assert dup.status_code == 400
        bad = await op.post(f"/api/channels/{chan}/inputs", json={"kind": "footage", "body": "x"},
                            headers={"x-csrf-token": op_csrf})  # fmt: skip
        assert bad.status_code == 400
        goal = {"kind": "goal", "body": "audience: new investors; cadence: 3/week"}
        assert (await op.post(f"/api/channels/{chan}/inputs", json=goal,
                              headers={"x-csrf-token": op_csrf})).status_code == 200  # fmt: skip
        r = await ap.post(f"/api/channels/{chan}/inputs", json={"kind": "note", "body": "n"},
                          headers={"x-csrf-token": ap_csrf})  # fmt: skip
        assert r.status_code == 403
        listed = await ap.get(f"/api/channels/{chan}/inputs", params={"status": "new"})
        assert sorted(i["kind"] for i in listed.json()) == ["goal", "idea"]


def test_ytctl_input_and_knowledge(
    database: str, channel: Callable[..., str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chan = channel()
    inputs = PostgresInputs.from_conninfo(database)
    kb = PostgresKnowledge.from_conninfo(database)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x01fake-footage")
    assert cli.main(["input", "add", "--channel", chan, "--kind", "idea", "--text", "Idea A"],
                    inputs=inputs, actor="cli:owner") == 0  # fmt: skip
    footage_args = ["--kind", "footage", "--file", str(clip), "--note", "B-roll of the office"]
    assert cli.main(["input", "add", "--channel", chan, *footage_args],
                    inputs=inputs, actor="cli:owner") == 0  # fmt: skip
    assert cli.main(["input", "add", "--channel", chan, "--kind", "idea", "--text", "Idea A"],
                    inputs=inputs, actor="cli:owner") == 1  # fmt: skip
    items = inputs.list_for(chan)
    footage = next(i for i in items if i.kind == "footage")
    assert footage.uri == clip.resolve().as_uri() and footage.body == "B-roll of the office"
    assert footage.sha256.startswith("sha256:")
    claims = tmp_path / "claims.md"
    claims.write_text("# banned\n- doubles your money\n")
    assert cli.main(["knowledge", "propose", "--channel", chan, "--kind", "banned_claims",
                     "--file", str(claims)], knowledge=kb, actor="cli:owner") == 0  # fmt: skip
    assert "a human approver must approve it" in capsys.readouterr().out
    assert kb.channel(chan).latest_approved().versions() == {}
    assert [d.kind for d in kb.channel(chan).drafts()] == ["banned_claims"]
