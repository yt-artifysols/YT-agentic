"""Red team: forged, replayed, edited and tampered approvals never reach YouTube."""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from tests.redteam.rig import Rig
from ytcore.approvals.hashing import content_hash, file_digest
from ytcore.gateway.assets import AssetIntegrityError
from ytcore.gateway.service import PublishRefused
from ytcore.store.errors import ApprovalInvalid, ApprovalReplayError
from ytcore.workflow.states import JobState


def test_forged_token_signed_by_unknown_key_is_refused() -> None:
    rig = Rig()
    job, chash = rig.awaiting()
    forged = rig.attacker_token(job, "chan-a", chash)
    with pytest.raises(ApprovalInvalid):
        rig.backend.verify_approval(job, forged, actor="mallory")
    assert rig.backend.get_state(job) == JobState.AWAITING_APPROVAL
    assert rig.client.uploads == []


def test_forged_token_cannot_publish_even_for_a_scheduled_job() -> None:
    rig = Rig()
    job, _, chash = rig.scheduled()
    with pytest.raises(PublishRefused):
        rig.service.publish(job, rig.attacker_token(job, "chan-a", chash))
    assert rig.client.uploads == []


@pytest.mark.parametrize("mangle", ["flip_sig", "flip_body", "strip_sig", "empty", "none_alg"])
def test_mangled_tokens_are_refused(mangle: str) -> None:
    rig = Rig()
    job, token, _ = rig.scheduled()
    body, _, sig = token.partition(".")
    bad = {
        "flip_sig": f"{body}.{sig[:-2]}AA",
        "flip_body": f"{body[:-2]}AA.{sig}",
        "strip_sig": body,
        "empty": "",
        "none_alg": f"{body}.",
    }[mangle]
    with pytest.raises(PublishRefused):
        rig.service.publish(job, bad)
    assert rig.client.uploads == []


def test_replayed_token_is_refused_on_the_same_job() -> None:
    rig = Rig()
    job, token, _ = rig.approved()
    with pytest.raises(ApprovalReplayError):
        rig.backend.verify_approval(job, token, actor="hub:approver")


def test_token_replayed_on_another_job_or_channel_is_refused() -> None:
    rig = Rig()
    _, token, _ = rig.approved("chan-a")
    other, _ = rig.awaiting("chan-a")
    with pytest.raises(ApprovalInvalid):
        rig.backend.verify_approval(other, token, actor="hub:approver")
    foreign, _ = rig.awaiting("chan-b")
    with pytest.raises(ApprovalInvalid):
        rig.backend.verify_approval(foreign, token, actor="hub:approver")
    assert rig.backend.get_state(other) == rig.backend.get_state(foreign)
    assert rig.backend.get_state(other) == JobState.AWAITING_APPROVAL


def test_expired_token_is_refused() -> None:
    rig = Rig()
    job, chash = rig.awaiting()
    stale, _ = rig.keys.signer.issue(
        job_id=job,
        channel_id="chan-a",
        content_hash=chash,
        approver="owner@example.test",
        ttl=timedelta(minutes=5),
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    with pytest.raises(ApprovalInvalid):
        rig.backend.verify_approval(job, stale, actor="hub:approver")


def test_token_for_content_that_was_edited_after_approval_is_refused() -> None:
    rig = Rig()
    job, token, _ = rig.scheduled()
    job_row = rig.backend.jobs[job]
    edited = copy.deepcopy(job_row.manifest)
    assert edited is not None
    edited["metadata"]["title"] = "Totally different video"
    job_row.manifest, job_row.content_hash = edited, content_hash(edited)
    with pytest.raises(PublishRefused):
        rig.service.publish(job, token)
    assert rig.client.uploads == []


def test_manifest_tampered_without_updating_the_hash_is_refused() -> None:
    rig = Rig()
    job, token, _ = rig.scheduled()
    manifest = rig.backend.jobs[job].manifest
    assert manifest is not None
    manifest["metadata"]["description"] = "buy my crypto"  # hash left untouched
    with pytest.raises(PublishRefused, match="manifest"):
        rig.service.publish(job, token)
    assert rig.client.uploads == []


def test_asset_bytes_swapped_after_approval_are_refused() -> None:
    rig = Rig()
    job, token, _ = rig.scheduled()
    manifest = rig.backend.jobs[job].manifest
    assert manifest is not None
    digest = manifest["assets"]["video"]
    key = next(k for k in rig.assets.objects if k.endswith(digest.removeprefix("sha256:")))
    rig.assets.objects[key] = b"\x00\x00\x00\x18ftypmp42" + b"evil" * 10
    with pytest.raises(AssetIntegrityError):
        rig.service.publish(job, token)
    assert rig.client.uploads == []


def test_synthetic_disclosure_cannot_be_dropped_after_approval() -> None:
    rig = Rig()
    job, token, _ = rig.scheduled()
    manifest = rig.backend.jobs[job].manifest
    assert manifest is not None
    manifest["metadata"]["contains_synthetic_media"] = False
    with pytest.raises(PublishRefused):
        rig.service.publish(job, token)
    assert rig.client.uploads == []


def test_approval_cannot_be_recorded_for_a_job_not_awaiting_approval() -> None:
    rig = Rig()
    rig.channel()
    job = rig.backend.create_job("chan-a")
    token = rig.keys.token(job, "chan-a", "sha256:" + "0" * 64)
    with pytest.raises(Exception):  # noqa: B017 - any typed refusal is fine
        rig.backend.verify_approval(job, token, actor="hub:approver")
    assert rig.backend.get_state(job) == JobState.PLANNED


def test_content_digest_helpers_are_collision_checked() -> None:
    assert file_digest(b"a") == "sha256:" + hashlib.sha256(b"a").hexdigest()
    assert file_digest(b"a") != file_digest(b"b")
