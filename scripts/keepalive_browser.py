#!/usr/bin/env python3
"""Flow2API database-driven browser keepalive service and operational gate."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import signal
import sys
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.database import Database  # noqa: E402
from src.core.models import KeepaliveToken  # noqa: E402
from src.services.alert_notifier import AlertNotifier  # noqa: E402
from src.services.keepalive.alerts import AlertKind  # noqa: E402
from src.services.keepalive.profile import (  # noqa: E402
    ProfileLeaseBusyError,
    SingletonLockState,
    acquire_profile_lease,
    canonical_profile_path,
    inspect_singleton_lock,
)
from src.services.keepalive.refresher import KeepaliveRefresher  # noqa: E402
from src.services.keepalive.scheduler import SchedulerPolicy  # noqa: E402
from src.services.keepalive.supervisor import (  # noqa: E402
    KeepaliveSupervisor,
    ManagedAccountRunner,
)
from src.services.proxy_manager import ProxyManager  # noqa: E402
from src.services.tokens.account_identity import normalize_account_email  # noqa: E402


def _load_runtime_dependencies():
    from src.core.config import config
    from src.services.flow_client import FlowClient

    return config, FlowClient


def _browser_executable() -> Path:
    configured = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip()
    return Path(configured or "/usr/bin/google-chrome-stable").expanduser().resolve(
        strict=False
    )


def _scheduler_policy(config_object) -> SchedulerPolicy:
    return SchedulerPolicy(
        active_interval_seconds=config_object.keepalive_browser_interval_seconds,
        retired_interval_seconds=config_object.keepalive_browser_retired_interval_seconds,
        initial_delay_seconds=config_object.keepalive_browser_initial_delay_seconds,
        retry_base_seconds=config_object.keepalive_browser_retry_base_seconds,
        retry_max_seconds=config_object.keepalive_browser_retry_max_seconds,
        human_retry_seconds=config_object.keepalive_browser_human_retry_seconds,
    )


def _target_from_records(token, lifecycle) -> KeepaliveToken:
    values = token.model_dump()
    values.update(lifecycle.model_dump(exclude={"token_id"}))
    return KeepaliveToken(**values)


async def _get_target(db: Database, token_id: int) -> KeepaliveToken:
    token = await db.get_token(token_id)
    lifecycle = await db.get_token_lifecycle(token_id)
    if token is None or lifecycle is None:
        raise ValueError(f"token lifecycle not found: {token_id}")
    return _target_from_records(token, lifecycle)


def _alert_sender(notifier: AlertNotifier):
    async def send(target, event) -> bool:
        code = event.code.value
        if event.kind is AlertKind.RECOVERY:
            return await notifier.send_alert(
                "保活器账号恢复",
                f"账号 {target.email}（ID {target.id}）已从 {code} 恢复。",
                severity="warning",
            )
        return await notifier.send_alert(
            "保活器账号异常",
            f"账号 {target.email}（ID {target.id}）发生 {code}：{event.detail}",
            severity="critical",
        )

    return send


def _runtime(db: Database, *, config_object, flow_client_class):
    flow_client = flow_client_class(ProxyManager(db), db)
    refresher = KeepaliveRefresher(db, flow_client)
    notifier = AlertNotifier(config_object.alert_webhook_url)
    return {
        "profile_base": config_object.keepalive_browser_profile_base,
        "browser_executable": _browser_executable(),
        "refresher": refresher,
        "proxy": config_object.keepalive_browser_proxy,
        "display": config_object.keepalive_browser_display,
        "settle_seconds": config_object.keepalive_browser_settle_seconds,
        "alert_sender": _alert_sender(notifier),
        "scheduler_policy": _scheduler_policy(config_object),
    }


def _display_socket(display: str) -> Path | None:
    value = str(display or "").strip()
    if not value.startswith(":"):
        return None
    number = value[1:].split(".", 1)[0]
    if not number.isdigit():
        return None
    return Path("/tmp/.X11-unix") / f"X{number}"


def validate_enabled_profile(target: object, profile_base: Path | str) -> list[str]:
    """Validate one enabled profile without opening cookies or using credentials."""

    failures: list[str] = []
    token_id = getattr(target, "id", None)
    try:
        profile_path = canonical_profile_path(profile_base, token_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return [f"invalid token/profile mapping: id={token_id}"]

    profile_state = str(getattr(target, "profile_state", "") or "").strip()
    if profile_state != "ready":
        failures.append(f"profile state is not ready: id={token_id}")

    account_email = normalize_account_email(getattr(target, "email", ""))
    verified_email = normalize_account_email(getattr(target, "verified_email", ""))
    if not account_email or not verified_email:
        failures.append(f"verified identity is missing: id={token_id}")
    elif account_email != verified_email:
        failures.append(f"verified identity mismatch: id={token_id}")

    if not profile_path.is_dir():
        failures.append(f"profile directory missing: id={token_id}")
        return failures
    cookie_database = profile_path / "Default" / "Cookies"
    if not cookie_database.is_file() or not os.access(cookie_database, os.R_OK):
        failures.append(f"Cookies database missing or unreadable: id={token_id}")

    try:
        lease = acquire_profile_lease(profile_base, token_id)
    except ProfileLeaseBusyError:
        failures.append(f"service lease is busy: id={token_id}")
        return failures
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        failures.append(
            f"service lease unavailable: id={token_id} ({type(error).__name__})"
        )
        return failures

    try:
        try:
            inspection = inspect_singleton_lock(profile_path)
            if inspection.state is SingletonLockState.BUSY:
                failures.append(f"Chrome profile is busy: id={token_id}")
            elif inspection.state is SingletonLockState.UNSAFE:
                failures.append(
                    f"Chrome SingletonLock is unsafe: id={token_id} ({inspection.reason})"
                )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            failures.append(
                "Chrome SingletonLock inspection unavailable: "
                f"id={token_id} ({type(error).__name__})"
            )
    finally:
        try:
            lease.release()
        except Exception as error:
            failures.append(
                "service lease release unavailable: "
                f"id={token_id} ({type(error).__name__})"
            )
    return failures


async def preflight(db: Database, *, config_object) -> int:
    if not config_object.keepalive_browser_enabled:
        print("[keepalive][preflight] OK browser keepalive is disabled")
        return 0

    failures: list[str] = []
    for dependency in ("nodriver", "browser_cookie3"):
        try:
            importlib.import_module(dependency)
        except Exception as error:
            failures.append(f"Python dependency {dependency}: {type(error).__name__}")

    try:
        executable = _browser_executable()
        executable_ready = executable.is_file() and os.access(executable, os.X_OK)
    except (OSError, RuntimeError, ValueError):
        executable_ready = False
    if not executable_ready:
        failures.append("browser executable missing or not executable")

    try:
        profile_base = Path(
            config_object.keepalive_browser_profile_base
        ).expanduser().resolve(strict=False)
        profile_base_ready = profile_base.is_dir()
    except (OSError, RuntimeError, TypeError, ValueError):
        profile_base = None
        profile_base_ready = False
    if not profile_base_ready:
        failures.append("profile base missing or invalid")

    display_socket = _display_socket(config_object.keepalive_browser_display)
    if display_socket is None or not display_socket.exists():
        failures.append("X display unavailable")

    try:
        targets = await db.list_keepalive_enabled_tokens()
    except Exception as error:
        failures.append(f"lifecycle database unavailable: {type(error).__name__}")
        targets = []

    if profile_base_ready and profile_base is not None:
        for target in targets:
            failures.extend(validate_enabled_profile(target, profile_base))

    if failures:
        for failure in failures:
            print(f"[keepalive][preflight] ERROR {failure}")
        return 1

    print(
        "[keepalive][preflight] OK "
        f"enabled_accounts={len(targets)} "
        "display_ready=True headless=False credentials_read=False"
    )
    return 0


async def run_once(
    db: Database,
    token_id: int | None,
    *,
    config_object,
    flow_client_class,
) -> int:
    runtime = _runtime(
        db,
        config_object=config_object,
        flow_client_class=flow_client_class,
    )
    if token_id is None:
        targets = await db.list_keepalive_enabled_tokens()
    else:
        targets = [await _get_target(db, token_id)]
    if not targets:
        print("[keepalive] no enabled account to validate")
        return 1

    all_ok = True
    for target in targets:
        runner = ManagedAccountRunner(target, db, **runtime)
        try:
            outcome = await runner.run_now()
            if outcome is None or not outcome.ok:
                all_ok = False
                code = outcome.code.value if outcome and outcome.code else "internal"
                detail = outcome.detail if outcome else "runner stopped"
                print(f"[keepalive] id={target.id} FAILED code={code} detail={detail}")
            else:
                print(
                    f"[keepalive] id={target.id} OK credits={outcome.credits} "
                    f"expiry={outcome.expiry} headless=False"
                )
        finally:
            await runner.stop()
    return 0 if all_ok else 1


def install_shutdown_handlers(
    supervisor: KeepaliveSupervisor,
    *,
    loop=None,
) -> Callable[[], None]:
    """Install idempotent SIGTERM/SIGINT handlers that drain the supervisor."""

    event_loop = loop or asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    shutdown_started = False

    def request_shutdown() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            return
        shutdown_started = True
        event_loop.create_task(supervisor.stop())

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            event_loop.add_signal_handler(signum, request_shutdown)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signum)

    def uninstall() -> None:
        for signum in installed:
            try:
                event_loop.remove_signal_handler(signum)
            except (NotImplementedError, RuntimeError):
                pass

    return uninstall


async def run_daemon(
    db: Database,
    *,
    config_object,
    flow_client_class,
) -> int:
    if not config_object.keepalive_browser_enabled:
        print("[keepalive] browser keepalive is disabled")
        return 0
    supervisor = KeepaliveSupervisor(
        db,
        **_runtime(
            db,
            config_object=config_object,
            flow_client_class=flow_client_class,
        ),
        max_concurrent_launches=config_object.keepalive_browser_max_concurrent_launches,
        max_concurrent_refreshes=config_object.keepalive_browser_max_concurrent_refreshes,
        reconcile_interval_seconds=config_object.keepalive_browser_reconcile_interval_seconds,
    )
    uninstall_handlers = install_shutdown_handlers(supervisor)
    print(
        "[keepalive] database supervisor started "
        f"reconcile={config_object.keepalive_browser_reconcile_interval_seconds}s "
        f"headless=False"
    )
    try:
        await supervisor.run_forever()
    finally:
        await supervisor.stop()
        uninstall_handlers()
    return 0


def _canonical_positive_int(raw_value: str) -> int:
    value = str(raw_value)
    if not value.isascii() or not value.isdigit() or value.startswith("0"):
        raise argparse.ArgumentTypeError("must use canonical positive decimal form")
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flow2API browser keepalive supervisor")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--daemon",
        dest="mode",
        action="store_const",
        const="daemon",
        help="run the dynamic database supervisor (default)",
    )
    modes.add_argument(
        "--preflight",
        dest="mode",
        action="store_const",
        const="preflight",
        help="validate runtime without launching Chrome or reading credentials",
    )
    modes.add_argument(
        "--once",
        dest="mode",
        action="store_const",
        const="once",
        help="run an immediate live account gate",
    )
    parser.set_defaults(mode="daemon")
    parser.add_argument(
        "--token-id",
        type=_canonical_positive_int,
        default=None,
        help="limit one-shot gate to one canonical positive token ID",
    )
    return parser


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.token_id is not None and args.mode != "once":
        parser.error("--token-id requires --once")
    return args


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    try:
        config_object, flow_client_class = _load_runtime_dependencies()
        db = Database()
        if args.mode == "preflight":
            return await preflight(db, config_object=config_object)
        if args.mode == "once":
            return await run_once(
                db,
                args.token_id,
                config_object=config_object,
                flow_client_class=flow_client_class,
            )
        return await run_daemon(
            db,
            config_object=config_object,
            flow_client_class=flow_client_class,
        )
    except Exception as error:
        print(
            f"[keepalive] ERROR runtime initialization failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
