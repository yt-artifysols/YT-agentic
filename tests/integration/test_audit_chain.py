"""Concurrent audit writers must keep the hash chain verifiable (id order == chain order)."""

from __future__ import annotations

import threading
import time

import psycopg
import pytest

pytestmark = pytest.mark.integration

LOCK = "SELECT pg_advisory_xact_lock(hashtext('audit_events_chain'))"
INSERT = "INSERT INTO audit_events (actor, action, subject) VALUES ('t', %s, 'race')"


def test_concurrent_audit_inserts_keep_the_chain_intact(database: str) -> None:
    holder = psycopg.connect(database)
    waiter = psycopg.connect(database, autocommit=True)
    try:
        holder.execute(LOCK)  # writer B holds the chain lock
        blocked = threading.Thread(target=lambda: waiter.execute(INSERT, ("first",)))
        blocked.start()  # writer A starts its insert and must wait for the lock
        with psycopg.connect(database, autocommit=True) as probe:
            for _ in range(100):
                waiting = probe.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                ).fetchone()
                if waiting and waiting[0]:
                    break
                time.sleep(0.05)
        holder.execute(INSERT, ("second",))
        holder.commit()
        blocked.join(timeout=10)
        assert not blocked.is_alive()
        with psycopg.connect(database, autocommit=True) as check:
            row = check.execute("SELECT audit_chain_first_break()").fetchone()
            assert row == (None,)
    finally:
        holder.close()
        waiter.close()
