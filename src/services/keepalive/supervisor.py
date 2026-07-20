"""Dynamic account runners and reconciliation for browser keepalive."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ...core.token_states import AccountLifecycleStatus
from .alerts import AlertKind, AlertState, evaluate_alert_transition
from .models import FailureCode, RefreshOutcome, RuntimeMode
from .profile import (
    ProfileBusyError,
    ProfileLeaseBusyError,
    ProfileLockUncertainError,
    acquire_profile_lease,
    prepare_profile,
)
from .refresher import (
    KeepaliveRefresher,
    classify_browser_launch_failure,
    launch_keepalive_browser,
    safe_stop_browser,
)
from .scheduler import DEFAULT_POLICY, KeepaliveScheduler, ScheduleState, SchedulerPolicy


Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
AlertSender = Callable[[object, object], Awaitable[Any]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock must return a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _failure_detail(operation: str, error: BaseException) -> str:
    return f"{operation} failed ({type(error).__name__})"


def _positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_float(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _runtime_mode(target: object) -> RuntimeMode:
    raw_mode = getattr(target, "runtime_mode", None)
    try:
        return RuntimeMode(raw_mode)
    except ValueError as error:
        raise ValueError(f"unsupported keepalive runtime mode: {raw_mode!r}") from error


def _is_retired(target: object) -> bool:
    status = getattr(target, "membership_confirmed_status", None)
    return status in (
        AccountLifecycleStatus.RETIRED,
        AccountLifecycleStatus.RETIRED.value,
    )


def _target_id(target: object) -> int:
    token_id = getattr(target, "id", None)
    if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id <= 0:
        raise ValueError("keepalive target must have a positive integer ID")
    return token_id


def _alert_state_from_target(target: object) -> AlertState:
    raw_code = getattr(target, "last_alert_code", None)
    active_code = FailureCode(raw_code) if raw_code else None
    return AlertState(
        active_code=active_code,
        episode=getattr(target, "alert_episode", 0),
        alerted=getattr(target, "alerted", False),
    )


class ManagedAccountRunner:
    """Own the browser, profile lease, scheduling, and telemetry for one token."""

    def __init__(
        self,
        target: object,
        db: object,
        *,
        profile_base: Path | str,
        browser_executable: Path | str,
        refresher: KeepaliveRefresher | object,
        proxy: Optional[str] = None,
        display: Optional[str] = None,
        settle_seconds: float = 8.0,
        browser_launcher: Callable[..., Any] = launch_keepalive_browser,
        browser_stopper: Callable[..., Any] = safe_stop_browser,
        profile_lease_acquirer: Callable[..., Any] = acquire_profile_lease,
        profile_preparer: Callable[..., Any] = prepare_profile,
        launch_semaphore: Optional[asyncio.Semaphore] = None,
        refresh_semaphore: Optional[asyncio.Semaphore] = None,
        alert_sender: Optional[AlertSender] = None,
        scheduler: Optional[KeepaliveScheduler] = None,
        scheduler_policy: SchedulerPolicy = DEFAULT_POLICY,
        clock: Clock = _utc_now,
        shutdown_timeout_seconds: float = 20.0,
    ) -> None:
        self._token_id = _target_id(target)
        _runtime_mode(target)
        if not callable(clock):
            raise TypeError("clock must be callable")
        dependencies = (
            browser_launcher,
            browser_stopper,
            profile_lease_acquirer,
            profile_preparer,
        )
        if not all(callable(dependency) for dependency in dependencies):
            raise TypeError("runner browser and profile dependencies must be callable")
        if not callable(getattr(refresher, "refresh", None)):
            raise TypeError("refresher must provide a callable refresh method")
        if alert_sender is not None and not callable(alert_sender):
            raise TypeError("alert_sender must be callable or None")

        self._target = target
        self._db = db
        self._profile_base = Path(profile_base).expanduser().resolve(strict=False)
        self._browser_executable = Path(browser_executable).expanduser().resolve(
            strict=False
        )
        self._proxy = str(proxy).strip() if proxy is not None else None
        self._display = str(display).strip() if display is not None else None
        self._settle_seconds = _nonnegative_float(settle_seconds, "settle_seconds")
        self._refresher = refresher
        self._browser_launcher = browser_launcher
        self._browser_stopper = browser_stopper
        self._profile_lease_acquirer = profile_lease_acquirer
        self._profile_preparer = profile_preparer
        self._launch_semaphore = launch_semaphore or asyncio.Semaphore(1)
        self._refresh_semaphore = refresh_semaphore or asyncio.Semaphore(1)
        self._alert_sender = alert_sender
        self._clock = clock
        self._shutdown_timeout_seconds = _nonnegative_float(
            shutdown_timeout_seconds,
            "shutdown_timeout_seconds",
        )
        if self._shutdown_timeout_seconds == 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._scheduler = scheduler or KeepaliveScheduler(
            clock=clock,
            policy=scheduler_policy,
        )

        self._browser: object | None = None
        self._profile_lease: object | None = None
        self._next_due_at = getattr(target, "next_due_at", None)
        self._failure_count = getattr(target, "keepalive_failure_count", 0)
        self._alert_state = _alert_state_from_target(target)
        self._last_alert_at = getattr(target, "last_alert_at", None)
        self._operation_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._active_task: asyncio.Task | None = None
        self._stopped = False
        self._drained = False

    @property
    def token_id(self) -> int:
        return self._token_id

    @property
    def target(self) -> object:
        return self._target

    @property
    def browser(self) -> object | None:
        return self._browser

    @property
    def profile_lease(self) -> object | None:
        return self._profile_lease

    @property
    def scheduler(self) -> KeepaliveScheduler:
        return self._scheduler

    @property
    def next_due_at(self) -> Optional[datetime]:
        return self._next_due_at

    def _schedule_state(self) -> ScheduleState:
        return ScheduleState(
            token_id=self._token_id,
            runtime_mode=_runtime_mode(self._target),
            retired=_is_retired(self._target),
            next_due_at=self._next_due_at,
            failure_count=self._failure_count,
        )

    def is_due(self) -> bool:
        if self._stopped:
            return False
        return self._scheduler.evaluate_due(
            self._schedule_state(),
            browser_running=self._browser is not None,
        ).due

    def _effective_proxy(self) -> Optional[str]:
        target_proxy = str(getattr(self._target, "captcha_proxy_url", None) or "").strip()
        return target_proxy or self._proxy

    async def _drain_resources(self) -> None:
        browser = self._browser
        lease = self._profile_lease
        self._browser = None
        self._profile_lease = None
        try:
            if browser is not None:
                try:
                    await _maybe_await(self._browser_stopper(browser))
                except Exception:
                    pass
        finally:
            if lease is not None:
                try:
                    await _maybe_await(lease.release())
                except Exception:
                    pass

    @staticmethod
    def _profile_error(error: BaseException) -> RefreshOutcome:
        if isinstance(error, ProfileLeaseBusyError):
            return RefreshOutcome.failure(
                FailureCode.PROFILE_BUSY,
                detail="keepalive profile lease is busy",
            )
        if isinstance(error, ProfileBusyError):
            return RefreshOutcome.failure(
                FailureCode.PROFILE_BUSY,
                detail="browser profile is owned by a live Chrome process",
                human_action=True,
            )
        if isinstance(error, ProfileLockUncertainError):
            return RefreshOutcome.failure(
                FailureCode.PROFILE_BUSY,
                detail="browser profile lock ownership is uncertain",
                human_action=True,
            )
        return RefreshOutcome.failure(
            FailureCode.INTERNAL,
            detail=_failure_detail("profile preparation", error),
        )

    async def _launch_browser(self) -> Optional[RefreshOutcome]:
        try:
            lease = await _maybe_await(
                self._profile_lease_acquirer(self._profile_base, self._token_id)
            )
        except (ProfileLeaseBusyError, ProfileBusyError, ProfileLockUncertainError) as error:
            return self._profile_error(error)
        except Exception as error:
            return RefreshOutcome.failure(
                FailureCode.INTERNAL,
                detail=_failure_detail("profile lease acquisition", error),
            )

        self._profile_lease = lease
        try:
            profile_path = Path(lease.profile_path).expanduser().resolve(strict=False)
        except Exception as error:
            return RefreshOutcome.failure(
                FailureCode.INTERNAL,
                detail=_failure_detail("profile path resolution", error),
            )
        profile_state = str(getattr(self._target, "profile_state", "")).strip()
        if profile_state != "ready" or not profile_path.is_dir():
            return RefreshOutcome.failure(
                FailureCode.PROFILE_MISSING,
                detail="browser profile is missing or not provisioned",
                human_action=True,
            )

        try:
            await _maybe_await(self._profile_preparer(lease))
        except FileNotFoundError:
            return RefreshOutcome.failure(
                FailureCode.PROFILE_MISSING,
                detail="browser profile is missing",
                human_action=True,
            )
        except (ProfileLeaseBusyError, ProfileBusyError, ProfileLockUncertainError) as error:
            return self._profile_error(error)
        except Exception as error:
            return self._profile_error(error)

        try:
            async with self._launch_semaphore:
                browser = await _maybe_await(
                    self._browser_launcher(
                        profile_path,
                        self._effective_proxy(),
                        self._display,
                        self._browser_executable,
                        headless=False,
                    )
                )
        except Exception as error:
            return classify_browser_launch_failure(error)
        if browser is None:
            return RefreshOutcome.failure(
                FailureCode.BROWSER_LAUNCH,
                detail="browser launcher returned no browser",
                restart_browser=True,
            )
        self._browser = browser
        return None

    async def _refresh(self, profile_path: Path) -> RefreshOutcome:
        try:
            async with self._refresh_semaphore:
                outcome = await self._refresher.refresh(
                    self._browser,
                    self._target,
                    profile_path,
                    settle_seconds=self._settle_seconds,
                )
        except Exception as error:
            return RefreshOutcome.failure(
                FailureCode.INTERNAL,
                detail=_failure_detail("keepalive refresh", error),
            )
        if not isinstance(outcome, RefreshOutcome):
            return RefreshOutcome.failure(
                FailureCode.INTERNAL,
                detail="keepalive refresher returned an invalid outcome",
            )
        return outcome

    async def _persist_alert_transition(
        self,
        outcome: RefreshOutcome,
        attempted_at: datetime,
    ) -> None:
        transition = evaluate_alert_transition(self._alert_state, outcome)
        state_to_persist = transition.current
        alerted_at = self._last_alert_at

        if transition.event is not None:
            delivered = False
            if self._alert_sender is not None:
                try:
                    delivery_result = await _maybe_await(
                        self._alert_sender(self._target, transition.event)
                    )
                    delivered = delivery_result is not False
                except Exception:
                    delivered = False
            if delivered:
                alerted_at = (
                    attempted_at
                    if transition.event.kind is AlertKind.FAILURE
                    else None
                )
            elif transition.event.kind is AlertKind.FAILURE:
                state_to_persist = AlertState(
                    active_code=transition.current.active_code,
                    episode=transition.current.episode,
                    alerted=False,
                )
                alerted_at = None
            else:
                state_to_persist = transition.previous
                alerted_at = self._last_alert_at

        await self._db.update_token_alert_state(
            self._token_id,
            alert_code=(
                state_to_persist.active_code.value
                if state_to_persist.active_code is not None
                else None
            ),
            episode=state_to_persist.episode,
            alerted=state_to_persist.alerted,
            alerted_at=alerted_at,
        )
        self._alert_state = state_to_persist
        self._last_alert_at = alerted_at

    async def _run_attempt(self, state: ScheduleState) -> RefreshOutcome:
        attempted_at = _as_utc(self._clock())
        outcome: Optional[RefreshOutcome] = None
        try:
            if self._browser is None:
                outcome = await self._launch_browser()
            if outcome is None:
                lease = self._profile_lease
                if self._browser is None or lease is None:
                    outcome = RefreshOutcome.failure(
                        FailureCode.INTERNAL,
                        detail="browser and profile lease ownership became inconsistent",
                        restart_browser=True,
                    )
                else:
                    profile_path = Path(lease.profile_path).expanduser().resolve(
                        strict=False
                    )
                    outcome = await self._refresh(profile_path)

            next_due_at = self._scheduler.next_due_at(state, outcome)
            await self._db.update_token_keepalive_telemetry(
                self._token_id,
                status="success" if outcome.ok else "failure",
                error=None if outcome.ok else outcome.detail,
                error_code=None if outcome.code is None else outcome.code.value,
                attempted_at=attempted_at,
                next_due_at=next_due_at,
            )
            self._next_due_at = next_due_at
            self._failure_count = 0 if outcome.ok else state.failure_count + 1
            await self._persist_alert_transition(outcome, attempted_at)
            return outcome
        finally:
            if outcome is None or (
                _runtime_mode(self._target) is RuntimeMode.WARM
                or outcome.restart_browser
                or self._browser is None
            ):
                await self._drain_resources()

    async def _run_tracked_attempt(
        self,
        *,
        require_due: bool,
    ) -> Optional[RefreshOutcome]:
        async with self._operation_lock:
            if self._stopped:
                return None
            current_task = asyncio.current_task()
            if current_task is None:
                raise RuntimeError("keepalive attempt requires an asyncio task")
            self._active_task = current_task
            try:
                state = self._schedule_state()
                if require_due:
                    decision = self._scheduler.evaluate_due(
                        state,
                        browser_running=self._browser is not None,
                    )
                    if not decision.due:
                        return None
                return await self._run_attempt(state)
            finally:
                if self._active_task is current_task:
                    self._active_task = None

    async def run_now(self) -> Optional[RefreshOutcome]:
        """Run an immediate operational gate regardless of the persisted due time."""
        return await self._run_tracked_attempt(require_due=False)

    async def run_if_due(self) -> Optional[RefreshOutcome]:
        """Run one due attempt, or return ``None`` when this target is not due."""
        return await self._run_tracked_attempt(require_due=True)

    async def update_target(self, target: object) -> None:
        """Apply fresh database metadata and drain on persistent-to-warm changes."""

        if _target_id(target) != self._token_id:
            raise ValueError("cannot replace a runner target with a different token ID")
        new_mode = _runtime_mode(target)
        async with self._operation_lock:
            previous_mode = _runtime_mode(self._target)
            previous_proxy = self._effective_proxy()
            self._target = target
            self._next_due_at = getattr(target, "next_due_at", None)
            self._failure_count = getattr(target, "keepalive_failure_count", 0)
            self._alert_state = _alert_state_from_target(target)
            self._last_alert_at = getattr(target, "last_alert_at", None)
            profile_ready = str(getattr(target, "profile_state", "")).strip() == "ready"
            should_drain = (
                previous_mode is RuntimeMode.PERSISTENT
                and new_mode is RuntimeMode.WARM
            ) or not profile_ready or previous_proxy != self._effective_proxy()
            if should_drain:
                await self._drain_resources()

    async def stop(self) -> None:
        """Cancel active work and release resources within a bounded interval."""

        self._stopped = True
        async with self._stop_lock:
            if self._drained:
                return
            active_task = self._active_task
            current_task = asyncio.current_task()
            if active_task is not None and active_task is not current_task:
                active_task.cancel()
                done, _pending = await asyncio.wait(
                    (active_task,),
                    timeout=self._shutdown_timeout_seconds,
                )
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
            try:
                await asyncio.wait_for(
                    self._drain_resources(),
                    timeout=self._shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                pass
            self._drained = True


class KeepaliveSupervisor:
    """Reconcile database targets into dynamic runners and execute due work."""

    def __init__(
        self,
        db: object,
        runner_factory: Optional[Callable[..., Any]] = None,
        *,
        profile_base: Path | str | None = None,
        browser_executable: Path | str | None = None,
        refresher: KeepaliveRefresher | object | None = None,
        refresher_factory: Optional[Callable[..., Any]] = None,
        proxy: Optional[str] = None,
        display: Optional[str] = None,
        settle_seconds: float = 8.0,
        browser_launcher: Callable[..., Any] = launch_keepalive_browser,
        browser_stopper: Callable[..., Any] = safe_stop_browser,
        profile_lease_acquirer: Callable[..., Any] = acquire_profile_lease,
        profile_preparer: Callable[..., Any] = prepare_profile,
        alert_sender: Optional[AlertSender] = None,
        scheduler_policy: SchedulerPolicy = DEFAULT_POLICY,
        max_concurrent_launches: int = 1,
        max_concurrent_refreshes: int = 1,
        reconcile_interval_seconds: float = 15.0,
        clock: Clock = _utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not callable(clock) or not callable(sleep):
            raise TypeError("clock and sleep must be callable")
        if runner_factory is not None and not callable(runner_factory):
            raise TypeError("runner_factory must be callable or None")
        if refresher is not None and refresher_factory is not None:
            raise ValueError("pass refresher or refresher_factory, not both")
        if runner_factory is None:
            if profile_base is None or browser_executable is None:
                raise ValueError(
                    "profile_base and browser_executable are required without runner_factory"
                )
            if refresher is None and refresher_factory is None:
                raise ValueError(
                    "refresher or refresher_factory is required without runner_factory"
                )

        self._db = db
        self._runner_factory = runner_factory
        self._profile_base = profile_base
        self._browser_executable = browser_executable
        self._refresher = refresher
        self._refresher_factory = refresher_factory
        self._proxy = proxy
        self._display = display
        self._settle_seconds = _nonnegative_float(settle_seconds, "settle_seconds")
        self._browser_launcher = browser_launcher
        self._browser_stopper = browser_stopper
        self._profile_lease_acquirer = profile_lease_acquirer
        self._profile_preparer = profile_preparer
        self._alert_sender = alert_sender
        self._scheduler_policy = scheduler_policy
        self._clock = clock
        self._sleep = sleep
        self._reconcile_interval_seconds = _nonnegative_float(
            reconcile_interval_seconds,
            "reconcile_interval_seconds",
        )
        self._launch_semaphore = asyncio.Semaphore(
            _positive_integer(max_concurrent_launches, "max_concurrent_launches")
        )
        self._refresh_semaphore = asyncio.Semaphore(
            _positive_integer(max_concurrent_refreshes, "max_concurrent_refreshes")
        )
        self._runners: dict[int, object] = {}
        self._reconcile_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._drained = False

    @property
    def runners(self) -> dict[int, object]:
        return dict(self._runners)

    @staticmethod
    def _factory_accepts_keywords(factory: Callable[..., Any], names: set[str]) -> bool:
        try:
            parameters = inspect.signature(factory).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters) or names.issubset(
            {parameter.name for parameter in parameters}
        )

    async def _call_target_factory(self, factory: Callable[..., Any], target: object) -> Any:
        try:
            parameters = inspect.signature(factory).parameters.values()
        except (TypeError, ValueError):
            return await _maybe_await(factory(target))
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and parameter.default is inspect.Parameter.empty
        ]
        if positional or any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters
        ):
            return await _maybe_await(factory(target))
        return await _maybe_await(factory())

    async def _create_runner(self, target: object) -> object:
        if self._runner_factory is not None:
            dependency_names = {"launch_semaphore", "refresh_semaphore"}
            if self._factory_accepts_keywords(self._runner_factory, dependency_names):
                result = self._runner_factory(
                    target,
                    launch_semaphore=self._launch_semaphore,
                    refresh_semaphore=self._refresh_semaphore,
                )
            else:
                result = self._runner_factory(target)
            return await _maybe_await(result)

        account_refresher = self._refresher
        if self._refresher_factory is not None:
            account_refresher = await self._call_target_factory(
                self._refresher_factory,
                target,
            )
        return ManagedAccountRunner(
            target,
            self._db,
            profile_base=self._profile_base,
            browser_executable=self._browser_executable,
            refresher=account_refresher,
            proxy=self._proxy,
            display=self._display,
            settle_seconds=self._settle_seconds,
            browser_launcher=self._browser_launcher,
            browser_stopper=self._browser_stopper,
            profile_lease_acquirer=self._profile_lease_acquirer,
            profile_preparer=self._profile_preparer,
            launch_semaphore=self._launch_semaphore,
            refresh_semaphore=self._refresh_semaphore,
            alert_sender=self._alert_sender,
            scheduler_policy=self._scheduler_policy,
            clock=self._clock,
        )

    async def reconcile(self) -> bool:
        """Refresh the runner set; preserve it unchanged on transient DB failure."""

        if self._stop_event.is_set():
            return False
        try:
            targets = await self._db.list_keepalive_enabled_tokens()
            target_map = {_target_id(target): target for target in targets}
        except Exception:
            return False

        async with self._reconcile_lock:
            if self._stop_event.is_set():
                return False
            removed_ids = sorted(set(self._runners) - set(target_map))
            for token_id in removed_ids:
                runner = self._runners.pop(token_id)
                try:
                    await runner.stop()
                except Exception:
                    pass

            for token_id in sorted(set(self._runners) & set(target_map)):
                await self._runners[token_id].update_target(target_map[token_id])

            for token_id in sorted(set(target_map) - set(self._runners)):
                self._runners[token_id] = await self._create_runner(
                    target_map[token_id]
                )
        return True

    @staticmethod
    def _due_sort_key(runner: object) -> tuple[datetime, int]:
        due_at = getattr(runner, "next_due_at", None)
        if due_at is None:
            target = getattr(runner, "target", None)
            due_at = getattr(target, "next_due_at", None)
        normalized_due = (
            datetime.min.replace(tzinfo=timezone.utc)
            if due_at is None
            else _as_utc(due_at)
        )
        return normalized_due, _target_id(getattr(runner, "target", None))

    async def run_due_once(self) -> list[object]:
        """Start all currently due runners in stable due-time/token-ID order."""

        if self._stop_event.is_set():
            return []
        ordered_runners = sorted(self._runners.values(), key=self._due_sort_key)
        due_runners = []
        for runner in ordered_runners:
            is_due = getattr(runner, "is_due", None)
            try:
                if callable(is_due) and not is_due():
                    continue
            except Exception:
                continue
            due_runners.append(runner)
        if not due_runners:
            return []
        return list(
            await asyncio.gather(
                *(runner.run_if_due() for runner in due_runners),
                return_exceptions=True,
            )
        )

    async def _sleep_or_stop(self) -> None:
        if self._stop_event.is_set():
            return
        sleep_task = asyncio.create_task(
            self._sleep(self._reconcile_interval_seconds)
        )
        stop_task = asyncio.create_task(self._stop_event.wait())
        done, pending = await asyncio.wait(
            (sleep_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            await task

    async def run_forever(self) -> None:
        """Continuously reconcile and run due accounts until graceful shutdown."""

        try:
            while not self._stop_event.is_set():
                await self.reconcile()
                await self.run_due_once()
                await self._sleep_or_stop()
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Signal shutdown and idempotently drain all managed runners."""

        self._stop_event.set()
        async with self._stop_lock:
            if self._drained:
                return
            async with self._reconcile_lock:
                runners = [
                    self._runners[token_id] for token_id in sorted(self._runners)
                ]
                self._runners.clear()
            if runners:
                await asyncio.gather(
                    *(runner.stop() for runner in runners),
                    return_exceptions=True,
                )
            self._drained = True


__all__ = ["KeepaliveSupervisor", "ManagedAccountRunner"]
