"""Offline safety tests for the operator-facing keepalive profile setup command."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).parents[1]
HELPER_PATH = PROJECT_ROOT / "scripts" / "setup_keepalive_profile.py"
WRAPPER_PATH = PROJECT_ROOT / "scripts" / "setup_keepalive_profile.sh"


def load_helper():
    spec = importlib.util.spec_from_file_location("setup_keepalive_profile_test", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_token_id_and_display_validation_are_strict():
    module = load_helper()

    assert module.canonical_token_id("23") == 23
    for invalid in ("0", "-1", "+1", "01", " 1", "1 ", "1.0", "abc"):
        with pytest.raises(ValueError):
            module.canonical_token_id(invalid)

    assert module.resolve_display(":11", {"DISPLAY": ":99"}) == ":11"
    assert module.resolve_display(None, {"DISPLAY": ":12.0"}) == ":12.0"
    with pytest.raises(ValueError):
        module.resolve_display(None, {})
    with pytest.raises(ValueError):
        module.resolve_display("localhost:11", {})


def test_runtime_uses_configured_profile_proxy_and_browser(tmp_path):
    module = load_helper()
    cfg = SimpleNamespace(
        keepalive_browser_profile_base=str(tmp_path / "configured-profiles"),
        keepalive_browser_proxy="http://127.0.0.1:9999",
    )
    executable = tmp_path / "configured-chrome"
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)

    runtime = module.resolve_runtime(
        cfg,
        {"BROWSER_EXECUTABLE_PATH": str(executable)},
    )

    assert runtime.profile_base == (tmp_path / "configured-profiles").resolve()
    assert runtime.proxy == "http://127.0.0.1:9999"
    assert runtime.browser_executable == executable.resolve()


def test_setup_rejects_proxy_userinfo_before_constructing_browser_command(tmp_path):
    module = load_helper()
    runtime = module.SetupRuntime(
        profile_base=tmp_path / "profiles",
        proxy="http://operator:secret-password@127.0.0.1:7890",
        browser_executable=tmp_path / "chrome",
    )

    with pytest.raises(ValueError, match="must not include userinfo") as error:
        module.build_browser_command(
            runtime,
            (tmp_path / "profiles" / "23").resolve(),
            "https://labs.google/fx/tools/flow",
        )

    assert "operator" not in str(error.value)
    assert "secret-password" not in str(error.value)


def test_setup_holds_service_lease_runs_foreground_then_verifies_identity(tmp_path):
    module = load_helper()
    profile_base = tmp_path / "profiles"
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    runtime = module.SetupRuntime(
        profile_base=profile_base,
        proxy="http://127.0.0.1:7890",
        browser_executable=executable,
    )
    token = SimpleNamespace(
        id=17,
        email="Account@Example.com",
        current_project_id="project-17",
    )
    lifecycle = SimpleNamespace(verified_email="account@example.com")

    class Database:
        async def get_token(self, token_id):
            assert token_id == 17
            return token

        async def get_token_lifecycle(self, token_id):
            assert token_id == 17
            return lifecycle

    events = []
    launched = {}

    def launcher(command, *, env, check, stdout, stderr):
        profile = profile_base / "17"
        lease_path = profile_base / ".flow2api-locks" / "17.lock"
        events.append("launch")
        launched.update(
            command=command,
            env=env,
            check=check,
            stdout=stdout,
            stderr=stderr,
        )
        assert profile.is_dir()
        assert lease_path.exists()
        assert stat.S_IMODE(profile.stat().st_mode) == 0o700
        return SimpleNamespace(returncode=0)

    def session_reader(profile_path):
        events.append("cookie")
        assert profile_path == (profile_base / "17").resolve()
        return "s" * 120

    async def identity_inspector(flow_client, session_token):
        events.append("identity")
        assert flow_client is FLOW_CLIENT
        assert session_token == "s" * 120
        return SimpleNamespace(
            email="account@example.com",
            normalized_email="account@example.com",
            credits=123,
            user_paygate_tier="PAYGATE_TIER_TWO",
        )

    FLOW_CLIENT = object()
    result = asyncio.run(
        module.setup_profile(
            17,
            display=":11",
            runtime=runtime,
            db=Database(),
            flow_client=FLOW_CLIENT,
            launcher=launcher,
            session_reader=session_reader,
            identity_inspector=identity_inspector,
        )
    )

    assert events == ["launch", "cookie", "identity"]
    assert launched["check"] is False
    assert launched["stdout"] is subprocess.DEVNULL
    assert launched["stderr"] is subprocess.DEVNULL
    assert launched["env"]["DISPLAY"] == ":11"
    assert launched["command"][0] == str(executable.resolve())
    assert f"--user-data-dir={(profile_base / '17').resolve()}" in launched["command"]
    assert "--proxy-server=http://127.0.0.1:7890" in launched["command"]
    assert launched["command"][-1].endswith("/project/project-17")
    assert result.token_id == 17
    assert result.email == "account@example.com"
    assert result.credits == 123
    assert not any(argument in ("&", "--headless") for argument in launched["command"])


def test_setup_suppresses_browser_child_stdout_and_stderr(tmp_path, capfd):
    module = load_helper()
    profile_base = tmp_path / "private-customer-profile-base"
    executable = tmp_path / "private-browser" / "chrome"
    executable.parent.mkdir()
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('CHILD_STDOUT ' + ' '.join(sys.argv))\n"
        "print('CHILD_STDERR ' + ' '.join(sys.argv), file=sys.stderr)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    runtime = module.SetupRuntime(profile_base, "", executable)
    token = SimpleNamespace(
        id=19,
        email="account@example.com",
        current_project_id=None,
    )

    class Database:
        async def get_token(self, token_id):
            assert token_id == 19
            return token

        async def get_token_lifecycle(self, token_id):
            assert token_id == 19
            return SimpleNamespace(verified_email="account@example.com")

    async def identity_inspector(_flow_client, session_token):
        assert session_token == "s" * 120
        return SimpleNamespace(
            email="account@example.com",
            normalized_email="account@example.com",
            credits=10,
            user_paygate_tier=None,
        )

    result = asyncio.run(
        module.setup_profile(
            19,
            display=":11",
            runtime=runtime,
            db=Database(),
            flow_client=object(),
            session_reader=lambda _profile: "s" * 120,
            identity_inspector=identity_inspector,
        )
    )

    output = capfd.readouterr()
    combined = output.out + output.err
    assert result.token_id == 19
    assert "CHILD_STDOUT" not in combined
    assert "CHILD_STDERR" not in combined
    assert str(profile_base) not in combined
    assert str(executable) not in combined


def test_setup_refuses_existing_singleton_artifact_without_deleting_it(tmp_path):
    module = load_helper()
    profile_base = tmp_path / "profiles"
    profile = profile_base / "5"
    profile.mkdir(parents=True)
    lock = profile / "SingletonLock"
    lock.write_text("untrusted", encoding="utf-8")
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    runtime = module.SetupRuntime(profile_base, "", executable)
    token = SimpleNamespace(id=5, email="five@example.com", current_project_id=None)

    class Database:
        async def get_token(self, token_id):
            return token

        async def get_token_lifecycle(self, token_id):
            return SimpleNamespace(verified_email=None)

    with pytest.raises(module.SetupSafetyError, match="SingletonLock"):
        asyncio.run(
            module.setup_profile(
                5,
                display=":11",
                runtime=runtime,
                db=Database(),
                flow_client=object(),
                launcher=lambda *args, **kwargs: pytest.fail("Chrome must not launch"),
            )
        )

    assert lock.exists()
    assert lock.read_text(encoding="utf-8") == "untrusted"


def test_setup_refuses_profile_when_service_lease_is_busy(tmp_path):
    module = load_helper()
    from src.services.keepalive.profile import acquire_profile_lease

    profile_base = tmp_path / "profiles"
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    runtime = module.SetupRuntime(profile_base, "", executable)
    token = SimpleNamespace(id=6, email="six@example.com", current_project_id=None)

    class Database:
        async def get_token(self, token_id):
            return token

        async def get_token_lifecycle(self, token_id):
            return SimpleNamespace(verified_email=None)

    lease = acquire_profile_lease(profile_base, 6)
    try:
        with pytest.raises(module.SetupSafetyError, match="service lease"):
            asyncio.run(
                module.setup_profile(
                    6,
                    display=":11",
                    runtime=runtime,
                    db=Database(),
                    flow_client=object(),
                    launcher=lambda *args, **kwargs: pytest.fail("Chrome must not launch"),
                )
            )
    finally:
        lease.release()


def test_setup_rejects_post_exit_identity_mismatch(tmp_path):
    module = load_helper()
    profile_base = tmp_path / "profiles"
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    runtime = module.SetupRuntime(profile_base, "", executable)
    token = SimpleNamespace(id=8, email="expected@example.com", current_project_id=None)

    class Database:
        async def get_token(self, token_id):
            return token

        async def get_token_lifecycle(self, token_id):
            return SimpleNamespace(verified_email="expected@example.com")

    async def wrong_identity(_flow_client, _session_token):
        return SimpleNamespace(
            email="wrong@example.com",
            normalized_email="wrong@example.com",
            credits=0,
            user_paygate_tier=None,
        )

    with pytest.raises(module.SetupValidationError, match="identity mismatch"):
        asyncio.run(
            module.setup_profile(
                8,
                display=":11",
                runtime=runtime,
                db=Database(),
                flow_client=object(),
                launcher=lambda *args, **kwargs: SimpleNamespace(returncode=0),
                session_reader=lambda _profile: "s" * 120,
                identity_inspector=wrong_identity,
            )
        )


def test_setup_cli_success_output_never_exposes_configured_paths(tmp_path, monkeypatch, capsys):
    module = load_helper()
    secret_base = tmp_path / "private-customer-profile-base"
    secret_executable = tmp_path / "private-browser-location" / "chrome"
    runtime = module.SetupRuntime(secret_base, "", secret_executable)
    monkeypatch.setattr(module, "resolve_runtime", lambda _config, _env: runtime)
    monkeypatch.setattr(module, "Database", lambda: object())
    monkeypatch.setattr(module, "ProxyManager", lambda _db: object())
    monkeypatch.setattr(
        module,
        "_load_runtime_dependencies",
        lambda: (object(), lambda _proxy, _db: object()),
    )

    async def successful_setup(*_args, **_kwargs):
        return module.SetupResult(23, "account@example.com", 100, "PAYGATE_TIER_ONE")

    monkeypatch.setattr(module, "setup_profile", successful_setup)

    assert asyncio.run(module.async_main(["23", ":11"])) == 0
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "id=23" in combined
    assert str(secret_base) not in combined
    assert str(secret_executable) not in combined


def test_setup_cli_path_error_is_sanitized(tmp_path, monkeypatch, capsys):
    module = load_helper()
    profile_base = tmp_path / "private-profile-base"
    external_profile = tmp_path / "external-customer-profile"
    profile_base.mkdir()
    external_profile.mkdir()
    (profile_base / "23").symlink_to(external_profile, target_is_directory=True)
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    runtime = module.SetupRuntime(profile_base, "", executable)
    monkeypatch.setattr(module, "resolve_runtime", lambda _config, _env: runtime)
    monkeypatch.setattr(module, "Database", lambda: object())
    monkeypatch.setattr(module, "ProxyManager", lambda _db: object())
    monkeypatch.setattr(
        module,
        "_load_runtime_dependencies",
        lambda: (object(), lambda _proxy, _db: object()),
    )

    assert asyncio.run(module.async_main(["23", ":11"])) == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "profile path validation failed" in combined
    assert str(profile_base) not in combined
    assert str(external_profile) not in combined


def test_setup_cli_external_lock_directory_error_is_sanitized(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = load_helper()
    profile_base = tmp_path / "private-profile-base"
    profile_base.mkdir()
    external_lock_directory = tmp_path / "external-customer-locks"
    external_lock_directory.mkdir()
    (profile_base / ".flow2api-locks").symlink_to(
        external_lock_directory,
        target_is_directory=True,
    )
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    runtime = module.SetupRuntime(profile_base, "", executable)
    token = SimpleNamespace(
        id=23,
        email="account@example.com",
        current_project_id=None,
    )

    class Database:
        async def get_token(self, token_id):
            assert token_id == 23
            return token

        async def get_token_lifecycle(self, token_id):
            assert token_id == 23
            return SimpleNamespace(verified_email=None)

    monkeypatch.setattr(module, "resolve_runtime", lambda _config, _env: runtime)
    monkeypatch.setattr(module, "Database", Database)
    monkeypatch.setattr(module, "ProxyManager", lambda _db: object())
    monkeypatch.setattr(
        module,
        "_load_runtime_dependencies",
        lambda: (object(), lambda _proxy, _db: object()),
    )

    assert asyncio.run(module.async_main(["23", ":11"])) == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "service lease could not be acquired" in combined
    assert "ValueError" in combined
    assert str(profile_base) not in combined
    assert str(external_lock_directory) not in combined


def test_setup_service_lease_runtime_error_is_converted_to_safety_error(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = load_helper()
    profile_base = tmp_path / "private-profile-base"
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    runtime = module.SetupRuntime(profile_base, "", executable)
    token = SimpleNamespace(
        id=23,
        email="account@example.com",
        current_project_id=None,
    )
    secret_path = tmp_path / "private-runtime-detail"

    class Database:
        async def get_token(self, token_id):
            assert token_id == 23
            return token

        async def get_token_lifecycle(self, token_id):
            assert token_id == 23
            return SimpleNamespace(verified_email=None)

    def fail_lease(_profile_base, _token_id):
        raise RuntimeError(f"failed while resolving {secret_path}")

    monkeypatch.setattr(module, "resolve_runtime", lambda _config, _env: runtime)
    monkeypatch.setattr(module, "Database", Database)
    monkeypatch.setattr(module, "ProxyManager", lambda _db: object())
    monkeypatch.setattr(
        module,
        "_load_runtime_dependencies",
        lambda: (object(), lambda _proxy, _db: object()),
    )
    monkeypatch.setattr(module, "acquire_profile_lease", fail_lease)

    assert asyncio.run(module.async_main(["23", ":11"])) == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "service lease could not be acquired" in combined
    assert "RuntimeError" in combined
    assert str(profile_base) not in combined
    assert str(secret_path) not in combined


def test_setup_lock_inspection_oserror_is_sanitized(tmp_path, monkeypatch, capsys):
    module = load_helper()
    profile_base = tmp_path / "private-profile-base"
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    executable.chmod(0o700)
    runtime = module.SetupRuntime(profile_base, "", executable)
    token = SimpleNamespace(
        id=23,
        email="account@example.com",
        current_project_id=None,
    )
    secret_path = tmp_path / "private-singleton-detail"

    class Database:
        async def get_token(self, token_id):
            assert token_id == 23
            return token

        async def get_token_lifecycle(self, token_id):
            assert token_id == 23
            return SimpleNamespace(verified_email=None)

    def fail_inspection(_profile_path):
        raise OSError(f"failed while inspecting {secret_path}")

    monkeypatch.setattr(module, "resolve_runtime", lambda _config, _env: runtime)
    monkeypatch.setattr(module, "Database", Database)
    monkeypatch.setattr(module, "ProxyManager", lambda _db: object())
    monkeypatch.setattr(
        module,
        "_load_runtime_dependencies",
        lambda: (object(), lambda _proxy, _db: object()),
    )
    monkeypatch.setattr(module, "inspect_singleton_lock", fail_inspection)

    assert asyncio.run(module.async_main(["23", ":11"])) == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "before launch: profile lock inspection failed" in combined
    assert str(profile_base) not in combined
    assert str(secret_path) not in combined


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
def test_setup_cli_sanitizes_lazy_config_failures(
    tmp_path,
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
            str(HELPER_PATH),
            "23",
            ":11",
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


def test_shell_wrapper_is_private_safe_and_contains_no_process_killing():
    source = WRAPPER_PATH.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "umask 077" in source
    assert "setup_keepalive_profile.py" in source
    assert "pkill" not in source
    assert "SingletonLock" not in source
    assert "rm " not in source
    assert "python3 -c" not in source
    assert "python -c" not in source
    assert not any(line.rstrip().endswith("&") for line in source.splitlines())
    assert stat.S_IMODE(WRAPPER_PATH.stat().st_mode) & stat.S_IXUSR
