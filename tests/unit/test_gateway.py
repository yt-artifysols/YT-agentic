from __future__ import annotations

import pytest

from tests.fakes import FakeBackend, InMemoryAssetSource, fake_secrets
from tests.harness import Keys, drive_to_awaiting, make_service
from ytcore.gateway import build_youtube_client
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.gateway.service import (
    PublishRefused,
    PublishService,
    UploadFailed,
    UploadInProgress,
    idempotency_key,
)
from ytcore.store.errors import ApprovalInvalid, ApprovalReplayError, QuotaExhausted
from ytcore.workflow.states import JobState


class World:
    def __init__(self, *, quota: int = 10_000) -> None:
        self.keys = Keys()
        self.backend = FakeBackend(self.keys.verifier, daily_quota_units=quota)
        self.assets = InMemoryAssetSource()
        self.client = FakeYouTubeClient()
        self.service = make_service(
            self.backend, self.keys, self.assets, self.client, fake_secrets()
        )

    def scheduled_job(self, channel: str = "chan-a") -> tuple[str, str]:
        if channel not in self.backend.channels:
            self.backend.add_channel(channel)
        job = self.backend.create_job(channel)
        chash = drive_to_awaiting(self.backend, self.assets, job, channel)
        token = self.keys.token(job, channel, chash)
        self.backend.verify_approval(job, token, actor="test")
        self.backend.transition(job, JobState.SCHEDULED, actor="test")
        return job, token


@pytest.fixture
def world() -> World:
    return World()


def test_publish_goes_live_with_disclosure(world: World) -> None:
    job, token = world.scheduled_job()
    result = world.service.publish(job, token)
    assert result.state == JobState.LIVE
    assert world.backend.get_state(job) == JobState.LIVE
    (video_id, metadata, key) = world.client.uploads[0]
    assert video_id == result.video_id
    assert metadata.contains_synthetic_media is True
    assert key == idempotency_key(job, world.backend.jobs[job].content_hash or "")
    assert world.backend.quota_used["proj-fake"] == 1601


def test_duplicate_publish_returns_same_video(world: World) -> None:
    job, token = world.scheduled_job()
    first = world.service.publish(job, token)
    second = world.service.publish(job, token)
    assert second.video_id == first.video_id and second.duplicate
    assert len(world.client.uploads) == 1


def test_pending_upload_is_never_uploaded_twice(world: World) -> None:
    job, token = world.scheduled_job()
    ctx = world.backend.publish_context(job)
    claims = world.keys.verifier.verify(
        token, job_id=job, channel_id="chan-a", content_hash=ctx.content_hash or ""
    )
    world.backend.begin_upload(
        ctx,
        idempotency_key=idempotency_key(job, ctx.content_hash or ""),
        approval_jti=claims.jti,
        synthetic_disclosure=True,
        quota_units=1600,
        actor="other-attempt",
    )
    with pytest.raises(UploadInProgress):
        world.service.publish(job, token)
    assert world.client.uploads == []


def test_replayed_token_is_rejected_by_the_store(world: World) -> None:
    job, token = world.scheduled_job()
    with pytest.raises(ApprovalReplayError):
        world.backend.verify_approval(job, token, actor="test")


def test_token_for_other_content_is_rejected(world: World) -> None:
    job, _ = world.scheduled_job()
    forged = world.keys.token(job, "chan-a", "sha256:" + "0" * 64)
    with pytest.raises(PublishRefused, match="token rejected"):
        world.service.publish(job, forged)
    assert world.client.uploads == []


def test_valid_signature_without_recorded_approval_is_refused(world: World) -> None:
    job, _ = world.scheduled_job()
    unconsumed = world.keys.token(job, "chan-a", world.backend.jobs[job].content_hash or "")
    with pytest.raises(PublishRefused, match="no recorded approval"):
        world.service.publish(job, unconsumed)
    assert world.client.uploads == []


def test_gateway_without_approval_is_refused(world: World) -> None:
    world.backend.add_channel("chan-a")
    job = world.backend.create_job("chan-a")
    chash = drive_to_awaiting(world.backend, world.assets, job, "chan-a")
    with pytest.raises(PublishRefused):
        world.service.publish(job, "not-a-token")
    with pytest.raises(PublishRefused):
        world.service.publish(job, world.keys.token(job, "chan-a", chash))
    assert world.client.uploads == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "paused"), ("cluster_status", "paused"), ("autonomy_tier", "shadow")],
)
def test_channel_gating_blocks_publish(world: World, field: str, value: str) -> None:
    job, token = world.scheduled_job()
    setattr(world.backend.channels["chan-a"], field, value)
    with pytest.raises(PublishRefused, match=r"not active|autonomy tier"):
        world.service.publish(job, token)
    assert world.client.uploads == []


def test_quota_exhausted_blocks_upload_and_keeps_job_scheduled() -> None:
    w = World(quota=1000)
    job, token = w.scheduled_job()
    with pytest.raises(QuotaExhausted):
        w.service.publish(job, token)
    assert w.client.uploads == []
    assert w.backend.get_state(job) == JobState.SCHEDULED


def test_tampered_manifest_is_refused(world: World) -> None:
    job, token = world.scheduled_job()
    manifest = world.backend.jobs[job].manifest or {}
    manifest["metadata"]["title"] = "changed after approval"
    with pytest.raises(PublishRefused, match="manifest"):
        world.service.publish(job, token)


def test_tampered_asset_bytes_are_refused(world: World) -> None:
    job, token = world.scheduled_job()
    key = next(iter(world.assets.objects))
    world.assets.objects[key] = b"swapped"
    with pytest.raises(Exception, match="digest"):
        world.service.publish(job, token)
    assert world.client.uploads == []


def test_upload_failure_marks_job_failed(world: World) -> None:
    job, token = world.scheduled_job()
    world.client.fail_uploads = True
    with pytest.raises(UploadFailed):
        world.service.publish(job, token)
    assert world.backend.get_state(job) == JobState.FAILED


def test_not_yet_processed_video_stays_published(world: World) -> None:
    job, token = world.scheduled_job()
    world.client.status_after_upload = "uploaded"
    assert world.service.publish(job, token).state == JobState.PUBLISHED


def test_store_rejects_invalid_token(world: World) -> None:
    world.backend.add_channel("chan-a")
    job = world.backend.create_job("chan-a")
    drive_to_awaiting(world.backend, world.assets, job, "chan-a")
    with pytest.raises(ApprovalInvalid):
        world.backend.verify_approval(job, "garbage.token", actor="test")
    assert world.backend.get_state(job) == JobState.AWAITING_APPROVAL


def test_production_refuses_fakes(world: World) -> None:
    with pytest.raises(RuntimeError, match="fake YouTube"):
        PublishService(
            repo=world.backend,
            verifier=world.keys.verifier,
            client=FakeYouTubeClient(),
            assets=world.assets,
            secrets=fake_secrets(),
            env="production",
        )


def test_client_selection_follows_environment() -> None:
    assert build_youtube_client("fake", "dev").is_fake
    with pytest.raises(RuntimeError):
        build_youtube_client("real", "dev")
    with pytest.raises(RuntimeError):
        build_youtube_client("fake", "production")
    assert build_youtube_client("real", "production").is_fake is False
