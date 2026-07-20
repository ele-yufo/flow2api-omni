"""Offline contracts for the browser keepalive operational entry point."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "keepalive_browser.py"


def load_script():
    spec = importlib.util.spec_from_file_location("keepalive_browser_runtime_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_modes_are_mutually_exclusive_and_default_to_daemon():
    module = load_script()

    assert module.parse_arguments([]).mode == "daemon"
    assert module.parse_arguments(["--daemon"]).mode == "daemon"
    assert module.parse_arguments(["--preflight"]).mode == "preflight"
    assert module.parse_arguments(["--once", "--token-id", "23"]).mode == "once"
    assert module.parse_arguments(["--once", "--token-id", "23"]).token_id == 23

    with pytest.raises(SystemExit):
        module.parse_arguments(["--preflight", "--once"])
    with pytest.raises(SystemExit):
        module.parse_arguments(["--daemon", "--preflight"])
    with pytest.raises(SystemExit):
        module.parse_arguments(["--daemon", "--once"])
    with pytest.raises(SystemExit):
        module.parse_arguments(["--token-id", "23"])


def test_cli_rejects_noncanonical_or_nonpositive_token_ids():
    module = load_script()

    for invalid in ("0", "-1", "+1", "01", "1.0", "abc"):
        with pytest.raises(SystemExit):
            module.parse_arguments(["--once", "--token-id", invalid])


def test_enabled_profile_preflight_checks_binding_cookie_db_and_lease(tmp_path):
    module = load_script()
    profile_base = tmp_path / "profiles"
    profile = profile_base / "7"
    cookies = profile / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"sqlite header only; credentials are not read by preflight")
    target = SimpleNamespace(
        id=7,
        email="User@Example.com ",
        verified_email=" user@example.com",
        profile_state="ready",
    )

    assert module.validate_enabled_profile(target, profile_base) == []
    assert (profile_base / ".flow2api-locks" / "7.lock").stat().st_mode & 0o777 == 0o600

    cookies.unlink()
    failures = module.validate_enabled_profile(target, profile_base)
    assert any("Cookies" in failure for failure in failures)

    target.verified_email = "other@example.com"
    failures = module.validate_enabled_profile(target, profile_base)
    assert any("verified identity mismatch" in failure for failure in failures)


def test_enabled_profile_preflight_refuses_busy_service_lease(tmp_path):
    module = load_script()
    from src.services.keepalive.profile import acquire_profile_lease

    profile_base = tmp_path / "profiles"
    cookies = profile_base / "9" / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"offline")
    target = SimpleNamespace(
        id=9,
        email="nine@example.com",
        verified_email="nine@example.com",
        profile_state="ready",
    )

    lease = acquire_profile_lease(profile_base, 9)
    try:
        failures = module.validate_enabled_profile(target, profile_base)
    finally:
        lease.release()

    assert any("service lease is busy" in failure for failure in failures)


def test_preflight_exits_cleanly_without_touching_runtime_when_disabled(capsys):
    module = load_script()

    class Database:
        async def list_keepalive_enabled_tokens(self):
            raise AssertionError("disabled preflight must not query lifecycle state")

    disabled_config = SimpleNamespace(keepalive_browser_enabled=False)
    assert asyncio.run(module.preflight(Database(), config_object=disabled_config)) == 0
    assert "disabled" in capsys.readouterr().out


def test_preflight_success_output_never_exposes_runtime_paths(tmp_path, monkeypatch, capsys):
    module = load_script()
    profile_base = tmp_path / "private-profile-base"
    cookies = profile_base / "23" / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"offline")
    executable = tmp_path / "private-bin" / "chrome"
    executable.parent.mkdir()
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    display_socket = tmp_path / "X10"
    display_socket.write_text("socket", encoding="utf-8")
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", str(executable))
    monkeypatch.setattr(module, "_display_socket", lambda _display: display_socket)
    config_object = SimpleNamespace(
        keepalive_browser_enabled=True,
        keepalive_browser_profile_base=str(profile_base),
        keepalive_browser_display=":10",
    )

    class Database:
        async def list_keepalive_enabled_tokens(self):
            return [SimpleNamespace(
                id=23,
                email="account@example.com",
                verified_email="account@example.com",
                profile_state="ready",
            )]

    assert asyncio.run(module.preflight(Database(), config_object=config_object)) == 0
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "enabled_accounts=1" in combined
    assert config_object.keepalive_browser_display not in combined
    assert str(profile_base) not in combined
    assert str(executable) not in combined


def test_preflight_display_error_never_echoes_raw_config(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = load_script()
    profile_base = tmp_path / "private-profile-base"
    profile_base.mkdir()
    executable = tmp_path / "private-browser" / "chrome"
    executable.parent.mkdir()
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    secret_display_path = tmp_path / "private-display-detail"
    malicious_display = f":10\n{secret_display_path}"
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", str(executable))
    config_object = SimpleNamespace(
        keepalive_browser_enabled=True,
        keepalive_browser_profile_base=str(profile_base),
        keepalive_browser_display=malicious_display,
    )

    class Database:
        async def list_keepalive_enabled_tokens(self):
            return []

    assert asyncio.run(module.preflight(Database(), config_object=config_object)) == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "[keepalive][preflight] ERROR X display unavailable" in combined
    assert malicious_display not in combined
    assert str(secret_display_path) not in combined


def test_preflight_path_errors_are_sanitized(tmp_path, monkeypatch, capsys):
    module = load_script()
    secret_base = tmp_path / "missing-private-profile-base"
    secret_executable = tmp_path / "missing-private-browser"
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", str(secret_executable))
    config_object = SimpleNamespace(
        keepalive_browser_enabled=True,
        keepalive_browser_profile_base=str(secret_base),
        keepalive_browser_display=":999",
    )

    class Database:
        async def list_keepalive_enabled_tokens(self):
            return []

    assert asyncio.run(module.preflight(Database(), config_object=config_object)) == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "browser executable missing or not executable" in combined
    assert "profile base missing" in combined
    assert str(secret_base) not in combined
    assert str(secret_executable) not in combined


def test_preflight_canonical_path_error_never_exposes_external_candidate(tmp_path):
    module = load_script()
    profile_base = tmp_path / "private-profile-base"
    external_profile = tmp_path / "external-customer-profile"
    profile_base.mkdir()
    external_profile.mkdir()
    (profile_base / "23").symlink_to(external_profile, target_is_directory=True)
    target = SimpleNamespace(
        id=23,
        email="account@example.com",
        verified_email="account@example.com",
        profile_state="ready",
    )

    failures = module.validate_enabled_profile(target, profile_base)
    combined = "\n".join(failures)
    assert "invalid token/profile mapping" in combined
    assert str(profile_base) not in combined
    assert str(external_profile) not in combined


def test_preflight_external_lock_directory_error_is_sanitized(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = load_script()
    profile_base = tmp_path / "private-profile-base"
    cookies = profile_base / "23" / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"offline")
    external_lock_directory = tmp_path / "external-customer-locks"
    external_lock_directory.mkdir()
    (profile_base / ".flow2api-locks").symlink_to(
        external_lock_directory,
        target_is_directory=True,
    )
    executable = tmp_path / "private-browser" / "chrome"
    executable.parent.mkdir()
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    display_socket = tmp_path / "X10"
    display_socket.write_text("socket", encoding="utf-8")
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", str(executable))
    monkeypatch.setattr(module, "_display_socket", lambda _display: display_socket)
    config_object = SimpleNamespace(
        keepalive_browser_enabled=True,
        keepalive_browser_profile_base=str(profile_base),
        keepalive_browser_display=":10",
    )

    class Database:
        async def list_keepalive_enabled_tokens(self):
            return [SimpleNamespace(
                id=23,
                email="account@example.com",
                verified_email="account@example.com",
                profile_state="ready",
            )]

    assert asyncio.run(module.preflight(Database(), config_object=config_object)) == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "service lease unavailable" in combined
    assert "ValueError" in combined
    assert str(profile_base) not in combined
    assert str(external_lock_directory) not in combined


def test_preflight_service_lease_runtime_error_is_sanitized(tmp_path, monkeypatch):
    module = load_script()
    profile_base = tmp_path / "private-profile-base"
    cookies = profile_base / "23" / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"offline")
    secret_path = tmp_path / "private-runtime-detail"

    def fail_lease(_profile_base, _token_id):
        raise RuntimeError(f"failed while resolving {secret_path}")

    monkeypatch.setattr(module, "acquire_profile_lease", fail_lease)
    target = SimpleNamespace(
        id=23,
        email="account@example.com",
        verified_email="account@example.com",
        profile_state="ready",
    )

    failures = module.validate_enabled_profile(target, profile_base)
    combined = "\n".join(failures)
    assert "service lease unavailable" in combined
    assert "RuntimeError" in combined
    assert str(profile_base) not in combined
    assert str(secret_path) not in combined


def test_preflight_lock_inspection_oserror_is_sanitized_and_releases_lease(
    tmp_path,
    monkeypatch,
):
    module = load_script()
    from src.services.keepalive.profile import acquire_profile_lease

    profile_base = tmp_path / "private-profile-base"
    cookies = profile_base / "23" / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"offline")
    secret_path = tmp_path / "private-singleton-detail"

    def fail_inspection(_profile_path):
        raise OSError(f"failed while inspecting {secret_path}")

    monkeypatch.setattr(module, "inspect_singleton_lock", fail_inspection)
    target = SimpleNamespace(
        id=23,
        email="account@example.com",
        verified_email="account@example.com",
        profile_state="ready",
    )

    failures = module.validate_enabled_profile(target, profile_base)
    combined = "\n".join(failures)
    assert "SingletonLock inspection unavailable" in combined
    assert "OSError" in combined
    assert str(profile_base) not in combined
    assert str(secret_path) not in combined
    with acquire_profile_lease(profile_base, 23):
        pass


def test_preflight_lease_release_failure_preserves_primary_failure(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = load_script()
    profile_base = tmp_path / "private-profile-base"
    cookies = profile_base / "23" / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"offline")
    inspection_secret = tmp_path / "private-inspection-detail"
    release_secret = tmp_path / "private-release-detail"

    class Lease:
        def release(self):
            raise RuntimeError(f"failed while releasing {release_secret}")

    def fail_inspection(_profile_path):
        raise OSError(f"failed while inspecting {inspection_secret}")

    monkeypatch.setattr(module, "acquire_profile_lease", lambda *_args: Lease())
    monkeypatch.setattr(module, "inspect_singleton_lock", fail_inspection)
    target = SimpleNamespace(
        id=23,
        email="account@example.com",
        verified_email="account@example.com",
        profile_state="ready",
    )

    failures = module.validate_enabled_profile(target, profile_base)
    assert len(failures) == 2
    assert "SingletonLock inspection unavailable" in failures[0]
    assert "OSError" in failures[0]
    assert "service lease release unavailable" in failures[1]
    assert "RuntimeError" in failures[1]
    combined = "\n".join(failures)
    assert str(inspection_secret) not in combined
    assert str(release_secret) not in combined

    executable = tmp_path / "private-browser" / "chrome"
    executable.parent.mkdir()
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    display_socket = tmp_path / "X10"
    display_socket.write_text("socket", encoding="utf-8")
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", str(executable))
    monkeypatch.setattr(module, "_display_socket", lambda _display: display_socket)
    config_object = SimpleNamespace(
        keepalive_browser_enabled=True,
        keepalive_browser_profile_base=str(profile_base),
        keepalive_browser_display=":10",
    )

    class Database:
        async def list_keepalive_enabled_tokens(self):
            return [target]

    assert asyncio.run(module.preflight(Database(), config_object=config_object)) == 1
    output = capsys.readouterr()
    combined_output = output.out + output.err
    assert "SingletonLock inspection unavailable" in combined_output
    assert "service lease release unavailable" in combined_output
    assert str(inspection_secret) not in combined_output
    assert str(release_secret) not in combined_output


@pytest.mark.parametrize("mode", ["--preflight", "--daemon"])
@pytest.mark.parametrize(
    ("exception_source", "error_name"),
    [
        (
            'raise FileNotFoundError("missing /private/operator/setting.toml")',
            "FileNotFoundError",
        ),
        (
            'raise ValueError("malformed /private/operator/setting.toml")',
            "ValueError",
        ),
    ],
)
def test_browser_cli_sanitizes_lazy_config_failures(
    tmp_path,
    mode,
    exception_source,
    error_name,
):
    hook_directory = tmp_path / "private-import-hook"
    hook_directory.mkdir()
    (hook_directory / "sitecustomize.py").write_text(
        "import builtins\n"
        "_original_import = builtins.__import__\n"
        "def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):\n"
        "    if name == 'src.core.config':\n"
        f"        {exception_source}\n"
        "    return _original_import(name, globals, locals, fromlist, level)\n"
        "builtins.__import__ = _guarded_import\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(hook_directory)
    if existing_pythonpath:
        environment["PYTHONPATH"] += os.pathsep + existing_pythonpath

    completed = subprocess.run(
        [
            str(PROJECT_ROOT / ".venv" / "bin" / "python"),
            str(SCRIPT_PATH),
            mode,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert f"runtime initialization failed ({error_name})" in combined
    assert "Traceback" not in combined
    assert str(hook_directory) not in combined
    assert "/private/operator/setting.toml" not in combined


def test_async_main_uses_injected_lazy_config_for_disabled_preflight(
    monkeypatch,
    capsys,
):
    module = load_script()
    disabled_config = SimpleNamespace(keepalive_browser_enabled=False)
    monkeypatch.setattr(
        module,
        "_load_runtime_dependencies",
        lambda: (disabled_config, object),
    )

    assert asyncio.run(module.async_main(["--preflight"])) == 0
    assert "disabled" in capsys.readouterr().out


def test_preflight_profile_validation_never_reads_session_credentials():
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "read_session_token" not in source
    assert "browser_cookie3.chrome" not in source
    assert "st_to_at" not in source


def test_signal_handlers_schedule_supervisor_stop_once():
    module = load_script()

    class Supervisor:
        def __init__(self):
            self.stop_calls = 0
            self.stopped = asyncio.Event()

        async def stop(self):
            self.stop_calls += 1
            self.stopped.set()

    class LoopProxy:
        def __init__(self, loop):
            self.loop = loop
            self.handlers = {}
            self.removed = []

        def add_signal_handler(self, signum, callback):
            self.handlers[signum] = callback

        def remove_signal_handler(self, signum):
            self.removed.append(signum)
            return True

        def create_task(self, coroutine):
            return self.loop.create_task(coroutine)

    async def scenario():
        supervisor = Supervisor()
        loop_proxy = LoopProxy(asyncio.get_running_loop())
        uninstall = module.install_shutdown_handlers(supervisor, loop=loop_proxy)

        loop_proxy.handlers[signal.SIGTERM]()
        loop_proxy.handlers[signal.SIGINT]()
        await asyncio.wait_for(supervisor.stopped.wait(), timeout=1)
        await asyncio.sleep(0)
        uninstall()

        return supervisor, loop_proxy

    supervisor, loop_proxy = asyncio.run(scenario())
    assert supervisor.stop_calls == 1
    assert loop_proxy.removed == [signal.SIGTERM, signal.SIGINT]
