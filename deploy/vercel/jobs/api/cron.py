"""Vercel entrypoint for the job driver and planner (staging). Holds the approval PUBLIC key
and the yt_worker / yt_ops database logins; it can never sign or record an approval.

Vercel Cron calls these with `Authorization: Bearer $CRON_SECRET`; the approval hub pokes
/api/cron/tick after each decision so a job moves at once."""

from __future__ import annotations

import hmac
import os
from functools import lru_cache

from fastapi import FastAPI, Header, HTTPException

from ytcore.serverless.runtime import JobsRuntime, build_jobs_runtime

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@lru_cache(maxsize=1)
def _runtime() -> JobsRuntime:
    return build_jobs_runtime()


def _authorize(authorization: str | None) -> None:
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        raise HTTPException(503, "CRON_SECRET is not configured")
    if not hmac.compare_digest(authorization or "", f"Bearer {secret}"):
        raise HTTPException(401, "unauthorized")


@app.get("/api/cron/tick")
async def tick(authorization: str | None = Header(default=None)) -> dict[str, object]:
    _authorize(authorization)
    res = await _runtime().driver.tick(max_jobs=25, budget_seconds=200)
    return {
        "examined": res.examined,
        "advanced": len(res.advanced),
        "failed": len(res.failed),
        "locked": res.skipped_locked,
    }


@app.get("/api/cron/plan")
async def plan(authorization: str | None = Header(default=None)) -> dict[str, object]:
    _authorize(authorization)
    planner = _runtime().planner
    if planner is None:
        raise HTTPException(503, "OPS_DATABASE_URL is not configured")
    run = planner.run()
    res = await _runtime().driver.tick(max_jobs=25, budget_seconds=200)  # start on new jobs now
    return {
        "planned": len(run.planned),
        "replanned": len(run.replanned),
        "skipped": len(run.skipped),
        "advanced": len(res.advanced),
    }
