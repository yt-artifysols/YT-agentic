"""The gateway as its own service: `POST /v1/publish {job_id, token}` and nothing else.

This is the only process that holds YouTube credentials and talks to the upload API. The worker
(which handles untrusted content) calls it with a service identity; it cannot reach the upload
API or the channel tokens itself. Every refusal keeps its exact exception name so the worker can
treat it the same way as an in-process call.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ytcore.gateway.auth import Authenticator, Unauthorized
from ytcore.gateway.service import PublishRefused, PublishService, UploadFailed, UploadInProgress
from ytcore.store.errors import QuotaExhausted, StoreError

log = logging.getLogger("ytcore.gateway")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class PublishIn(BaseModel):
    job_id: Annotated[str, Field(pattern=UUID.pattern)]
    token: Annotated[str, Field(min_length=1, max_length=4096)]


class PublishOut(BaseModel):
    job_id: str
    video_id: str
    state: str
    upload_id: str
    duplicate: bool


def create_gateway_app(service: PublishService, authenticator: Authenticator) -> FastAPI:
    app = FastAPI(title="yt-agentic gateway", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/publish", response_model=PublishOut)
    def publish(body: PublishIn, authorization: Annotated[str | None, Header()] = None) -> object:
        try:
            caller = authenticator.authenticate(authorization)
        except Unauthorized:
            raise HTTPException(401, "unauthorized") from None
        try:
            result = service.publish(body.job_id, body.token, actor=f"gateway:{caller}")
        except UploadInProgress as exc:
            return _error(409, exc)
        except PublishRefused as exc:
            return _error(403, exc)
        except QuotaExhausted as exc:
            return _error(429, exc)
        except UploadFailed as exc:
            return _error(502, exc)
        except StoreError as exc:
            return _error(409, exc)
        return PublishOut(
            job_id=result.job_id,
            video_id=result.video_id,
            state=result.state.value,
            upload_id=result.upload_id,
            duplicate=result.duplicate,
        )

    def _error(status: int, exc: Exception) -> JSONResponse:
        # Never echo the token; exception texts here never contain it.
        log.warning("publish refused: %s", type(exc).__name__)
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=status)

    return app
