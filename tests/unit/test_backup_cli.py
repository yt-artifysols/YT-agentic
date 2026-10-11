"""pg tool command lines never carry passwords."""

from __future__ import annotations

from ytcore.ops.backup import _pg_connection


def test_password_goes_to_the_environment_not_the_command_line() -> None:
    args, env = _pg_connection("host=db.internal port=5432 user=ytcore password=hunter2 dbname=app")
    assert "hunter2" not in " ".join(args)
    assert env["PGPASSWORD"] == "hunter2"
    assert args == ["-h", "db.internal", "-p", "5432", "-U", "ytcore", "-d", "app"]


def test_uri_form_is_handled_too() -> None:
    args, env = _pg_connection("postgresql://u:s3cret@h:6543/db?sslmode=require")
    assert "s3cret" not in " ".join(args)
    assert env == {"PGPASSWORD": "s3cret", "PGSSLMODE": "require"}
    assert args[-2:] == ["-d", "db"]
