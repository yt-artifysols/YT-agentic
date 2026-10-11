"""The worker's view of the gateway: the same `publish(job_id, token)` as PublishService, over
authenticated HTTP. It holds no YouTube credentials and cannot upload by itself."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from ytcore.gateway.service import PublishRefused, PublishResult, UploadFailed, UploadInProgress
from ytcore.store import errors as store_errors
from ytcore.workflow.states import JobState

TokenProvider = Callable[[], str]

_KNOWN: dict[str, type[Exception]] = {
    "PublishRefused": PublishRefused,
    "UploadInProgress": UploadInProgress,
    "UploadFailed": UploadFailed,
    **{
        name: cls
        for name, cls in vars(store_errors).items()
        if isinstance(cls, type) and issubclass(cls, store_errors.StoreError)
    },
}


class RemoteGateway:
    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        *,
        client: httpx.Client | None = None,
        timeout: float = 120.0,
        allow_insecure: bool = False,
    ) -> None:
        if client is None and not allow_insecure and not base_url.startswith("https://"):
            raise ValueError("the gateway URL must be https")
        self._base = base_url.rstrip("/")
        self._token = token_provider
        self._client = client or httpx.Client(timeout=timeout)

    def publish(self, job_id: str, token: str, *, actor: str = "worker") -> PublishResult:
        try:
            res = self._client.post(
                f"{self._base}/v1/publish",
                json={"job_id": job_id, "token": token},
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        except httpx.TransportError as exc:  # network trouble: retryable, nothing was uploaded
            raise ConnectionError(f"gateway unreachable: {type(exc).__name__}") from exc
        if res.status_code == 200:
            body: dict[str, Any] = res.json()
            return PublishResult(
                job_id=body["job_id"],
                video_id=body["video_id"],
                state=JobState(body["state"]),
                upload_id=body["upload_id"],
                duplicate=bool(body["duplicate"]),
            )
        if res.status_code in (401, 403) and "error" not in _json(res):
            raise PublishRefused(f"gateway rejected the caller ({res.status_code})")
        info = _json(res)
        cls = _KNOWN.get(str(info.get("error", "")))
        if cls is not None:
            raise cls(str(info.get("detail", "")))
        # 5xx without a typed refusal: transient, let the workflow retry (the gateway is idempotent)
        raise ConnectionError(f"gateway error {res.status_code}")


def _json(res: httpx.Response) -> dict[str, Any]:
    try:
        data = res.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def google_id_token_provider(audience: str) -> TokenProvider:  # pragma: no cover - needs GCP
    def provide() -> str:
        import google.auth.transport.requests
        from google.oauth2 import id_token

        return str(
            id_token.fetch_id_token(  # type: ignore[no-untyped-call]
                google.auth.transport.requests.Request(), audience
            )
        )

    return provide
