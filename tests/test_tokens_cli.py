"""tokens.py CLI tests — Agent-facing JSON-only account management.

Task 4 covers the argparse/JSON/exit-code framework. Task 5 adds
status/enable/disable/keepalive subcommand behavior. Task 6 adds the onboard
subcommand's phased JSON output and OnboardError -> exit-code mapping.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.db_fixtures import make_database_with_token

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKENS_PY = str(PROJECT_ROOT / "scripts" / "tokens.py")


def _run(argv):
    return subprocess.run(
        [sys.executable, TOKENS_PY] + argv,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )


# ---------------------------------------------------------------------------
# Task 4: CLI framework — subcommand dispatch, JSON output, exit codes
# ---------------------------------------------------------------------------


def test_no_args_prints_usage_and_exits_2():
    r = _run([])
    assert r.returncode == 2


def test_unknown_subcommand_exits_2():
    r = _run(["bogus"])
    assert r.returncode == 2


def test_status_help_mentions_json():
    """status --help documents that output is JSON-only (Agent-consumable)."""
    r = _run(["status", "--help"])
    assert r.returncode == 0
    assert "json" in r.stdout.lower()


def test_onboard_requires_email_or_token_id():
    """--email/--token-id is a required mutually exclusive group."""
    r = _run(["onboard"])
    assert r.returncode == 2


def test_onboard_rejects_both_email_and_token_id():
    r = _run(["onboard", "--email", "a@example.com", "--token-id", "1"])
    assert r.returncode == 2


def test_enable_requires_token_id():
    r = _run(["enable"])
    assert r.returncode == 2


def test_keepalive_requires_state_choice():
    r = _run(["keepalive", "--token-id", "1", "sideways"])
    assert r.returncode == 2


def test_exit_code_constants():
    from scripts.tokens import ExitCode

    assert ExitCode.OK == 0
    assert ExitCode.ARG_ERROR == 2
    assert ExitCode.NOT_FOUND == 3
    assert ExitCode.CONFLICT == 4
    assert ExitCode.VALIDATION_FAILED == 5
    assert ExitCode.PUBLISH_FAILED == 6
    assert ExitCode.BUSY == 7
    assert ExitCode.INTERNAL == 70


def test_emit_error_prints_json_error_envelope_and_returns_exit_code(capsys):
    from scripts.tokens import ExitCode, emit_error

    rc = emit_error("not_found", "token 9 not found", {"token_id": 9}, exit_code=ExitCode.NOT_FOUND)
    assert rc == int(ExitCode.NOT_FOUND)
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": {"code": "not_found", "message": "token 9 not found", "detail": {"token_id": 9}}
    }


def test_emit_json_prints_one_json_line(capsys):
    from scripts.tokens import emit_json

    emit_json({"a": 1, "b": [1, 2, 3]})
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert json.loads(out) == {"a": 1, "b": [1, 2, 3]}


# ---------------------------------------------------------------------------
# Task 5: status / enable / disable / keepalive subcommands
# ---------------------------------------------------------------------------


def _ns(**kw):
    return type("Args", (), kw)()


def _make_db_with_token(tmp_path, *, keepalive_enabled=False, ban_reason=None, is_active=True):
    """Build a temp DB with one token, optionally keepalive-enabled + persistent."""
    db, _repo, token_id = make_database_with_token(tmp_path, ban_reason=ban_reason, is_active=is_active)
    if keepalive_enabled:
        asyncio.run(db.set_token_desired_state(token_id, keepalive_enabled=True, runtime_mode="persistent"))
    return db, token_id


async def _get_lifecycle(db, token_id):
    return await db.get_token_lifecycle(token_id)


def test_status_emits_empty_tokens_when_none_keepalive_enabled(tmp_path, capsys):
    from scripts.tokens import ExitCode, _cmd_status

    db, _token_id = _make_db_with_token(tmp_path, keepalive_enabled=False)
    rc = asyncio.run(_cmd_status(_ns(token_id=None), db))
    assert rc == int(ExitCode.OK)
    assert json.loads(capsys.readouterr().out) == {"tokens": []}


def test_status_emits_enabled_token_health_without_credentials(tmp_path, capsys):
    from scripts.tokens import ExitCode, _cmd_status

    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=True)
    rc = asyncio.run(_cmd_status(_ns(token_id=None), db))
    assert rc == int(ExitCode.OK)
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["tokens"]) == 1
    row = payload["tokens"][0]
    assert row["token_id"] == token_id
    assert row["email"] == "alice@example.com"
    assert row["runtime_mode"] == "persistent"
    assert row["health"] in {"HEALTHY", "UNHEALTHY", "PROBE_ERROR"}
    assert "st" not in row and "at" not in row and "cookie" not in row


def test_status_filters_by_token_id(tmp_path, capsys):
    from scripts.tokens import ExitCode, _cmd_status

    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=True)
    rc = asyncio.run(_cmd_status(_ns(token_id=token_id + 999), db))
    assert rc == int(ExitCode.OK)
    assert json.loads(capsys.readouterr().out) == {"tokens": []}


def test_enable_dry_run_does_not_write(tmp_path, capsys):
    from scripts.tokens import ExitCode, _cmd_enable

    db, token_id = _make_db_with_token(tmp_path, ban_reason="manual_disabled", is_active=False)
    rc = asyncio.run(_cmd_enable(_ns(token_id=token_id, dry_run=True), db))
    assert rc == int(ExitCode.OK)
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    token = asyncio.run(db.get_token(token_id))
    assert token.is_active == 0  # untouched by dry-run


def test_enable_clears_manual_disabled(tmp_path):
    from scripts.tokens import ExitCode, _cmd_enable

    db, token_id = _make_db_with_token(tmp_path, ban_reason="manual_disabled", is_active=False)
    rc = asyncio.run(_cmd_enable(_ns(token_id=token_id, dry_run=False), db))
    assert rc == int(ExitCode.OK)
    token = asyncio.run(db.get_token(token_id))
    assert token.is_active == 1
    assert token.ban_reason is None


def test_enable_not_found_returns_exit_3(tmp_path):
    from scripts.tokens import ExitCode, _cmd_enable

    db, token_id = _make_db_with_token(tmp_path)
    rc = asyncio.run(_cmd_enable(_ns(token_id=token_id + 999, dry_run=False), db))
    assert rc == int(ExitCode.NOT_FOUND)


def test_disable_dry_run_does_not_write(tmp_path, capsys):
    from scripts.tokens import ExitCode, _cmd_disable

    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=True)
    rc = asyncio.run(_cmd_disable(_ns(token_id=token_id, dry_run=True), db))
    assert rc == int(ExitCode.OK)
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    token = asyncio.run(db.get_token(token_id))
    assert token.is_active == 1  # untouched by dry-run


def test_disable_sets_manual_disabled_and_keeps_keepalive(tmp_path):
    from scripts.tokens import ExitCode, _cmd_disable

    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=True)
    rc = asyncio.run(_cmd_disable(_ns(token_id=token_id, dry_run=False), db))
    assert rc == int(ExitCode.OK)
    token = asyncio.run(db.get_token(token_id))
    assert token.is_active == 0
    assert token.ban_reason == "manual_disabled"
    lifecycle = asyncio.run(_get_lifecycle(db, token_id))
    assert lifecycle.keepalive_enabled == 1  # keepalive continues


def test_disable_not_found_returns_exit_3(tmp_path):
    from scripts.tokens import ExitCode, _cmd_disable

    db, token_id = _make_db_with_token(tmp_path)
    rc = asyncio.run(_cmd_disable(_ns(token_id=token_id + 999, dry_run=False), db))
    assert rc == int(ExitCode.NOT_FOUND)


def test_keepalive_dry_run_does_not_write(tmp_path, capsys):
    from scripts.tokens import ExitCode, _cmd_keepalive

    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=False)
    rc = asyncio.run(_cmd_keepalive(_ns(token_id=token_id, state="on", dry_run=True), db))
    assert rc == int(ExitCode.OK)
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    lifecycle = asyncio.run(_get_lifecycle(db, token_id))
    assert lifecycle.keepalive_enabled == 0  # untouched by dry-run


def test_keepalive_on_sets_keepalive_enabled_one(tmp_path):
    from scripts.tokens import ExitCode, _cmd_keepalive

    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=False)
    rc = asyncio.run(_cmd_keepalive(_ns(token_id=token_id, state="on", dry_run=False), db))
    assert rc == int(ExitCode.OK)
    lifecycle = asyncio.run(_get_lifecycle(db, token_id))
    assert lifecycle.keepalive_enabled == 1
    assert lifecycle.runtime_mode == "persistent"


def test_keepalive_off_sets_keepalive_enabled_zero(tmp_path):
    from scripts.tokens import ExitCode, _cmd_keepalive

    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=True)
    rc = asyncio.run(_cmd_keepalive(_ns(token_id=token_id, state="off", dry_run=False), db))
    assert rc == int(ExitCode.OK)
    lifecycle = asyncio.run(_get_lifecycle(db, token_id))
    assert lifecycle.keepalive_enabled == 0


def test_keepalive_not_found_returns_exit_3(tmp_path):
    from scripts.tokens import ExitCode, _cmd_keepalive

    db, token_id = _make_db_with_token(tmp_path)
    rc = asyncio.run(_cmd_keepalive(_ns(token_id=token_id + 999, state="on", dry_run=False), db))
    assert rc == int(ExitCode.NOT_FOUND)
