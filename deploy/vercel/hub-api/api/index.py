"""Vercel entrypoint for the approval hub API (staging). Holds the approval PRIVATE key
(YT_HUB_SIGNING_KEY_PEM) and the yt_hub database login; nothing else."""

from ytcore.serverless.runtime import build_hub_app

app = build_hub_app()
