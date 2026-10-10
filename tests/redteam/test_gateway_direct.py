"""Red team: calling the gateway directly, and proving it is the only path to the upload API."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.redteam.rig import Rig
from ytcore.gateway.service import PublishRefused

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("token", ["", "x", "a.b", "Bearer abc", "{}"])
def test_direct_call_without_a_valid_token_is_refused(token: str) -> None:
    rig = Rig()
    job, _, _ = rig.scheduled()
    with pytest.raises(PublishRefused):
        rig.service.publish(job, token)
    assert rig.client.uploads == []


def test_valid_signature_but_no_recorded_approval_is_refused() -> None:
    rig = Rig()
    job, chash = rig.awaiting()
    unrecorded = rig.keys.token(job, "chan-a", chash)
    with pytest.raises(PublishRefused):
        rig.service.publish(job, unrecorded)
    assert rig.client.uploads == []


def test_job_that_is_only_approved_not_scheduled_is_refused() -> None:
    rig = Rig()
    job, token, _ = rig.approved()
    with pytest.raises(PublishRefused, match="not scheduled"):
        rig.service.publish(job, token)
    assert rig.client.uploads == []


@pytest.mark.parametrize(
    "setup",
    ["channel_paused", "cluster_paused", "shadow", "global_paused"],
)
def test_kill_switches_and_shadow_mode_block_publish(setup: str) -> None:
    rig = Rig()
    job, token, _ = rig.scheduled()
    ch = rig.backend.channels["chan-a"]
    if setup == "channel_paused":
        ch.status = "paused"
    elif setup == "cluster_paused":
        ch.cluster_status = "paused"
    elif setup == "shadow":
        ch.autonomy_tier = "shadow"
    else:
        rig.backend.global_paused = True
    with pytest.raises(PublishRefused):
        rig.service.publish(job, token)
    assert rig.client.uploads == []


def test_kill_switch_engaged_between_checks_stops_the_upload() -> None:
    rig = Rig()
    job, token, _ = rig.scheduled()
    real_begin = rig.backend.begin_upload

    def begin_then_pause(*a: object, **kw: object):  # type: ignore[no-untyped-def]
        record = real_begin(*a, **kw)  # type: ignore[arg-type]
        rig.backend.global_paused = True  # operator hits the switch mid-publish
        return record

    rig.backend.begin_upload = begin_then_pause  # type: ignore[method-assign]
    with pytest.raises(PublishRefused, match="kill switch"):
        rig.service.publish(job, token)
    assert rig.client.uploads == []


def test_only_the_gateway_touches_the_upload_api_and_it_never_holds_the_private_key() -> None:
    """Architecture scan: nothing outside ytcore/gateway imports the YouTube clients or google
    upload libraries; only the approvals package imports signing and key providers."""
    offenders: list[str] = []
    for path in (ROOT / "ytcore").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "") + "." + a.name for a in node.names]
                names.append(node.module or "")
            for name in names:
                in_gateway = rel.startswith("ytcore/gateway/")
                if (name.startswith(("googleapiclient", "google.oauth2", "google_auth"))) and (
                    not in_gateway
                ):
                    offenders.append(f"{rel} imports {name}")
                if name.endswith("RealYouTubeClient") and not in_gateway:
                    offenders.append(f"{rel} imports {name}")
                if name.startswith("ytcore.approvals.keys") and in_gateway:
                    offenders.append(f"{rel} imports {name}")
    assert offenders == []


def test_gateway_package_never_references_the_signer() -> None:
    for path in (ROOT / "ytcore" / "gateway").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "ApprovalSigner" not in text, path
        assert "approvals.keys" not in text, path
        assert "PRIVATE KEY" not in text, path


def test_signing_key_code_is_only_used_inside_the_approvals_package() -> None:
    for path in (ROOT / "ytcore").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("ytcore/approvals/"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "ApprovalSigner" not in text, rel
        assert "approvals.keys" not in text, rel
