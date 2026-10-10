"""Secrets access. Refresh tokens and API keys are ALWAYS read through a SecretsProvider.

Code, config and logs only ever carry SecretRef values (references like
"vault://channels/chan-a/youtube#refresh_token"), never the secret material itself.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

_REF_RE = re.compile(r"^(vault|env)://[A-Za-z0-9_./-]+(#[A-Za-z0-9_.-]+)?$")
PRODUCTION = "production"


class SecretsError(Exception):
    pass


class ProductionForbiddenError(SecretsError):
    """A dev-only provider was used in production."""


@dataclass(frozen=True, slots=True)
class SecretRef:
    ref: str

    def __post_init__(self) -> None:
        if not _REF_RE.match(self.ref):
            raise SecretsError("secret reference must look like vault://path#key or env://NAME")

    @property
    def scheme(self) -> str:
        return self.ref.split("://", 1)[0]

    @property
    def path(self) -> str:
        return self.ref.split("://", 1)[1]


class SecretValue:
    """Wraps secret material so it never leaks through repr/str/logging."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(****)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretValue) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("SecretValue", self._value))


class SecretsProvider(ABC):
    #: True for providers that must never run in production.
    dev_only: bool = False

    @abstractmethod
    def get(self, ref: SecretRef) -> SecretValue: ...


def _current_env(explicit: str | None) -> str:
    return (explicit or os.environ.get("ENV", "")).strip().lower()


class EnvFileSecretsProvider(SecretsProvider):
    """DEV ONLY. Reads KEY=VALUE pairs from a local env file (git-ignored).

    The reference "env://NAME" or "vault://some/path#NAME" resolves to NAME in the file.
    Refuses to run when ENV=production, whether passed explicitly or set in the process env.
    """

    dev_only = True

    def __init__(self, path: Path, *, env: str | None = None) -> None:
        for candidate in (env, os.environ.get("ENV")):
            if candidate is not None and candidate.strip().lower() == PRODUCTION:
                raise ProductionForbiddenError(
                    "EnvFileSecretsProvider is dev-only and refuses to run when ENV=production"
                )
        self._values = self._parse(path) if path.exists() else {}

    @staticmethod
    def _parse(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
        return values

    def get(self, ref: SecretRef) -> SecretValue:
        key = ref.path.split("#", 1)[1] if "#" in ref.path else ref.path
        if key not in self._values:
            raise SecretsError(f"secret not found for {ref.ref}")
        return SecretValue(self._values[key])


class InMemorySecretsProvider(SecretsProvider):
    """Fake for tests. Dev only."""

    dev_only = True

    def __init__(self, values: dict[str, str] | None = None) -> None:
        if _current_env(None) == PRODUCTION:
            raise ProductionForbiddenError("InMemorySecretsProvider refuses ENV=production")
        self._values = dict(values or {})

    def get(self, ref: SecretRef) -> SecretValue:
        if ref.ref not in self._values:
            raise SecretsError(f"secret not found for {ref.ref}")
        return SecretValue(self._values[ref.ref])


class CloudSecretsManagerProvider(SecretsProvider):
    """Stub for a cloud secrets manager (GCP Secret Manager / AWS Secrets Manager / Vault).

    Wire a real client in a later phase. Only vault:// references are accepted.
    """

    dev_only = False

    def __init__(self, *, project: str) -> None:
        if not project:
            raise SecretsError("cloud secrets manager needs a project/namespace")
        self._project = project

    def get(self, ref: SecretRef) -> SecretValue:
        if ref.scheme != "vault":
            raise SecretsError("cloud secrets manager only resolves vault:// references")
        raise NotImplementedError("cloud secrets manager client is not wired yet")
