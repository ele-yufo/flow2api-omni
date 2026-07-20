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
