from __future__ import annotations

from pathlib import Path

import pytest

from ytcore.secrets import (
    CloudSecretsManagerProvider,
    EnvFileSecretsProvider,
    InMemorySecretsProvider,
    ProductionForbiddenError,
    SecretRef,
    SecretsError,
)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env.local"
    path.write_text("# comment\nYT_REFRESH_TOKEN='dummy-value'\nOTHER=x\n")
    return path


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENV", raising=False)


def test_env_file_provider_reads_refs(env_file: Path) -> None:
    p = EnvFileSecretsProvider(env_file, env="dev")
    assert p.get(SecretRef("env://YT_REFRESH_TOKEN")).reveal() == "dummy-value"
    assert p.get(SecretRef("vault://channels/a/youtube#YT_REFRESH_TOKEN")).reveal() == "dummy-value"


def test_env_file_provider_refuses_production_argument(env_file: Path) -> None:
    with pytest.raises(ProductionForbiddenError):
        EnvFileSecretsProvider(env_file, env="production")


def test_env_file_provider_refuses_production_process_env(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENV", "Production")
    with pytest.raises(ProductionForbiddenError):
        EnvFileSecretsProvider(env_file, env="dev")


def test_in_memory_provider_refuses_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(ProductionForbiddenError):
        InMemorySecretsProvider()


def test_secret_value_is_masked(env_file: Path) -> None:
    value = EnvFileSecretsProvider(env_file, env="dev").get(SecretRef("env://YT_REFRESH_TOKEN"))
    assert "dummy-value" not in repr(value)
    assert "dummy-value" not in str(value)
    assert "dummy-value" not in f"{value}"


def test_missing_secret_error_does_not_leak(env_file: Path) -> None:
    with pytest.raises(SecretsError) as exc:
        EnvFileSecretsProvider(env_file, env="dev").get(SecretRef("env://NOPE"))
    assert "dummy-value" not in str(exc.value)


@pytest.mark.parametrize("bad", ["literal-token", "https://x", "vault://", "env://a b"])
def test_secret_ref_rejects_non_references(bad: str) -> None:
    with pytest.raises(SecretsError):
        SecretRef(bad)


class _FakeSecretManager:
    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets, self.asked = secrets, []

    def access_secret_version(self, name: str) -> object:
        self.asked.append(name)
        sid = name.split("/secrets/")[1].split("/")[0]
        if sid not in self.secrets:
            raise LookupError("NOT_FOUND with secret detail")
        data = self.secrets[sid].encode()
        return type("R", (), {"payload": type("P", (), {"data": data})()})()


def test_cloud_provider_only_accepts_vault_refs() -> None:
    p = CloudSecretsManagerProvider(project="yt-prod", client=_FakeSecretManager({}))
    with pytest.raises(SecretsError):
        p.get(SecretRef("env://X"))


def test_cloud_provider_reads_latest_version_and_json_fields() -> None:
    fake = _FakeSecretManager(
        {"database-url": "postgresql://h/db", "channels-a-youtube": '{"refresh_token": "rt"}'}
    )
    p = CloudSecretsManagerProvider(project="yt-prod", client=fake)
    assert p.get(SecretRef("vault://database-url")).reveal() == "postgresql://h/db"
    assert p.get(SecretRef("vault://channels/a/youtube#refresh_token")).reveal() == "rt"
    assert fake.asked[0] == "projects/yt-prod/secrets/database-url/versions/latest"
    with pytest.raises(SecretsError, match="no field"):
        p.get(SecretRef("vault://channels/a/youtube#missing"))


def test_cloud_provider_errors_never_echo_the_backend_message() -> None:
    p = CloudSecretsManagerProvider(project="yt-prod", client=_FakeSecretManager({}))
    with pytest.raises(SecretsError) as exc:
        p.get(SecretRef("vault://nope"))
    assert "secret detail" not in str(exc.value) and "LookupError" in str(exc.value)
