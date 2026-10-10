"""Red team: secrets never appear in logs, prompts or git; hostile assets never get through."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from tests.agentkit import SOURCE_TEXT, Kit
from tests.redteam.rig import Rig
from ytcore.gateway.service import PublishRefused
from ytcore.inputs.models import InputKind
from ytcore.safety.assets import AssetRejected, check_asset, check_size
from ytcore.safety.scrub import (
    find_secrets,
    install_log_scrubber,
    scrub,
)
from ytcore.store.errors import InputRejected

ROOT = Path(__file__).resolve().parents[2]

# Built at runtime so this file itself contains no secret-shaped literal.
CANARIES = {
    "anthropic": "sk-ant-" + "a1B2c3D4" * 4,
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "github": "ghp_" + "Z9y8X7w6" * 5,
    "stripe": "sk_live_" + "q1w2e3r4t5y6u7i8",
    "pem": "-----BEGIN "
    + "PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEI"
    + "x" * 30
    + "\n-----END "
    + "PRIVATE KEY-----",
    "jwt": "eyJ" + "h" * 20 + "." + "p" * 20 + "." + "s" * 10,
}


@pytest.fixture
def scrubbed_logs() -> None:
    install_log_scrubber()


@pytest.mark.parametrize("name", sorted(CANARIES))
def test_log_scrubber_removes_secrets_from_messages_args_and_tracebacks(
    name: str, caplog: pytest.LogCaptureFixture, scrubbed_logs: None
) -> None:
    secret = CANARIES[name]
    log = logging.getLogger("redteam")
    with caplog.at_level(logging.DEBUG):
        log.info("plain %s", secret)
        log.warning(f"fstring {secret}")
        try:
            raise RuntimeError(f"boom {secret}")
        except RuntimeError:
            log.exception("failed")
    rendered = caplog.text + "".join(r.getMessage() for r in caplog.records)
    assert secret not in rendered
    assert secret.split("\n")[0][:12] not in rendered or (name == "pem" and "REDACTED" in rendered)


def test_scrub_is_idempotent_and_keeps_ordinary_text() -> None:
    text = "The index fund charged 0.03% fees in 2024."
    assert scrub(text) == text
    once = scrub(f"key {CANARIES['anthropic']}")
    assert scrub(once) == once


def test_secrets_in_source_pages_and_owner_inputs_never_reach_a_prompt() -> None:
    kit = Kit()
    kit.web.pages["https://example.test/index-funds"] = (
        "Index funds explained",
        f"{SOURCE_TEXT} Contact admin, password: hunter2hunter2 and {CANARIES['anthropic']}",
    )
    chan = kit.channel()
    kit.idea(chan, "Why index funds beat stock picking")
    kit.produce(kit.job(chan), chan)
    assert kit.llm.requests
    for req in kit.llm.requests:
        blob = req.system + req.user
        for secret in CANARIES.values():
            assert secret not in blob
        assert "hunter2hunter2" not in blob


def test_secret_in_an_owner_input_is_rejected_at_intake() -> None:
    kit = Kit()
    chan = kit.channel()
    with pytest.raises(InputRejected, match="credential"):
        kit.inputs.add(
            chan, InputKind.IDEA, body=f"use this {CANARIES['github']}", submitted_by="owner"
        )


def test_secret_in_proposed_knowledge_is_rejected() -> None:
    from ytcore.knowledge.models import KnowledgeKind
    from ytcore.store.errors import StoreError

    kit = Kit()
    chan = kit.channel()
    with pytest.raises(StoreError):
        kit.knowledge.channel(chan).propose(
            KnowledgeKind.BRAND_VOICE, CANARIES["aws"], proposed_by="agent:x", base_version=None
        )


def test_no_secret_shaped_string_is_committed_to_the_repo() -> None:
    files = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.split(b"\0")
    hits: list[str] = []
    for raw in filter(None, files):
        path = ROOT / raw.decode()
        if not path.is_file() or path.suffix in {".png", ".ico", ".lock", ".woff2", ".jpg"}:
            continue
        if "node_modules" in path.parts or path.name == "package-lock.json":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        # Documented dev-only placeholders are allow-listed in .gitleaks.toml too.
        text = "\n".join(
            ln for ln in text.splitlines() if "ytcore-dev" not in ln and "dummy-pw" not in ln
        )
        kinds = {m.name for m in find_secrets(text)} - {"assigned-secret", "approval-token"}
        if kinds:
            hits.append(f"{path.relative_to(ROOT)}: {sorted(kinds)}")
    assert hits == []


# ----------------------------------------------------------------------------- assets

HOSTILE = {
    "windows exe": b"MZ\x90\x00" + b"\x00" * 64,
    "elf": b"\x7fELF" + b"\x00" * 64,
    "shell script": b"#!/bin/sh\nrm -rf /\n",
    "zip bomb": b"PK\x03\x04" + b"\x00" * 64,
    "pdf": b"%PDF-1.7 /JS",
    "svg with script": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    "html": b"<!DOCTYPE html><html></html>",
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_disguised_payloads_are_rejected_as_media(name: str) -> None:
    for kind, media_type in (("video", "video/mp4"), ("image", "image/png")):
        with pytest.raises(AssetRejected):
            check_asset(kind, media_type, HOSTILE[name])


def test_oversized_and_wrong_type_assets_are_rejected() -> None:
    with pytest.raises(AssetRejected):
        check_size("image", 21 * 1024 * 1024)
    with pytest.raises(AssetRejected):
        check_asset("video", "application/x-msdownload", b"\x00\x00\x00\x18ftypmp42")
    with pytest.raises(AssetRejected):
        check_asset("nonsense", "video/mp4", b"\x00\x00\x00\x18ftypmp42")


@pytest.mark.parametrize("name", ["windows exe", "elf", "shell script", "html"])
def test_gateway_refuses_a_hostile_file_even_when_its_hash_was_approved(name: str) -> None:
    rig = Rig()
    job, token, _ = rig.scheduled()
    manifest = rig.backend.jobs[job].manifest
    assert manifest is not None
    # An attacker with write access to storage AND the manifest cannot get past the hash...
    digest = manifest["assets"]["video"]
    key = next(k for k in rig.assets.objects if k.endswith(digest.removeprefix("sha256:")))
    payload = HOSTILE[name]
    rig.assets.objects[key] = payload
    with pytest.raises(Exception):  # noqa: B017 - digest mismatch
        rig.service.publish(job, token)
    assert rig.client.uploads == []


def test_gateway_refuses_hostile_bytes_that_match_the_approved_digest() -> None:
    """Even if the approved content itself is a disguised executable, the gateway refuses."""
    from tests.harness import PRODUCTION_STEPS, manifest_for
    from ytcore.approvals.hashing import content_hash

    rig = Rig()
    rig.channel()
    job = rig.backend.create_job("chan-a")
    for state in PRODUCTION_STEPS:
        rig.backend.transition(job, state, actor="t")
    digest = rig.assets.put(rig.backend.storage_prefix("chan-a"), HOSTILE["elf"])
    manifest = manifest_for(job, "chan-a", digest)
    chash = rig.backend.submit_for_approval(job, manifest, actor="t")
    assert chash == content_hash(manifest)
    token = rig.keys.token(job, "chan-a", chash)
    rig.backend.verify_approval(job, token, actor="hub:approver")
    from ytcore.workflow.states import JobState

    rig.backend.transition(job, JobState.SCHEDULED, actor="scheduler")
    with pytest.raises(PublishRefused, match="asset"):
        rig.service.publish(job, token)
    assert rig.client.uploads == []
