"""OpenAPI contract and structural security checks for the approval hub."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.contract import SPEC
from ytcore.approvals.service import cli
from ytcore.approvals.service.settings import HubSettings

ROOT = Path(__file__).resolve().parents[2]
READS = {
    "/api/auth/me",
    "/api/controls",
    "/api/alerts",
    "/api/jobs",
    "/api/jobs/{job_id}",
    "/api/jobs/{job_id}/assets/{name}",
    "/api/knowledge/drafts",
    "/api/knowledge/drafts/{doc_id}",
    "/api/channels/{channel_id}/knowledge",
    "/api/channels/{channel_id}/inputs",
}


def test_committed_openapi_spec_is_current() -> None:
    assert cli.main(["export-openapi", "--check"]) == 0


def test_no_state_change_through_get() -> None:
    gets = {p for p, ops in SPEC["paths"].items() if "get" in ops}
    assert gets == READS
    for path, ops in SPEC["paths"].items():
        assert set(ops) <= {"get", "post"}, path
        if path not in READS:
            assert set(ops) == {"post"}, path


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs/{job_id}/approve",
        "/api/jobs/{job_id}/edit",
        "/api/jobs/{job_id}/reject",
        "/api/jobs/batch-approve",
    ],
)
def test_decisions_require_the_content_hash_the_reviewer_saw(path: str) -> None:
    ref = SPEC["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
    schema = SPEC["components"]["schemas"][ref["$ref"].split("/")[-1]]
    if "items" in schema["properties"]:
        item_ref = schema["properties"]["items"]["items"]["$ref"].split("/")[-1]
        schema = SPEC["components"]["schemas"][item_ref]
    assert "content_hash" in schema["required"]
    assert schema["properties"]["content_hash"]["pattern"] == "^sha256:[0-9a-f]{64}$"


def test_private_key_never_reaches_gateway_store_workflow_or_agents() -> None:
    """Only the hub (ytcore/approvals/service), keys.py and token.py may touch signing keys."""
    forbidden_names = {"ApprovalSigner", "KeyProvider", "Ed25519PrivateKey", "LocalFileKeyProvider",
                       "KmsKeyProvider", "InMemoryKeyProvider"}  # fmt: skip
    allowed = {ROOT / "ytcore/approvals/keys.py", ROOT / "ytcore/approvals/token.py"}
    offenders = []
    for path in (ROOT / "ytcore").rglob("*.py"):
        if path in allowed or (ROOT / "ytcore/approvals/service") in path.parents:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "ytcore.approvals.keys":
                offenders.append(f"{path.relative_to(ROOT)} imports approvals.keys")
            if isinstance(node, ast.alias) and node.name in forbidden_names:
                offenders.append(f"{path.relative_to(ROOT)} imports {node.name}")
    assert offenders == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "http://hub.example.com"),
        ("cookie_secure", False),
        ("key_backend", "local_file"),
        ("signaler_backend", "fake"),
        ("notifier_backend", "fake"),
        ("rp_id", "evil.example"),
    ],
)
def test_production_hub_settings_refuse_dev_shortcuts(field: str, value: object) -> None:
    ok = {
        "ENV": "production",
        "origin": "https://hub.example.com",
        "rp_id": "hub.example.com",
        "cookie_secure": True,
        "key_backend": "kms",
        "kms_key_resource": "projects/p/locations/l/keyRings/r/cryptoKeys/k",
        "signaler_backend": "temporal",
        "notifier_backend": "slack",
    }
    HubSettings(**ok)
    with pytest.raises(ValidationError):
        HubSettings(**{**ok, field: value})


@pytest.mark.parametrize(
    "path", ["/api/knowledge/drafts/{doc_id}/approve", "/api/knowledge/drafts/{doc_id}/reject"]
)
def test_knowledge_decisions_require_the_draft_hash_the_reviewer_saw(path: str) -> None:
    ref = SPEC["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
    schema = SPEC["components"]["schemas"][ref["$ref"].split("/")[-1]]
    assert "content_sha256" in schema["required"]
    assert schema["properties"]["content_sha256"]["pattern"] == "^sha256:[0-9a-f]{64}$"


def test_knowledge_approval_lives_only_in_the_hub() -> None:
    """Agents and the knowledge package have no way to approve knowledge."""
    for path in [*(ROOT / "ytcore" / "knowledge").rglob("*.py"),
                 *(ROOT / "ytcore" / "agents").rglob("*.py")]:  # fmt: skip
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("ytcore.approvals.service"), path
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "UPDATE knowledge_docs" not in node.value, path
                assert "approved_by = " not in node.value, path
