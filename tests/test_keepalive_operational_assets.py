"""Offline tests for the compatibility wrapper, reporter, and systemd unit."""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).parents[1]
GATE_PATH = PROJECT_ROOT / "scripts" / "keepalive_gate_test.py"
PATROL_PATH = PROJECT_ROOT / "scripts" / "keepalive_patrol.py"
SERVICE_PATH = PROJECT_ROOT / "flow2api-keepalive.service"
NOW = datetime(2026, 7, 19, 10, 25, tzinfo=timezone.utc)


def load_script(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_patrol(
    module,
    db_path: Path,
    *,
    now: datetime = NOW,
    active_interval_seconds: int = 1200,
    retired_interval_seconds: int = 43200,
) -> int:
    return module.main(
        ["--db", str(db_path)],
        now=now,
        active_interval_seconds=active_interval_seconds,
        retired_interval_seconds=retired_interval_seconds,
    )


def create_patrol_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tokens (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                is_active BOOLEAN NOT NULL,
                ban_reason TEXT
            );
            CREATE TABLE token_lifecycle (
                token_id INTEGER PRIMARY KEY,
                keepalive_enabled BOOLEAN NOT NULL,
                runtime_mode TEXT NOT NULL,
                profile_state TEXT NOT NULL,
                membership_confirmed_status TEXT NOT NULL,
                last_keepalive_at TIMESTAMP,
                last_keepalive_success_at TIMESTAMP,
                last_keepalive_status TEXT,
                keepalive_failure_count INTEGER NOT NULL,
                next_due_at TIMESTAMP,
                last_failure_code TEXT
            );
            """
        )


def insert_patrol_record(
    path: Path,
    *,
    token_id: int,
    email: str,
    is_active: bool,
    keepalive_enabled: bool,
    membership: str,
    status: str | None,
    failure_code: str | None = None,
    failure_count: int = 0,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO tokens (id, email, is_active, ban_reason) VALUES (?, ?, ?, ?)",
            (token_id, email, is_active, "membership_expired" if not is_active else None),
        )
        connection.execute(
            """
            INSERT INTO token_lifecycle (
                token_id, keepalive_enabled, runtime_mode, profile_state,
                membership_confirmed_status, last_keepalive_at,
                last_keepalive_success_at, last_keepalive_status,
                keepalive_failure_count, next_due_at, last_failure_code
            ) VALUES (?, ?, 'warm', 'ready', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                keepalive_enabled,
                membership,
                "2026-07-19 10:00:00" if status else None,
                "2026-07-19 10:00:00" if status == "success" else None,
                status,
                failure_count,
                "2026-07-19 10:20:00",
                failure_code,
            ),
        )


def test_gate_wrapper_delegates_exactly_to_production_once_path():
    module = load_script(GATE_PATH, "keepalive_gate_wrapper_test")
    calls = []

    result = module.run_gate(23, entrypoint=lambda argv: calls.append(argv) or 7)

    assert result == 7
    assert calls == [["--once", "--token-id", "23"]]
    with pytest.raises(SystemExit):
        module.parse_arguments([])
    with pytest.raises(SystemExit):
        module.parse_arguments(["--token-id", "01"])


def test_gate_wrapper_contains_no_duplicate_credentials_or_browser_logic():
    source = GATE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "ruby",
        "ruby-test",
        "DB_PATH",
        "DEFAULT_PROXY",
        "--st",
        "--headless",
        "st_to_at",
        "get_credits",
        "browser_cookie3",
        "nodriver",
        "下一步",
    )
    for value in forbidden:
        assert value not in source
    assert '["--once", "--token-id"' in source


def test_patrol_reads_enabled_accounts_even_when_business_disabled_or_retired(tmp_path):
    module = load_script(PATROL_PATH, "keepalive_patrol_query_test")
    db_path = tmp_path / "patrol.db"
    create_patrol_db(db_path)
    insert_patrol_record(
        db_path,
        token_id=1,
        email="active@example.com",
        is_active=True,
        keepalive_enabled=True,
        membership="active",
        status="success",
    )
    insert_patrol_record(
        db_path,
        token_id=2,
        email="retired@example.com",
        is_active=False,
        keepalive_enabled=True,
        membership="retired",
        status="success",
    )
    insert_patrol_record(
        db_path,
        token_id=3,
        email="disabled-keepalive@example.com",
        is_active=True,
        keepalive_enabled=False,
        membership="active",
        status="success",
    )

    records = module.read_telemetry(db_path)

    assert [record.token_id for record in records] == [1, 2]
    assert records[1].business_enabled is False
    assert records[1].membership_status == "retired"


def test_patrol_sanitizes_output_and_all_healthy_includes_retired(tmp_path, capsys):
    module = load_script(PATROL_PATH, "keepalive_patrol_healthy_test")
    db_path = tmp_path / "patrol.db"
    create_patrol_db(db_path)
    insert_patrol_record(
        db_path,
        token_id=4,
        email="private.account@example.com",
        is_active=False,
        keepalive_enabled=True,
        membership="retired",
        status="success",
    )

    exit_code = run_patrol(module, db_path)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "HEALTHY" in output
    assert "business=disabled" in output
    assert "membership=retired" in output
    assert "private.account@example.com" not in output
    assert "p***@example.com" in output


def test_patrol_probe_error_and_unhealthy_both_exit_nonzero(tmp_path, capsys):
    module = load_script(PATROL_PATH, "keepalive_patrol_failure_test")
    probe_db = tmp_path / "probe.db"
    create_patrol_db(probe_db)
    insert_patrol_record(
        probe_db,
        token_id=5,
        email="probe@example.com",
        is_active=True,
        keepalive_enabled=True,
        membership="active",
        status="failure",
        failure_code="network",
        failure_count=2,
    )

    assert run_patrol(module, probe_db) == 2
    assert "PROBE_ERROR" in capsys.readouterr().out

    unhealthy_db = tmp_path / "unhealthy.db"
    create_patrol_db(unhealthy_db)
    insert_patrol_record(
        unhealthy_db,
        token_id=6,
        email="unhealthy@example.com",
        is_active=True,
        keepalive_enabled=True,
        membership="active",
        status="failure",
        failure_code="grant_expired",
        failure_count=1,
    )

    assert run_patrol(module, unhealthy_db) == 1
    assert "UNHEALTHY" in capsys.readouterr().out


def test_patrol_missing_or_invalid_telemetry_is_probe_error(tmp_path, capsys):
    module = load_script(PATROL_PATH, "keepalive_patrol_invalid_test")
    db_path = tmp_path / "invalid.db"
    db_path.write_bytes(b"not sqlite")

    assert run_patrol(module, db_path) == 2
    output = capsys.readouterr().out
    assert "PROBE_ERROR" in output
    assert str(db_path) not in output


@pytest.mark.parametrize(
    ("config_module_source", "error_name"),
    [
        (None, "ModuleNotFoundError"),
        (
            'raise ValueError("malformed config at /private/operator/setting.toml")\n',
            "ValueError",
        ),
        (
            "class StructurallyMalformedConfig:\n"
            "    @property\n"
            "    def keepalive_browser_interval_seconds(self):\n"
            "        raw = {'keepalive': '/private/operator/setting.toml'}\n"
            "        return raw.get('keepalive', {}).get('browser_interval_seconds')\n"
            "\n"
            "config = StructurallyMalformedConfig()\n",
            "AttributeError",
        ),
    ],
)
def test_patrol_cli_sanitizes_lazy_config_import_failures(
    tmp_path,
    config_module_source,
    error_name,
):
    runtime_root = tmp_path / "isolated-runtime"
    scripts_directory = runtime_root / "scripts"
    core_directory = runtime_root / "src" / "core"
    scripts_directory.mkdir(parents=True)
    core_directory.mkdir(parents=True)
    (runtime_root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (core_directory / "__init__.py").write_text("", encoding="utf-8")
    if config_module_source is not None:
        (core_directory / "config.py").write_text(
            config_module_source,
            encoding="utf-8",
        )
    runtime_script = scripts_directory / "keepalive_patrol.py"
    shutil.copy2(PATROL_PATH, runtime_script)
    db_path = runtime_root / "operator-data" / "flow.db"
    db_path.parent.mkdir()
    create_patrol_db(db_path)

    completed = subprocess.run(
        [
            str(PROJECT_ROOT / ".venv" / "bin" / "python"),
            str(runtime_script),
            "--db",
            str(db_path),
        ],
        cwd=runtime_root,
        check=False,
        capture_output=True,
        text=True,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode == 2
    assert f"[patrol] PROBE_ERROR telemetry unavailable ({error_name})" in combined
    assert "Traceback" not in combined
    assert str(runtime_root) not in combined
    assert str(db_path) not in combined
    assert "/private/operator/setting.toml" not in combined


def test_patrol_marks_success_overdue_beyond_cadence_grace_unhealthy(tmp_path, capsys):
    module = load_script(PATROL_PATH, "keepalive_patrol_stale_test")
    db_path = tmp_path / "stale.db"
    create_patrol_db(db_path)
    insert_patrol_record(
        db_path,
        token_id=7,
        email="stale@example.com",
        is_active=True,
        keepalive_enabled=True,
        membership="active",
        status="success",
    )

    overdue_now = datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc)
    assert run_patrol(module, db_path, now=overdue_now) == 1
    output = capsys.readouterr().out
    assert "UNHEALTHY" in output
    assert "overdue" in output


def test_patrol_retired_success_uses_retired_cadence_grace(tmp_path, capsys):
    module = load_script(PATROL_PATH, "keepalive_patrol_retired_grace_test")
    db_path = tmp_path / "retired.db"
    create_patrol_db(db_path)
    insert_patrol_record(
        db_path,
        token_id=8,
        email="retired@example.com",
        is_active=False,
        keepalive_enabled=True,
        membership="retired",
        status="success",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE token_lifecycle SET next_due_at = ? WHERE token_id = 8",
            ("2026-07-19 22:00:00",),
        )

    before_retired_grace = datetime(2026, 7, 19, 22, 30, tzinfo=timezone.utc)
    assert run_patrol(module, db_path, now=before_retired_grace) == 0
    assert "HEALTHY" in capsys.readouterr().out


def test_patrol_uses_configured_one_hour_active_cadence(tmp_path, capsys):
    module = load_script(PATROL_PATH, "keepalive_patrol_custom_active_test")
    db_path = tmp_path / "custom-active.db"
    create_patrol_db(db_path)
    insert_patrol_record(
        db_path,
        token_id=10,
        email="custom-active@example.com",
        is_active=True,
        keepalive_enabled=True,
        membership="active",
        status="success",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE token_lifecycle SET next_due_at = ? WHERE token_id = 10",
            ("2026-07-19 11:00:00",),
        )
    config_object = SimpleNamespace(
        keepalive_browser_interval_seconds=3600,
        keepalive_browser_retired_interval_seconds=43200,
    )
    custom_now = datetime(2026, 7, 19, 11, 20, tzinfo=timezone.utc)

    assert module.main(
        ["--db", str(db_path)],
        now=custom_now,
        config_object=config_object,
    ) == 0
    assert "HEALTHY" in capsys.readouterr().out


def test_patrol_lazily_loads_configured_cadence(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = load_script(PATROL_PATH, "keepalive_patrol_lazy_config_test")
    db_path = tmp_path / "lazy-config.db"
    create_patrol_db(db_path)
    insert_patrol_record(
        db_path,
        token_id=12,
        email="lazy-config@example.com",
        is_active=True,
        keepalive_enabled=True,
        membership="active",
        status="success",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE token_lifecycle SET next_due_at = ? WHERE token_id = 12",
            ("2026-07-19 11:00:00",),
        )
    config_object = SimpleNamespace(
        keepalive_browser_interval_seconds=3600,
        keepalive_browser_retired_interval_seconds=43200,
    )
    monkeypatch.setattr(module, "_load_runtime_config", lambda: config_object)
    custom_now = datetime(2026, 7, 19, 11, 20, tzinfo=timezone.utc)

    assert module.main(["--db", str(db_path)], now=custom_now) == 0
    assert "HEALTHY" in capsys.readouterr().out


def test_patrol_supports_injected_custom_retired_cadence(tmp_path, capsys):
    module = load_script(PATROL_PATH, "keepalive_patrol_custom_retired_test")
    db_path = tmp_path / "custom-retired.db"
    create_patrol_db(db_path)
    insert_patrol_record(
        db_path,
        token_id=11,
        email="custom-retired@example.com",
        is_active=False,
        keepalive_enabled=True,
        membership="retired",
        status="success",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE token_lifecycle SET next_due_at = ? WHERE token_id = 11",
            ("2026-07-20 10:00:00",),
        )
    custom_now = datetime(2026, 7, 19, 23, 30, tzinfo=timezone.utc)

    assert run_patrol(
        module,
        db_path,
        now=custom_now,
        retired_interval_seconds=86400,
    ) == 0
    assert "HEALTHY" in capsys.readouterr().out


def test_patrol_conversion_errors_are_sanitized_probe_errors(tmp_path, capsys):
    module = load_script(PATROL_PATH, "keepalive_patrol_conversion_test")
    db_path = tmp_path / "conversion.db"
    create_patrol_db(db_path)
    insert_patrol_record(
        db_path,
        token_id=9,
        email="conversion@example.com",
        is_active=True,
        keepalive_enabled=True,
        membership="active",
        status="success",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE token_lifecycle SET keepalive_failure_count = ? WHERE token_id = 9",
            ("secret-invalid-count",),
        )

    assert run_patrol(module, db_path) == 2
    output = capsys.readouterr().out
    assert "PROBE_ERROR" in output
    assert "secret-invalid-count" not in output
    assert str(db_path) not in output


def test_patrol_is_read_only_and_has_no_parallel_alert_state():
    source = PATROL_PATH.read_text(encoding="utf-8")
    forbidden = (
        "FlowClient",
        "ProxyManager",
        "AlertNotifier",
        "st_to_at",
        "get_credits",
        "rotated_st",
        "keepalive_patrol_state",
        "save_state",
        "send_alert",
        "UPDATE ",
        "INSERT ",
        "DELETE ",
    )
    for value in forbidden:
        assert value not in source
    assert "mode=ro" in source
    assert "keepalive_enabled = 1" in source


def test_systemd_unit_has_safe_runtime_dependencies_and_shutdown():
    source = SERVICE_PATH.read_text(encoding="utf-8")

    assert "Wants=network-online.target" in source
    assert "After=network-online.target xvfb@10.service flow2api.service" in source
    assert "Requires=xvfb@10.service flow2api.service" in source
    assert "ExecStartPre=" in source and "--preflight" in source
    assert "ExecStart=" in source and "--daemon" in source
    assert "Restart=on-failure" in source
    assert "UMask=0077" in source
    assert "KillSignal=SIGTERM" in source
    assert "TimeoutStopSec=" in source
    assert "Environment=DISPLAY=:10" in source
    assert "Environment=DBUS_SESSION_BUS_ADDRESS=" in source
    assert "Environment=XDG_RUNTIME_DIR=" in source
    assert "Environment=BROWSER_EXECUTABLE_PATH=" in source
    assert "EnvironmentFile=-/etc/flow2api-keepalive.env" in source
    assert "FLOW2API_ALERT_WEBHOOK_URL" not in source
    assert "xrdp" not in source.casefold()
