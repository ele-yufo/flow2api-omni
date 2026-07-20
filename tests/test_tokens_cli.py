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


def test_emit_error_prints_json_error_envelope_to_stderr_and_returns_exit_code(capsys):
    """Errors go to stderr (spec Sec 7.1) so stdout stays pure result/phase JSON."""
    from scripts.tokens import ExitCode, emit_error

    rc = emit_error("not_found", "token 9 not found", {"token_id": 9}, exit_code=ExitCode.NOT_FOUND)
    assert rc == int(ExitCode.NOT_FOUND)
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload == {
        "error": {"code": "not_found", "message": "token 9 not found", "detail": {"token_id": 9}}
    }


def test_emit_json_prints_one_json_line(capsys):
    from scripts.tokens import emit_json

    emit_json({"a": 1, "b": [1, 2, 3]})
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert json.loads(out) == {"a": 1, "b": [1, 2, 3]}


def test_main_keyboard_interrupt_emits_json_error_on_stderr_and_returns_internal(monkeypatch, capsys):
    """KeyboardInterrupt must still emit parseable JSON and a documented exit code."""
    from scripts.tokens import ExitCode, main

    def _raise_keyboard_interrupt(coro):
        coro.close()  # never awaited -- close it to avoid a "never awaited" warning
        raise KeyboardInterrupt

    monkeypatch.setattr("scripts.tokens.asyncio.run", _raise_keyboard_interrupt)
    rc = main(["status"])
    assert rc == int(ExitCode.INTERNAL)
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "interrupted"


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
    """The one token here is business-active but keepalive-disabled by default, so
    it must be absent from ``tokens`` -- see
    ``test_status_lists_keepalive_disabled_token_in_excluded_not_in_tokens`` for
    where it does surface.
    """
    from scripts.tokens import ExitCode, _cmd_status

    db, _token_id = _make_db_with_token(tmp_path, keepalive_enabled=False)
    rc = asyncio.run(_cmd_status(_ns(token_id=None), db))
    assert rc == int(ExitCode.OK)
    assert json.loads(capsys.readouterr().out)["tokens"] == []


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
    assert row["business_active"] is True
    assert "business_enabled" not in row  # CLI-facing name is business_active everywhere
    assert row["health"] in {"HEALTHY", "UNHEALTHY", "PROBE_ERROR"}
    assert "st" not in row and "at" not in row and "cookie" not in row
    assert payload["excluded_keepalive_disabled"] == []


def test_status_filters_by_token_id(tmp_path, capsys):
    from scripts.tokens import ExitCode, _cmd_status

    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=True)
    rc = asyncio.run(_cmd_status(_ns(token_id=token_id + 999), db))
    assert rc == int(ExitCode.OK)
    assert json.loads(capsys.readouterr().out) == {"tokens": [], "excluded_keepalive_disabled": []}


def test_status_lists_keepalive_disabled_token_in_excluded_not_in_tokens(tmp_path, capsys):
    """Finding 2: a business-active token whose keepalive got disabled must not
    silently vanish from ``status`` -- it should surface under
    ``excluded_keepalive_disabled`` instead, credential-free, and NOT in ``tokens``.
    """
    from scripts.tokens import ExitCode, _cmd_status

    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=False, is_active=True)
    rc = asyncio.run(_cmd_status(_ns(token_id=None), db))
    assert rc == int(ExitCode.OK)
    payload = json.loads(capsys.readouterr().out)
    assert payload["tokens"] == []
    assert len(payload["excluded_keepalive_disabled"]) == 1
    excluded = payload["excluded_keepalive_disabled"][0]
    assert excluded == {
        "token_id": token_id, "email": "alice@example.com", "is_active": 1, "ban_reason": None,
    }
    assert "st" not in excluded and "at" not in excluded


def test_status_excluded_keepalive_disabled_respects_token_id_filter(tmp_path, capsys):
    from scripts.tokens import ExitCode, _cmd_status

    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=False, is_active=True)
    rc = asyncio.run(_cmd_status(_ns(token_id=token_id + 999), db))
    assert rc == int(ExitCode.OK)
    payload = json.loads(capsys.readouterr().out)
    assert payload["excluded_keepalive_disabled"] == []


def test_status_missing_db_file_returns_db_missing_error(tmp_path, capsys):
    """Finding 5: a not-yet-created DB must surface a clear ``db_missing`` error
    on stderr with exit code NOT_FOUND, not a generic internal-error crash.
    """
    from scripts.tokens import ExitCode, _cmd_status
    from src.core.database import Database

    db = Database(db_path=str(tmp_path / "does-not-exist.db"))
    rc = asyncio.run(_cmd_status(_ns(token_id=None), db))
    assert rc == int(ExitCode.NOT_FOUND)
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "db_missing"


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


# ---------------------------------------------------------------------------
# Task 6: onboard subcommand — phased JSON + OnboardError -> exit-code mapping
# ---------------------------------------------------------------------------


def _fake_runtime(tmp_path: Path):
    from scripts.setup_keepalive_profile import SetupRuntime

    return SetupRuntime(profile_base=tmp_path, proxy="", browser_executable=tmp_path / "chrome")


def _fake_publish_outcome(token_id=25):
    from src.core.repositories.token_lifecycle_repository import PublishOutcome

    return PublishOutcome(
        token_id=token_id,
        membership_status="active",
        pool_transition=None,
        business_active=True,
        ban_reason=None,
        keepalive_enabled=True,
        runtime_mode="persistent",
        profile_state="ready",
    )


def test_onboard_dry_run_new_account_does_not_call_onboard_new(tmp_path, capsys, monkeypatch):
    from scripts.tokens import ExitCode, _cmd_onboard

    async def _must_not_run(**kw):
        raise AssertionError("onboard_new must not run under --dry-run")

    monkeypatch.setattr("scripts.tokens.onboard_new", _must_not_run)
    args = _ns(email="new@example.com", token_id=None, display=None, dry_run=True)
    rc = asyncio.run(_cmd_onboard(args, db=object(), flow_client=object(), runtime=_fake_runtime(tmp_path), display=":11"))
    assert rc == int(ExitCode.OK)
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["would_do"][0]["action"] == "onboard_new"
    assert payload["would_do"][0]["target"] == "new@example.com"


def test_onboard_dry_run_existing_account_does_not_call_onboard_existing(tmp_path, capsys, monkeypatch):
    from scripts.tokens import ExitCode, _cmd_onboard

    async def _must_not_run(**kw):
        raise AssertionError("onboard_existing must not run under --dry-run")

    monkeypatch.setattr("scripts.tokens.onboard_existing", _must_not_run)
    args = _ns(email=None, token_id=7, display=None, dry_run=True)
    rc = asyncio.run(_cmd_onboard(args, db=object(), flow_client=object(), runtime=_fake_runtime(tmp_path), display=":11"))
    assert rc == int(ExitCode.OK)
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["would_do"][0]["action"] == "onboard_existing"
    assert payload["would_do"][0]["target"] == 7


def test_onboard_new_account_emits_awaiting_login_then_published(tmp_path, capsys, monkeypatch):
    from scripts.tokens import ExitCode, _cmd_onboard

    captured = {}

    async def fake_onboard_new(**kw):
        captured.update(kw)
        return _fake_publish_outcome(token_id=25)

    monkeypatch.setattr("scripts.tokens.onboard_new", fake_onboard_new)
    args = _ns(email="new@example.com", token_id=None, display=None, dry_run=False)
    rc = asyncio.run(_cmd_onboard(args, db=object(), flow_client=object(), runtime=_fake_runtime(tmp_path), display=":11"))
    assert rc == int(ExitCode.OK)
    assert captured["email"] == "new@example.com"
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["phase"] == "awaiting_login"
    assert lines[0]["target"] == "new@example.com"
    assert lines[-1] == {
        "phase": "published", "token_id": 25, "membership_status": "active",
        "pool_transition": None, "business_active": True, "ban_reason": None,
        "keepalive_enabled": True, "runtime_mode": "persistent", "profile_state": "ready",
    }


def test_onboard_existing_account_calls_onboard_existing_not_onboard_new(tmp_path, capsys, monkeypatch):
    from scripts.tokens import ExitCode, _cmd_onboard

    async def _must_not_run(**kw):
        raise AssertionError("onboard_new must not run for a --token-id relogin")

    captured = {}

    async def fake_onboard_existing(**kw):
        captured.update(kw)
        return _fake_publish_outcome(token_id=7)

    monkeypatch.setattr("scripts.tokens.onboard_new", _must_not_run)
    monkeypatch.setattr("scripts.tokens.onboard_existing", fake_onboard_existing)
    args = _ns(email=None, token_id=7, display=None, dry_run=False)
    rc = asyncio.run(_cmd_onboard(args, db=object(), flow_client=object(), runtime=_fake_runtime(tmp_path), display=":11"))
    assert rc == int(ExitCode.OK)
    assert captured["token_id"] == 7


@pytest.mark.parametrize(
    ("code", "expected_exit"),
    [
        ("onboard_busy", "BUSY"),
        ("profile_busy", "BUSY"),
        ("not_found", "NOT_FOUND"),
        ("publish_failed", "PUBLISH_FAILED"),
        ("cookie_missing", "VALIDATION_FAILED"),
        ("identity_mismatch", "VALIDATION_FAILED"),
        ("project_pool_failed", "VALIDATION_FAILED"),
        ("browser_launch", "VALIDATION_FAILED"),
        ("login_timeout", "VALIDATION_FAILED"),
        ("browser_crashed", "VALIDATION_FAILED"),
        ("invalid_account", "VALIDATION_FAILED"),
        ("session_rejected", "VALIDATION_FAILED"),
        ("session_error", "VALIDATION_FAILED"),
        ("grant_expired", "VALIDATION_FAILED"),
        ("credits_error", "VALIDATION_FAILED"),
    ],
)
def test_onboard_error_codes_map_to_stable_exit_codes(tmp_path, capsys, monkeypatch, code, expected_exit):
    from scripts.tokens import ExitCode, _cmd_onboard
    from src.services.tokens.onboard import OnboardError

    async def fake_onboard_new(**kw):
        raise OnboardError(code, f"boom {code}")

    monkeypatch.setattr("scripts.tokens.onboard_new", fake_onboard_new)
    args = _ns(email="new@example.com", token_id=None, display=None, dry_run=False)
    rc = asyncio.run(_cmd_onboard(args, db=object(), flow_client=object(), runtime=_fake_runtime(tmp_path), display=":11"))
    assert rc == int(getattr(ExitCode, expected_exit))
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[-1] == {"phase": "failed", "error": {"code": code, "message": f"boom {code}"}}


# ---------------------------------------------------------------------------
# Task 6: _dispatch routing (real Database() construction is inert; --dry-run
# short-circuits every write subcommand before any I/O, so this is safe to run
# against the CLI's real default DB path without touching it).
# ---------------------------------------------------------------------------


def test_dispatch_routes_enable_dry_run():
    from scripts.tokens import ExitCode, build_parser, _dispatch

    args = build_parser().parse_args(["enable", "--token-id", "1", "--dry-run"])
    assert asyncio.run(_dispatch(args)) == int(ExitCode.OK)


def test_dispatch_routes_disable_dry_run():
    from scripts.tokens import ExitCode, build_parser, _dispatch

    args = build_parser().parse_args(["disable", "--token-id", "1", "--dry-run"])
    assert asyncio.run(_dispatch(args)) == int(ExitCode.OK)


def test_dispatch_routes_keepalive_dry_run():
    from scripts.tokens import ExitCode, build_parser, _dispatch

    args = build_parser().parse_args(["keepalive", "--token-id", "1", "on", "--dry-run"])
    assert asyncio.run(_dispatch(args)) == int(ExitCode.OK)


def test_dispatch_routes_onboard_dry_run():
    from scripts.tokens import ExitCode, build_parser, _dispatch

    args = build_parser().parse_args(
        ["onboard", "--email", "new@example.com", "--display", ":99", "--dry-run"]
    )
    assert asyncio.run(_dispatch(args)) == int(ExitCode.OK)


def test_cli_enable_dry_run_via_subprocess_prints_json_and_exits_0():
    r = _run(["enable", "--token-id", "1", "--dry-run"])
    assert r.returncode == 0
    assert json.loads(r.stdout)["dry_run"] is True


def test_cli_onboard_dry_run_via_subprocess_prints_json_and_exits_0():
    r = _run(["onboard", "--email", "new@example.com", "--display", ":99", "--dry-run"])
    assert r.returncode == 0
    assert json.loads(r.stdout)["dry_run"] is True
