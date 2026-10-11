"""Scheduled tier evaluation: `python -m ytcore.tiers run-once`.

Demotes risky tier-2 channels and creates promotion RECOMMENDATIONS. It never promotes: only an
admin can confirm a recommendation, in the approval hub.
"""

from __future__ import annotations

import argparse
import logging

log = logging.getLogger("ytcore.tiers")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    argparse.ArgumentParser(prog="python -m ytcore.tiers").add_argument("cmd", choices=["run-once"])
    from ytcore.config import build_secrets_provider, load_settings
    from ytcore.safety.scrub import install_log_scrubber
    from ytcore.secrets.provider import SecretRef
    from ytcore.tiers.service import TierService

    install_log_scrubber()
    logging.basicConfig(level=logging.INFO)
    core = load_settings()
    dsn = build_secrets_provider(core).get(SecretRef(core.database_url_ref)).reveal()
    svc = TierService.from_conninfo(dsn)
    for d in svc.evaluate_demotions():
        log.warning("channel %s returned to approve-all: %s", d.channel_id, d.reason)
    for r in svc.evaluate_promotions():
        log.info("channel %s recommended for promotion (id %s)", r.channel_id, r.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
