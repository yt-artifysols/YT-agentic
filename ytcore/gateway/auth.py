"""Who may call the gateway service: only the worker's service account.

Production authenticates every call with a Google-signed ID token whose audience is the
gateway's own URL and whose email is on the allowed-callers list (Cloud Run IAM `run.invoker`
already restricts who can reach the service; this check is defence in depth and keeps the
service safe if it is ever exposed by mistake). The static token is for tests and dev only.
"""

from __future__ import annotations

import hmac
from typing import Protocol

from ytcore.secrets.provider import ProductionForbiddenError, _current_env


class Unauthorized(Exception):
    pass


class Authenticator(Protocol):
    def authenticate(self, authorization: str | None) -> str:
        """Return the caller's identity or raise Unauthorized."""
        ...


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("missing bearer token")
    return authorization[7:].strip()


class StaticBearerAuthenticator:
    """Dev/test only: one shared token. Refuses ENV=production."""

    def __init__(self, token: str, identity: str = "worker") -> None:
        if _current_env(None) == "production":
            raise ProductionForbiddenError("StaticBearerAuthenticator refuses ENV=production")
        if len(token) < 16:
            raise ValueError("static token too short")
        self._token, self._identity = token, identity

    def authenticate(self, authorization: str | None) -> str:
        if not hmac.compare_digest(_bearer(authorization), self._token):
            raise Unauthorized("bad token")
        return self._identity


class GoogleIdTokenAuthenticator:  # pragma: no cover - needs Google's signing certs
    def __init__(self, audience: str, allowed_emails: frozenset[str]) -> None:
        if not audience or not allowed_emails:
            raise ValueError("audience and at least one allowed caller are required")
        self._audience, self._allowed = audience, allowed_emails

    def authenticate(self, authorization: str | None) -> str:
        from google.auth.transport import requests
        from google.oauth2 import id_token

        try:
            claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
                _bearer(authorization), requests.Request(), audience=self._audience
            )
        except ValueError as exc:
            raise Unauthorized("invalid ID token") from exc
        email = str(claims.get("email", ""))
        if not claims.get("email_verified") or email not in self._allowed:
            raise Unauthorized("caller not allowed")
        return email
