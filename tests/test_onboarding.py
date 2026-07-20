"""Server-first XRDP onboarding service tests with no real browser or network."""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.account_identity import VerifiedAccountSnapshot, normalize_account_email
from src.core.models import OnboardingJob, Project, Token, TokenLifecycle
from src.core.token_states import (
    TOKEN_REASON_429_RATE_LIMIT,
    TOKEN_REASON_MANUAL_DISABLED,
    TOKEN_REASON_ONBOARDING_PENDING,
)


LONG_ST = "eyJ" + "s" * 1100
ROTATED_ST = "eyJ" + "r" * 1100
NOW = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)


def snapshot(
    email: str = "owner@example.com",
    tier: str | None = "PAYGATE_TIER_ONE",
    st: str = ROTATED_ST,
) -> VerifiedAccountSnapshot:
    return VerifiedAccountSnapshot(
        email=email,
        normalized_email=normalize_account_email(email),
        name="Owner",
        st=st,
        at="verified-access-token",
        at_expires=NOW + timedelta(hours=1),
        credits=900,
        user_paygate_tier=tier,
    )


class FakeDB:
    def __init__(self, tokens: list[Token] | None = None):
        self.jobs: dict[str, OnboardingJob] = {}
        self.tokens = {token.id: token.model_copy(deep=True) for token in tokens or []}
        self.lifecycles = {
            token.id: TokenLifecycle(token_id=token.id) for token in tokens or []
        }
        self.projects: dict[int, list[Project]] = {}
        self.job_updates: list[tuple[str, dict]] = []
        self.desired_updates: list[tuple[int, dict]] = []
        self.fail_completion_once = False
        self.fail_commit_marker_once = False
        self._job_counter = 0

    async def create_onboarding_job(self, job: OnboardingJob):
        self._job_counter += 1
        job_id = job.job_id or f"job-{self._job_counter}"
        self.jobs[job_id] = job.model_copy(update={"job_id": job_id, "id": self._job_counter})
        return job_id

    async def get_onboarding_job(self, job_id):
        job = self.jobs.get(str(job_id))
        return job.model_copy(deep=True) if job else None

    async def claim_onboarding_job(self, job_id):
        job_id = str(job_id)
        job = self.jobs[job_id]
        if job.state != "pending" or any(
            candidate.state in {"running", "failed"}
            and candidate.job_id != job_id
            for candidate in self.jobs.values()
        ):
            return False
        self.jobs[job_id] = job.model_copy(
            update={
                "state": "running",
                "phase": "browser_start",
                "started_at": NOW,
                "updated_at": NOW,
            }
        )
        return True

    async def claim_failed_onboarding_job_resume(
        self,
        job_id,
        *,
        expected_phase,
        expected_error_code,
        expected_pid,
        expected_start_ticks,
        expected_expires_at,
        refreshed_expires_at,
    ):
        job_id = str(job_id)
        job = self.jobs[job_id]
        metadata_is_clear = all(
            getattr(job, field) is None
            for field in (
                "resolved_token_id",
                "discovered_email",
                "discovered_tier",
                "discovered_credits",
                "discovered_at_expires",
                "project_count",
                "profile_ready",
                "conflict_status",
            )
        )
        active_job_exists = any(
            candidate.state in {"running", "failed"}
            and candidate.job_id != job_id
            for candidate in self.jobs.values()
        )
        if (
            job.state != "failed"
            or job.phase != expected_phase
            or job.phase not in {"stop_browser", "verify_account"}
            or job.error_code != expected_error_code
            or job.browser_pid != expected_pid
            or job.browser_start_ticks != expected_start_ticks
            or job.expires_at != expected_expires_at
            or not metadata_is_clear
            or active_job_exists
        ):
            return False
        self.jobs[job_id] = job.model_copy(
            update={
                "state": "running",
                "phase": "browser_start",
                "browser_pid": None,
                "browser_start_ticks": None,
                "error_code": None,
                "error_message": None,
                "expires_at": refreshed_expires_at,
                "completed_at": None,
                "cancelled_at": None,
                "updated_at": NOW,
            }
        )
        return True

    async def list_onboarding_jobs(self, **filters):
        jobs = list(self.jobs.values())
        for field, value in filters.items():
            if value is not None:
                jobs = [job for job in jobs if getattr(job, field) == value]
        return [job.model_copy(deep=True) for job in jobs]

    async def update_onboarding_job(self, job_id, **fields):
        self.job_updates.append((str(job_id), dict(fields)))
        self.jobs[str(job_id)] = self.jobs[str(job_id)].model_copy(update=fields)

    async def clear_onboarding_browser_identity(
        self,
        job_id,
        *,
        expected_pid,
        expected_start_ticks,
    ):
        return await self.replace_onboarding_browser_identity(
            job_id,
            expected_pid=expected_pid,
            expected_start_ticks=expected_start_ticks,
            browser_pid=None,
            browser_start_ticks=None,
        )

    async def replace_onboarding_browser_identity(
        self,
        job_id,
        *,
        expected_pid,
        expected_start_ticks,
        browser_pid,
        browser_start_ticks,
    ):
        job_id = str(job_id)
        current = self.jobs[job_id]
        if (
            current.browser_pid != expected_pid
            or current.browser_start_ticks != expected_start_ticks
            or current.state not in {"running", "failed"}
        ):
            return False
        update = {
            "browser_pid": browser_pid,
            "browser_start_ticks": browser_start_ticks,
        }
        self.job_updates.append((job_id, update))
        self.jobs[job_id] = current.model_copy(update=update)
        return True

    async def transition_onboarding_job_state(
        self,
        job_id,
        *,
        expected_state,
        expected_phase,
        state,
        phase,
        clear_error=False,
    ):
        current = self.jobs[str(job_id)]
        if current.state != expected_state or current.phase != expected_phase:
            return False
        await self.update_onboarding_job_state(
            job_id,
            state,
            phase=phase,
            clear_error=clear_error,
        )
        return True

    async def update_onboarding_job_state(
        self,
        job_id,
        state,
        *,
        phase=None,
        error_code=None,
        error_message=None,
        clear_error=False,
    ):
        if state == "completed" and self.fail_completion_once:
            self.fail_completion_once = False
            raise RuntimeError("database completion write failed with sensitive details")
        if state == "running" and phase == "commit_complete" and self.fail_commit_marker_once:
            self.fail_commit_marker_once = False
            raise RuntimeError("database commit marker write failed with sensitive details")
        current = self.jobs[str(job_id)]
        updates = {"state": state, "updated_at": NOW}
        if phase is not None:
            updates["phase"] = phase
        if clear_error:
            updates.update(error_code=None, error_message=None)
        else:
            if error_code is not None:
                updates["error_code"] = error_code
            if error_message is not None:
                updates["error_message"] = error_message
        if state == "running" and current.started_at is None:
            updates["started_at"] = NOW
        if state in {"completed", "failed"}:
            updates["completed_at"] = NOW
        if state == "cancelled":
            updates["cancelled_at"] = NOW
        self.jobs[str(job_id)] = current.model_copy(update=updates)

    async def get_token(self, token_id):
        token = self.tokens.get(token_id)
        return token.model_copy(deep=True) if token else None

    async def get_token_lifecycle(self, token_id):
        lifecycle = self.lifecycles.get(token_id)
        return lifecycle.model_copy(deep=True) if lifecycle else None

    async def get_projects_by_token(self, token_id):
        return [project.model_copy(deep=True) for project in self.projects.get(token_id, [])]

    async def set_token_desired_state(self, token_id, **fields):
        self.desired_updates.append((token_id, dict(fields)))
        lifecycle = self.lifecycles[token_id]
        self.lifecycles[token_id] = lifecycle.model_copy(
            update={
                "keepalive_enabled": fields["keepalive_enabled"],
                "runtime_mode": fields.get("runtime_mode") or lifecycle.runtime_mode,
                "profile_state": fields.get("profile_state") or lifecycle.profile_state,
            }
        )

    async def finalize_onboarding_account_state(
        self,
        token_id,
        *,
        keepalive_enabled,
        runtime_mode,
        enable_business_if_pending,
        completed_at=None,
    ):
        current = self.tokens[token_id]
        if current.ban_reason == TOKEN_REASON_ONBOARDING_PENDING:
            if enable_business_if_pending:
                current = current.model_copy(
                    update={"is_active": True, "ban_reason": None, "banned_at": None}
                )
            else:
                current = current.model_copy(
                    update={
                        "is_active": False,
                        "ban_reason": TOKEN_REASON_MANUAL_DISABLED,
                        "banned_at": completed_at or NOW,
                    }
                )
            self.tokens[token_id] = current
        await self.set_token_desired_state(
            token_id,
            keepalive_enabled=keepalive_enabled,
            runtime_mode=runtime_mode,
            profile_state="ready",
        )


class CoordinatedClaimDB(FakeDB):
    """Expose the expected atomic claim API while forcing legacy list/start races."""

    def __init__(self):
        super().__init__()
        self.claim_calls: list[str] = []
        self._claim_lock = asyncio.Lock()
        self._running_list_calls = 0
        self._running_list_gate = asyncio.Event()

    async def claim_onboarding_job(self, job_id):
        async with self._claim_lock:
            job_id = str(job_id)
            self.claim_calls.append(job_id)
            job = self.jobs[job_id]
            active_job_exists = any(
                candidate.state in {"running", "failed"}
                and candidate.job_id != job_id
                for candidate in self.jobs.values()
            )
            if job.state != "pending" or active_job_exists:
                return False
            self.jobs[job_id] = job.model_copy(
                update={
                    "state": "running",
                    "phase": "browser_start",
                    "started_at": NOW,
                    "updated_at": NOW,
                }
            )
            return True

    async def list_onboarding_jobs(self, **filters):
        if filters == {"state": "running"} and not self.claim_calls:
            jobs_before_race = await super().list_onboarding_jobs(**filters)
            self._running_list_calls += 1
            if self._running_list_calls == 2:
                self._running_list_gate.set()
            await self._running_list_gate.wait()
            return jobs_before_race
        return await super().list_onboarding_jobs(**filters)


class RacingIdentityClearDB(FakeDB):
    def __init__(self, tokens: list[Token] | None = None):
        super().__init__(tokens)
        self.on_identity_cleared = None

    async def clear_onboarding_browser_identity(
        self,
        job_id,
        *,
        expected_pid,
        expected_start_ticks,
    ):
        cleared = await super().clear_onboarding_browser_identity(
            job_id,
            expected_pid=expected_pid,
            expected_start_ticks=expected_start_ticks,
        )
        if cleared and self.on_identity_cleared is not None:
            self.on_identity_cleared()
        return cleared


class RacingIdentityReplaceDB(FakeDB):
    def __init__(self, tokens: list[Token] | None = None):
        super().__init__(tokens)
        self.before_identity_replace = None

    async def replace_onboarding_browser_identity(self, job_id, **fields):
        if self.before_identity_replace is not None:
            callback = self.before_identity_replace
            self.before_identity_replace = None
            callback()
        return await super().replace_onboarding_browser_identity(job_id, **fields)


class BlockingAwaitingStateDB(FakeDB):
    def __init__(self):
        super().__init__()
        self.awaiting_transition_started = asyncio.Event()
        self.allow_awaiting_transition = asyncio.Event()
        self.blocked_once = False

    async def update_onboarding_job_state(self, job_id, state, **fields):
        if (
            not self.blocked_once
            and state == "running"
            and fields.get("phase") == "awaiting_login"
        ):
            self.blocked_once = True
            self.awaiting_transition_started.set()
            await self.allow_awaiting_transition.wait()
        await super().update_onboarding_job_state(job_id, state, **fields)


class BlockingThenFailAwaitingTransitionDB(FakeDB):
    def __init__(self):
        super().__init__()
        self.awaiting_transition_started = asyncio.Event()
        self.transition_calls = 0

    async def transition_onboarding_job_state(self, job_id, **fields):
        if fields.get("state") == "running" and fields.get("phase") == "awaiting_login":
            self.transition_calls += 1
            if self.transition_calls == 1:
                self.awaiting_transition_started.set()
                await asyncio.Event().wait()
            if self.transition_calls == 2:
                raise RuntimeError("simulated recovery transition failure")
        return await super().transition_onboarding_job_state(job_id, **fields)


class ToggleFailAwaitingTransitionDB(FakeDB):
    def __init__(self):
        super().__init__()
        self.fail_next_awaiting_transition = False

    async def transition_onboarding_job_state(self, job_id, **fields):
        if (
            self.fail_next_awaiting_transition
            and fields.get("state") == "running"
            and fields.get("phase") == "awaiting_login"
        ):
            self.fail_next_awaiting_transition = False
            raise RuntimeError("simulated resumed transition failure")
        return await super().transition_onboarding_job_state(job_id, **fields)


class FailingAwaitingTransitionDB(FakeDB):
    def __init__(self):
        super().__init__()
        self.failed_once = False

    async def transition_onboarding_job_state(self, job_id, **fields):
        if (
            not self.failed_once
            and fields.get("state") == "running"
            and fields.get("phase") == "awaiting_login"
        ):
            self.failed_once = True
            raise RuntimeError("simulated awaiting-login transition failure")
        return await super().transition_onboarding_job_state(job_id, **fields)


class CancelledBeforeAwaitingTransitionDB(FakeDB):
    async def transition_onboarding_job_state(self, job_id, **fields):
        if fields.get("state") == "running" and fields.get("phase") == "awaiting_login":
            current = self.jobs[str(job_id)]
            self.jobs[str(job_id)] = current.model_copy(
                update={
                    "state": "cancelled",
                    "phase": "cancelled",
                    "browser_pid": None,
                    "browser_start_ticks": None,
                    "cancelled_at": NOW,
                }
            )
        return await super().transition_onboarding_job_state(job_id, **fields)


class ClaimEntryRaceDB(FakeDB):
    def __init__(self):
        super().__init__()
        self.on_claim_entry = None

    async def claim_onboarding_job(self, job_id):
        if self.on_claim_entry is not None:
            callback = self.on_claim_entry
            self.on_claim_entry = None
            callback()
        return await super().claim_onboarding_job(job_id)


class IdentityPersistRaceDB(FakeDB):
    def __init__(self):
        super().__init__()
        self.after_identity_persisted = None

    async def replace_onboarding_browser_identity(self, job_id, **fields):
        replaced = await super().replace_onboarding_browser_identity(job_id, **fields)
        if (
            replaced
            and fields.get("browser_pid") is not None
            and fields.get("browser_start_ticks") is not None
            and self.after_identity_persisted is not None
        ):
            callback = self.after_identity_persisted
            self.after_identity_persisted = None
            callback()
        return replaced


class FakeTokenManager:
    def __init__(
        self,
        db: FakeDB,
        snapshots: list[VerifiedAccountSnapshot] | None = None,
    ):
        self.db = db
        self.snapshots = list(snapshots or [snapshot(), snapshot()])
        self.inspect_calls = 0
        self.inspected_session_tokens: list[str] = []
        self.add_calls: list[dict] = []
        self.update_calls: list[tuple[int, dict]] = []
        self.ensure_calls: list[int] = []
        self.ensure_projects: list[Project] = []
        self.enable_calls: list[int] = []
        self.duplicate_email: str | None = None
        self.fail_account_commit_once = False

    async def inspect_account(self, session_token):
        self.inspect_calls += 1
        self.inspected_session_tokens.append(session_token)
        if not self.snapshots:
            raise AssertionError("unexpected account inspection")
        return self.snapshots.pop(0)

    async def find_token_by_email(self, email):
        if self.duplicate_email == normalize_account_email(email):
            raise ValueError("duplicate normalized email")
        matches = [
            token
            for token in self.db.tokens.values()
            if normalize_account_email(token.email) == normalize_account_email(email)
        ]
        if len(matches) > 1:
            raise ValueError("duplicate normalized email")
        return matches[0].model_copy(deep=True) if matches else None

    async def add_token(self, **kwargs):
        self.add_calls.append(dict(kwargs))
        verified = kwargs["verified_snapshot"]
        token_id = max(self.db.tokens, default=0) + 1
        token = Token(
            id=token_id,
            st=verified.st,
            at=verified.at,
            at_expires=verified.at_expires,
            email=verified.email,
            name=verified.name,
            credits=verified.credits,
            user_paygate_tier=verified.user_paygate_tier,
            is_active=kwargs["is_active"],
            ban_reason=kwargs["ban_reason"],
        )
        self.db.tokens[token_id] = token
        self.db.lifecycles[token_id] = TokenLifecycle(token_id=token_id)
        return token.model_copy(deep=True)

    async def update_token(self, token_id, **kwargs):
        self.update_calls.append((token_id, dict(kwargs)))
        if self.fail_account_commit_once:
            self.fail_account_commit_once = False
            raise RuntimeError("simulated account commit failure")
        verified = kwargs["verified_snapshot"]
        current = self.db.tokens[token_id]
        self.db.tokens[token_id] = current.model_copy(
            update={
                "st": verified.st,
                "at": verified.at,
                "at_expires": verified.at_expires,
                "name": verified.name,
                "credits": verified.credits,
                "user_paygate_tier": verified.user_paygate_tier,
            }
        )

    async def ensure_project_pool(self, token_id):
        self.ensure_calls.append(token_id)
        return [project.model_copy(deep=True) for project in self.ensure_projects]

    async def enable_token(self, token_id):
        self.enable_calls.append(token_id)
        current = self.db.tokens[token_id]
        self.db.tokens[token_id] = current.model_copy(
            update={"is_active": True, "ban_reason": None, "banned_at": None}
        )

    async def finalize_onboarding_account_state(
        self,
        token_id,
        *,
        keepalive_enabled,
        runtime_mode,
        enable_business_if_pending,
        completed_at=None,
    ):
        before = self.db.tokens[token_id]
        await self.db.finalize_onboarding_account_state(
            token_id,
            keepalive_enabled=keepalive_enabled,
            runtime_mode=runtime_mode,
            enable_business_if_pending=enable_business_if_pending,
            completed_at=completed_at,
        )
        after = self.db.tokens[token_id]
        if not before.is_active and after.is_active:
            self.enable_calls.append(token_id)


class FakePidfdHandle:
    def __init__(self, process_table, pid, start_ticks):
        self.process_table = process_table
        self.pid = pid
        self.start_ticks = start_ticks
        self.closed = False

    def send_signal(self, stop_signal):
        process = self.process_table.processes.get(self.pid)
        if process is None or process["ticks"] != self.start_ticks:
            raise ProcessLookupError(self.pid)
        self.process_table.signal_hook(self.pid, stop_signal)

    def close(self):
        if not self.closed:
            self.closed = True
            self.process_table.pidfd_closes.append(self.pid)


class FakeProcessTable:
    def __init__(self, pid: int = 4321, ticks: int = 777):
        self.next_pid = pid
        self.next_ticks = ticks
        self.processes: dict[int, dict] = {}
        self.launches: list[tuple[list[str], dict]] = []
        self.stops: list[tuple[int, signal.Signals]] = []
        self.pidfd_opens: list[int] = []
        self.pidfd_closes: list[int] = []
        self.signal_hook = self.stop

    def launch(self, argv, **kwargs):
        pid = self.next_pid
        self.launches.append((list(argv), dict(kwargs)))
        self.processes[pid] = {"ticks": self.next_ticks, "cmdline": tuple(argv)}
        return SimpleNamespace(pid=pid)

    def read_ticks(self, pid):
        process = self.processes.get(pid)
        return process["ticks"] if process else None

    def read_cmdline(self, pid):
        process = self.processes.get(pid)
        return process["cmdline"] if process else None

    def stop(self, pid, stop_signal):
        self.stops.append((pid, stop_signal))
        self.processes.pop(pid, None)

    def open_pidfd(self, pid):
        process = self.processes.get(pid)
        if process is None:
            raise ProcessLookupError(pid)
        self.pidfd_opens.append(pid)
        return FakePidfdHandle(self, pid, process["ticks"])


def cookie_reader_for(value: str = LONG_ST):
    return lambda **_kwargs: [
        SimpleNamespace(
            name="__Secure-next-auth.session-token",
            domain=".labs.google",
            path="/",
            value=value,
            expires=2_000_000_000,
            secure=True,
        )
    ]


def directory_cookie_reader_for(value: str = LONG_ST):
    def read_cookie(**kwargs):
        profile_path = Path(kwargs["cookie_file"]).parent.parent
        if not profile_path.is_dir():
            raise NotADirectoryError(profile_path)
        return cookie_reader_for(value)(**kwargs)

    return read_cookie


def build_service(
    tmp_path: Path,
    *,
    db: FakeDB | None = None,
    token_manager: FakeTokenManager | None = None,
    processes: FakeProcessTable | None = None,
    cookie_reader=None,
    clock=None,
    proxy="http://configured-proxy:7890",
    profile_lease_acquirer=None,
    onboarding_profile_lease_acquirer=None,
    onboarding_operation_lease_acquirer=None,
    profile_preparer=None,
    process_handle_opener=None,
    path_exchanger=None,
    cleanup_nonce_factory=None,
    process_cmdline_reader=None,
    process_start_ticks_reader=None,
):
    from src.services.onboarding import OnboardingService

    database = db or FakeDB()
    manager = token_manager or FakeTokenManager(database)
    process_table = processes or FakeProcessTable()
    service = OnboardingService(
        db=database,
        token_manager=manager,
        profile_base=tmp_path / "profiles",
        browser_executable="/configured/chrome",
        display=":11",
        proxy=proxy,
        session_ttl_seconds=600,
        clock=clock or (lambda: NOW),
        sleep=AsyncMock(),
        process_launcher=process_table.launch,
        process_handle_opener=(process_handle_opener or process_table.open_pidfd),
        cookie_reader=cookie_reader or cookie_reader_for(),
        process_cmdline_reader=process_cmdline_reader or process_table.read_cmdline,
        process_start_ticks_reader=(
            process_start_ticks_reader or process_table.read_ticks
        ),
        **(
            {"profile_lease_acquirer": profile_lease_acquirer}
            if profile_lease_acquirer is not None
            else {}
        ),
        **(
            {
                "onboarding_profile_lease_acquirer": onboarding_profile_lease_acquirer
            }
            if onboarding_profile_lease_acquirer is not None
            else {}
        ),
        **(
            {
                "onboarding_operation_lease_acquirer": onboarding_operation_lease_acquirer
            }
            if onboarding_operation_lease_acquirer is not None
            else {}
        ),
        **(
            {"profile_preparer": profile_preparer}
            if profile_preparer is not None
            else {}
        ),
        **(
            {"path_exchanger": path_exchanger}
            if path_exchanger is not None
            else {}
        ),
        **(
            {"cleanup_nonce_factory": cleanup_nonce_factory}
            if cleanup_nonce_factory is not None
            else {}
        ),
    )
    return service, database, manager, process_table


async def create_and_start(service, **kwargs):
    job = await service.create_job(**kwargs)
    return await service.start_job(job.job_id)


def close_browser(processes: FakeProcessTable, job: OnboardingJob):
    processes.processes.pop(job.browser_pid, None)


def replace_browser_owner(
    processes: FakeProcessTable,
    job: OnboardingJob,
    profile_path: Path,
    *,
    pid: int,
    ticks: int,
):
    processes.processes.pop(job.browser_pid, None)
    processes.processes[pid] = {
        "ticks": ticks,
        "cmdline": (
            "/configured/chrome",
            f"--user-data-dir={profile_path}",
            "https://labs.google/fx/tools/flow",
        ),
    }
    (profile_path / "SingletonLock").symlink_to(f"{socket.gethostname()}-{pid}")


def test_launch_uses_fixed_argv_display_no_shell_and_one_active_job(tmp_path):
    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        second = await service.create_job()
        with pytest.raises(Exception) as conflict:
            await service.start_job(second.job_id)
        return running, processes, conflict.value

    running, processes, conflict = asyncio.run(run())
    argv, kwargs = processes.launches[0]
    assert argv[0] == "/configured/chrome"
    assert argv[-1] == "https://labs.google/fx/tools/flow"
    assert f"--user-data-dir={tmp_path / 'profiles' / '.onboarding' / running.job_id}" in argv
    assert "--proxy-server=http://configured-proxy:7890" in argv
    assert "--no-first-run" in argv
    assert "--password-store=basic" in argv
    assert kwargs["shell"] is False
    assert kwargs["env"]["DISPLAY"] == ":11"
    assert running.state == "running"
    assert running.phase == "awaiting_login"
    assert getattr(conflict, "code", None) == "active_job_exists"
    assert len(processes.launches) == 1


@pytest.mark.parametrize(
    "secret_proxy",
    [
        "http://proxy-user:proxy-password@127.0.0.1:7890",
        "http=http://proxy-user:proxy-password@127.0.0.1:7890",
        (
            "http=http://127.0.0.1:7890;"
            "https=http://proxy-user:proxy-password@127.0.0.1:7890"
        ),
    ],
)
def test_proxy_userinfo_is_rejected_before_onboarding_browser_launch(
    tmp_path,
    secret_proxy,
):
    processes = FakeProcessTable()

    with pytest.raises(ValueError, match="must not include userinfo") as error:
        build_service(tmp_path, processes=processes, proxy=secret_proxy)

    assert "proxy-user" not in str(error.value)
    assert "proxy-password" not in str(error.value)
    assert processes.launches == []


def test_two_service_instances_use_atomic_claim_before_launch(tmp_path):
    async def run():
        db = CoordinatedClaimDB()
        manager = FakeTokenManager(db)
        first_processes = FakeProcessTable(pid=4101)
        second_processes = FakeProcessTable(pid=4102)
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=first_processes,
        )
        second_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=second_processes,
        )
        first_job = await first_service.create_job()
        second_job = await second_service.create_job()
        results = await asyncio.gather(
            first_service.start_job(first_job.job_id),
            second_service.start_job(second_job.job_id),
            return_exceptions=True,
        )
        return db, first_processes, second_processes, results

    db, first_processes, second_processes, results = asyncio.run(run())
    started = [result for result in results if isinstance(result, OnboardingJob)]
    rejected = [result for result in results if isinstance(result, Exception)]
    assert len(started) == 1
    assert started[0].state == "running"
    assert len(rejected) == 1
    assert getattr(rejected[0], "code", None) == "active_job_exists"
    assert len(first_processes.launches) + len(second_processes.launches) == 1
    assert sorted(db.claim_calls) == sorted(db.jobs)


def test_launch_verifies_exact_profile_ownership_before_awaiting_login(tmp_path):
    async def run():
        processes = FakeProcessTable()

        def launch_wrong_profile(argv, **kwargs):
            process = processes.launch(argv, **kwargs)
            processes.processes[process.pid]["cmdline"] = (
                "/configured/chrome",
                "--user-data-dir=/unrelated/profile",
                "https://labs.google/fx/tools/flow",
            )
            return process

        service, _db, _manager, _processes = build_service(
            tmp_path,
            processes=processes,
        )
        service.process_launcher = launch_wrong_profile
        job = await service.create_job()
        with pytest.raises(Exception) as ownership_error:
            await service.start_job(job.job_id)
        failed = await service.get(job.job_id)
        return ownership_error.value, failed, processes

    ownership_error, failed, processes = asyncio.run(run())
    assert getattr(ownership_error, "code", None) == "process_ownership_mismatch"
    assert failed.state == "failed"
    assert failed.phase == "browser_start"
    assert processes.stops == []
    assert len(processes.launches) == 1


def test_launch_reverifies_generation_immediately_before_awaiting_login(tmp_path):
    async def run():
        db = IdentityPersistRaceDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )

        def replace_generation_after_identity_persisted():
            processes.processes[4321]["ticks"] += 1
            processes.processes[4321]["cmdline"] = ("/usr/bin/unrelated-process",)

        db.after_identity_persisted = replace_generation_after_identity_persisted
        job = await service.create_job()
        with pytest.raises(Exception) as ownership_error:
            await service.start_job(job.job_id)
        failed = await service.get(job.job_id)
        return ownership_error.value, failed, processes

    ownership_error, failed, processes = asyncio.run(run())
    assert getattr(ownership_error, "code", None) == "process_ownership_mismatch"
    assert failed.state == "failed"
    assert failed.phase == "browser_start"
    assert processes.stops == []


def test_start_ticks_failure_retains_pid_only_identity_without_unsafe_signal(tmp_path):
    async def run():
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            processes=processes,
            process_start_ticks_reader=lambda _pid: None,
        )
        job = await service.create_job()
        with pytest.raises(Exception) as launch_error:
            await service.start_job(job.job_id)
        failed = await service.get(job.job_id)
        next_job = await service.create_job()
        with pytest.raises(Exception) as blocked_error:
            await service.start_job(next_job.job_id)
        blocked = await service.get(next_job.job_id)
        return launch_error.value, blocked_error.value, failed, blocked, processes

    launch_error, blocked_error, failed, blocked, processes = asyncio.run(run())
    temp_profile = tmp_path / "profiles" / ".onboarding" / failed.job_id
    assert getattr(launch_error, "code", None) == "process_identity_unavailable"
    assert str(launch_error) == "The onboarding browser identity could not be verified."
    assert getattr(blocked_error, "code", None) == "process_identity_unavailable"
    assert failed.state == "failed"
    assert failed.phase == "browser_start"
    assert failed.error_code == "process_identity_unavailable"
    assert failed.error_message == "The onboarding browser identity could not be verified."
    assert failed.browser_pid == 4321
    assert failed.browser_start_ticks is None
    assert blocked.state == "pending"
    assert processes.stops == []
    assert 4321 in processes.processes
    assert len(processes.launches) == 1
    assert temp_profile.exists()


def test_cancel_fails_closed_when_pidfd_is_unavailable(tmp_path):
    def unavailable_pidfd(_pid):
        raise OSError(errno.ENOSYS, "pidfd unavailable")

    async def run():
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            processes=processes,
            process_handle_opener=unavailable_pidfd,
        )
        running = await create_and_start(service)
        with pytest.raises(Exception) as unavailable:
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        return unavailable.value, failed, processes

    unavailable, failed, processes = asyncio.run(run())
    assert getattr(unavailable, "code", None) == "process_identity_unavailable"
    assert failed.state == "failed"
    assert failed.browser_pid == 4321
    assert failed.browser_start_ticks == 777
    assert processes.stops == []


def test_cancel_rechecks_process_generation_immediately_before_term(tmp_path):
    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        observed_ticks = iter(
            [
                running.browser_start_ticks,
                running.browser_start_ticks,
                running.browser_start_ticks + 1,
            ]
        )
        service.process_start_ticks_reader = lambda _pid: next(
            observed_ticks,
            running.browser_start_ticks + 1,
        )
        with pytest.raises(Exception) as mismatch:
            await service.cancel(running.job_id)
        return mismatch.value, processes

    mismatch, processes = asyncio.run(run())
    assert getattr(mismatch, "code", None) == "process_ownership_mismatch"
    assert processes.stops == []


def test_cancel_never_signals_reused_pid_before_kill(tmp_path):
    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        signals = []
        generation_changed = False

        def stopper(pid, stop_signal):
            signals.append((pid, stop_signal))
            if stop_signal == signal.SIGKILL:
                processes.processes.pop(pid, None)

        def cmdline_reader(pid):
            nonlocal generation_changed
            cmdline = processes.read_cmdline(pid)
            if signals and not generation_changed:
                processes.processes[pid]["ticks"] += 1
                processes.processes[pid]["cmdline"] = (
                    "/usr/bin/unrelated-reused-process",
                )
                generation_changed = True
            return cmdline

        processes.signal_hook = stopper
        service.process_cmdline_reader = cmdline_reader
        cancelled = await service.cancel(running.job_id)
        return cancelled, signals, processes

    cancelled, signals, processes = asyncio.run(run())
    assert cancelled.state == "cancelled"
    assert cancelled.browser_pid is None
    assert cancelled.browser_start_ticks is None
    assert signals == [(4321, signal.SIGTERM)]
    assert processes.processes[4321]["cmdline"] == (
        "/usr/bin/unrelated-reused-process",
    )


def test_pid_mismatch_refuses_stop_and_cancel_cleanup(tmp_path):
    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        processes.processes[running.browser_pid]["ticks"] += 1
        with pytest.raises(Exception) as mismatch:
            await service.cancel(running.job_id)
        return mismatch.value, processes, temp_profile

    mismatch, processes, temp_profile = asyncio.run(run())
    assert getattr(mismatch, "code", None) == "process_ownership_mismatch"
    assert processes.stops == []
    assert temp_profile.exists()


def test_cancel_stops_exact_browser_and_removes_only_created_temp(tmp_path):
    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("real profile data", encoding="utf-8")
        cancelled = await service.cancel(running.job_id)
        repeated = await service.cancel(running.job_id)
        return cancelled, repeated, processes, temp_profile, running.browser_pid

    cancelled, repeated, processes, temp_profile, original_pid = asyncio.run(run())
    assert processes.stops == [(original_pid, signal.SIGTERM)]
    assert processes.pidfd_opens == [original_pid]
    assert processes.pidfd_closes == [original_pid]
    assert not temp_profile.exists()
    assert cancelled.state == "cancelled"
    assert cancelled.browser_pid is None
    assert cancelled.browser_start_ticks is None
    assert repeated.state == "cancelled"


def test_cancel_quarantine_detects_successor_after_check_before_delete(tmp_path):
    import src.services.onboarding as onboarding_module

    processes = FakeProcessTable()
    temp_profile = None
    injected = False

    def racing_exchange(left, right):
        nonlocal injected
        if not injected and temp_profile is not None and Path(left) == temp_profile:
            injected = True
            processes.processes[9876] = {
                "ticks": 2222,
                "cmdline": (
                    "/configured/chrome",
                    f"--user-data-dir={temp_profile}",
                    "https://labs.google/fx/tools/flow",
                ),
            }
            (temp_profile / "SingletonLock").symlink_to(
                f"{socket.gethostname()}-9876"
            )
        return onboarding_module._linux_rename_exchange(left, right)

    async def run():
        nonlocal temp_profile
        service, _db, _manager, _processes = build_service(
            tmp_path,
            processes=processes,
            path_exchanger=racing_exchange,
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("preserve", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(Exception) as cleanup_race:
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        return cleanup_race.value, failed

    cleanup_race, failed = asyncio.run(run())
    quarantine = temp_profile.with_name(f".{failed.job_id}.cleanup-quarantine")
    assert injected is True
    assert getattr(cleanup_race, "code", None) == "process_ownership_mismatch"
    assert failed.state == "failed"
    assert failed.phase == "cancel"
    assert (temp_profile / "browser-data").read_text(encoding="utf-8") == "preserve"
    assert not quarantine.exists()
    assert processes.stops == []


def test_cancel_resumes_idempotently_from_cleanup_blocker_after_crash(tmp_path):
    import src.services.onboarding as onboarding_module

    class SimulatedCrash(BaseException):
        pass

    calls = 0

    def crashing_exchange(left, right):
        nonlocal calls
        calls += 1
        onboarding_module._linux_rename_exchange(left, right)
        if calls == 1:
            raise SimulatedCrash()

    async def run():
        service, _db, _manager, processes = build_service(
            tmp_path,
            path_exchanger=crashing_exchange,
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("delete", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(SimulatedCrash):
            await service.cancel(running.job_id)
        quarantine = temp_profile.with_name(
            f".{running.job_id}.cleanup-quarantine"
        )
        crash_state = {
            "temp_blocker": temp_profile.is_file(),
            "quarantine_data": (quarantine / "browser-data").read_text(
                encoding="utf-8"
            ),
        }
        service.path_exchanger = onboarding_module._linux_rename_exchange
        recovered = await service.recover_incomplete()
        cancelled = await service.get(running.job_id)
        return cancelled, recovered, temp_profile, quarantine, crash_state

    cancelled, recovered, temp_profile, quarantine, crash_state = asyncio.run(run())
    assert crash_state == {
        "temp_blocker": True,
        "quarantine_data": "delete",
    }
    assert cancelled.state == "cancelled"
    assert any(job == cancelled for job in recovered)
    assert not temp_profile.exists()
    assert not quarantine.exists()


def test_recovery_accepts_markerless_partial_quarantine_with_matching_identity(
    tmp_path,
    monkeypatch,
):
    import src.services.onboarding as onboarding_module

    class SimulatedCrash(BaseException):
        pass

    real_rmtree = onboarding_module.shutil.rmtree

    def partial_rmtree(path):
        marker = Path(path) / ".flow2api-onboarding"
        marker.unlink()
        raise SimulatedCrash()

    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("partial", encoding="utf-8")
        close_browser(processes, running)
        monkeypatch.setattr(onboarding_module.shutil, "rmtree", partial_rmtree)
        with pytest.raises(SimulatedCrash):
            await service.cancel(running.job_id)
        quarantine = temp_profile.with_name(
            f".{running.job_id}.cleanup-quarantine"
        )
        crash_state = {
            "temp_blocker": temp_profile.is_file(),
            "quarantine_directory": quarantine.is_dir(),
            "marker_missing": not (quarantine / ".flow2api-onboarding").exists(),
            "data": (quarantine / "browser-data").read_text(encoding="utf-8"),
        }
        monkeypatch.setattr(onboarding_module.shutil, "rmtree", real_rmtree)
        recovered = await service.recover_incomplete()
        cancelled = await service.get(running.job_id)
        return cancelled, recovered, temp_profile, quarantine, crash_state

    cancelled, recovered, temp_profile, quarantine, crash_state = asyncio.run(run())
    assert crash_state == {
        "temp_blocker": True,
        "quarantine_directory": True,
        "marker_missing": True,
        "data": "partial",
    }
    assert cancelled.state == "cancelled"
    assert any(job == cancelled for job in recovered)
    assert not temp_profile.exists()
    assert not quarantine.exists()


@pytest.mark.parametrize(
    "replacement_type",
    ["different-directory", "symlink", "xattr-absent", "xattr-mismatch"],
)
def test_recovery_rejects_quarantine_identity_mismatch(
    tmp_path,
    monkeypatch,
    replacement_type,
):
    import src.services.onboarding as onboarding_module

    class SimulatedCrash(BaseException):
        pass

    real_rmtree = onboarding_module.shutil.rmtree

    def interrupted_rmtree(_path):
        raise SimulatedCrash()

    async def run():
        service, _db, _manager, processes = build_service(
            tmp_path,
            cleanup_nonce_factory=lambda: "a" * 64,
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("original", encoding="utf-8")
        close_browser(processes, running)
        monkeypatch.setattr(onboarding_module.shutil, "rmtree", interrupted_rmtree)
        with pytest.raises(SimulatedCrash):
            await service.cancel(running.job_id)
        quarantine = temp_profile.with_name(
            f".{running.job_id}.cleanup-quarantine"
        )
        monkeypatch.setattr(onboarding_module.shutil, "rmtree", real_rmtree)
        if replacement_type == "different-directory":
            original_quarantine = quarantine.with_name(
                f"{quarantine.name}.original"
            )
            quarantine.rename(original_quarantine)
            quarantine.mkdir()
            (quarantine / ".flow2api-onboarding").write_text(
                running.job_id,
                encoding="ascii",
            )
            (quarantine / "replacement").write_text("do not delete", encoding="utf-8")
        elif replacement_type == "symlink":
            real_rmtree(quarantine)
            outside = tmp_path / "outside-replacement"
            outside.mkdir()
            quarantine.symlink_to(outside, target_is_directory=True)
        elif replacement_type == "xattr-absent":
            os.removexattr(
                quarantine,
                "user.flow2api-cleanup-nonce",
                follow_symlinks=False,
            )
        else:
            os.setxattr(
                quarantine,
                "user.flow2api-cleanup-nonce",
                b"b" * 64,
                follow_symlinks=False,
            )
        recovered = await service.recover_incomplete()
        failed = await service.get(running.job_id)
        return failed, recovered, temp_profile, quarantine

    failed, recovered, temp_profile, quarantine = asyncio.run(run())
    assert failed.state == "failed"
    assert failed.phase == "cancel"
    assert failed.error_code == "unsafe_profile_path"
    assert any(job == failed for job in recovered)
    assert temp_profile.is_file()
    assert quarantine.exists()
    if replacement_type == "different-directory":
        assert (quarantine / "replacement").read_text(encoding="utf-8") == "do not delete"
    elif replacement_type.startswith("xattr-"):
        assert (quarantine / "browser-data").read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(
    "failure_phase",
    ["open", "write", "file-fsync", "publish", "parent-fsync"],
)
def test_cleanup_blocker_atomic_publication_failures_are_retryable(
    tmp_path,
    failure_phase,
):
    async def run():
        service, _db, _manager, processes = build_service(
            tmp_path,
            cleanup_nonce_factory=lambda: "a" * 64,
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("retry", encoding="utf-8")
        close_browser(processes, running)
        method_name = {
            "open": "_open_cleanup_staging",
            "write": "_write_cleanup_staging",
            "file-fsync": "_fsync_cleanup_file",
            "publish": "_publish_cleanup_staging",
            "parent-fsync": "_fsync_cleanup_parent",
        }[failure_phase]
        original_method = getattr(service, method_name, None)

        def fail(*_args, **_kwargs):
            raise OSError(errno.EIO, f"simulated {failure_phase} failure")

        setattr(service, method_name, fail)
        with pytest.raises(Exception) as publication_error:
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        quarantine = temp_profile.with_name(
            f".{running.job_id}.cleanup-quarantine"
        )
        staging = temp_profile.with_name(
            f".{running.job_id}.cleanup-blocker-staging"
        )
        failure_state = {
            "temp_directory": temp_profile.is_dir(),
            "published_blocker": quarantine.is_file(),
            "staging_exists": staging.exists(),
            "nonce_present": (
                os.getxattr(
                    temp_profile,
                    "user.flow2api-cleanup-nonce",
                    follow_symlinks=False,
                )
                if temp_profile.is_dir()
                and "user.flow2api-cleanup-nonce"
                in os.listxattr(temp_profile, follow_symlinks=False)
                else None
            ),
        }
        if original_method is None:
            delattr(service, method_name)
        else:
            setattr(service, method_name, original_method)
        cancelled = await service.cancel(running.job_id)
        return (
            publication_error.value,
            failed,
            cancelled,
            temp_profile,
            quarantine,
            staging,
            failure_state,
        )

    (
        publication_error,
        failed,
        cancelled,
        temp_profile,
        quarantine,
        staging,
        failure_state,
    ) = asyncio.run(run())
    assert getattr(publication_error, "code", None) == "unsafe_profile_path"
    assert failed.state == "failed"
    assert failed.phase == "cancel"
    assert failure_state["temp_directory"] is True
    assert failure_state["staging_exists"] is False
    if failure_phase == "parent-fsync":
        assert failure_state["published_blocker"] is True
        assert failure_state["nonce_present"] == b"a" * 64
    else:
        assert failure_state["published_blocker"] is False
        assert failure_state["nonce_present"] is None
    assert cancelled.state == "cancelled"
    assert not temp_profile.exists()
    assert not quarantine.exists()
    assert not staging.exists()


def test_cleanup_partial_staging_crash_is_recovered_idempotently(tmp_path):
    class SimulatedCrash(BaseException):
        pass

    async def run():
        service, _db, _manager, processes = build_service(
            tmp_path,
            cleanup_nonce_factory=lambda: "a" * 64,
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("partial", encoding="utf-8")
        close_browser(processes, running)
        original_write = getattr(service, "_write_cleanup_staging", None)

        def crash_after_partial_write(descriptor, _payload):
            os.write(descriptor, b"partial-blocker")
            raise SimulatedCrash()

        service._write_cleanup_staging = crash_after_partial_write
        with pytest.raises(SimulatedCrash):
            await service.cancel(running.job_id)
        staging = temp_profile.with_name(
            f".{running.job_id}.cleanup-blocker-staging"
        )
        crash_state = {
            "temp_directory": temp_profile.is_dir(),
            "staging_partial": staging.read_bytes() == b"partial-blocker",
        }
        if original_write is None:
            delattr(service, "_write_cleanup_staging")
        else:
            service._write_cleanup_staging = original_write
        recovered = await service.recover_incomplete()
        cancelled = await service.get(running.job_id)
        return cancelled, recovered, temp_profile, staging, crash_state

    cancelled, recovered, temp_profile, staging, crash_state = asyncio.run(run())
    assert crash_state == {
        "temp_directory": True,
        "staging_partial": True,
    }
    assert cancelled.state == "cancelled"
    assert any(job == cancelled for job in recovered)
    assert not temp_profile.exists()
    assert not staging.exists()


def test_cleanup_partial_staging_symlink_is_rejected(tmp_path):
    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        close_browser(processes, running)
        staging = temp_profile.with_name(
            f".{running.job_id}.cleanup-blocker-staging"
        )
        outside = tmp_path / "outside-staging"
        outside.write_text("preserve", encoding="utf-8")
        staging.symlink_to(outside)
        with pytest.raises(Exception) as unsafe:
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        return unsafe.value, failed, temp_profile, staging, outside

    unsafe, failed, temp_profile, staging, outside = asyncio.run(run())
    assert getattr(unsafe, "code", None) == "unsafe_profile_path"
    assert failed.state == "failed"
    assert failed.phase == "cancel"
    assert temp_profile.is_dir()
    assert staging.is_symlink()
    assert outside.read_text(encoding="utf-8") == "preserve"


def test_cleanup_fails_closed_when_user_xattrs_are_unsupported(
    tmp_path,
    monkeypatch,
):
    import src.services.onboarding as onboarding_module

    def unsupported_xattr(*_args, **_kwargs):
        raise OSError(errno.EOPNOTSUPP, "user xattrs unavailable")

    async def run():
        service, _db, _manager, processes = build_service(
            tmp_path,
            cleanup_nonce_factory=lambda: "a" * 64,
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("preserve", encoding="utf-8")
        close_browser(processes, running)
        monkeypatch.setattr(onboarding_module.os, "setxattr", unsupported_xattr)
        with pytest.raises(Exception) as unsupported:
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        return unsupported.value, failed, temp_profile

    unsupported, failed, temp_profile = asyncio.run(run())
    quarantine = temp_profile.with_name(f".{failed.job_id}.cleanup-quarantine")
    assert getattr(unsupported, "code", None) == "unsafe_profile_path"
    assert failed.state == "failed"
    assert failed.phase == "cancel"
    assert (temp_profile / "browser-data").read_text(encoding="utf-8") == "preserve"
    assert not quarantine.exists()


def test_recovery_never_deletes_recreated_quarantine_on_simulated_inode_reuse(
    tmp_path,
):
    class SimulatedCrash(BaseException):
        pass

    async def run():
        service, _db, _manager, processes = build_service(
            tmp_path,
            cleanup_nonce_factory=lambda: "a" * 64,
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("delete", encoding="utf-8")
        close_browser(processes, running)
        original_remove = service._remove_cleanup_blocker

        def crash_before_blocker_removal(_path, _job_id):
            raise SimulatedCrash()

        service._remove_cleanup_blocker = crash_before_blocker_removal
        with pytest.raises(SimulatedCrash):
            await service.cancel(running.job_id)
        service._remove_cleanup_blocker = original_remove
        quarantine = temp_profile.with_name(
            f".{running.job_id}.cleanup-quarantine"
        )
        assert not quarantine.exists()
        blocker_identity = service._read_cleanup_blocker_identity(
            temp_profile,
            running.job_id,
        )
        quarantine.mkdir()
        (quarantine / ".flow2api-onboarding").write_text(
            running.job_id,
            encoding="ascii",
        )
        (quarantine / "unrelated").write_text("preserve", encoding="utf-8")
        real_directory_stat = quarantine.lstat()

        def reused_inode_stat(path):
            if Path(path) == quarantine:
                return SimpleNamespace(
                    st_mode=real_directory_stat.st_mode,
                    st_dev=blocker_identity[0],
                    st_ino=blocker_identity[1],
                )
            return Path(path).lstat()

        service._cleanup_directory_stat = reused_inode_stat
        recovered = await service.recover_incomplete()
        failed = await service.get(running.job_id)
        return failed, recovered, temp_profile, quarantine

    failed, recovered, temp_profile, quarantine = asyncio.run(run())
    assert failed.state == "failed"
    assert failed.phase == "cancel"
    assert failed.error_code == "unsafe_profile_path"
    assert any(job == failed for job in recovered)
    assert temp_profile.is_file()
    assert (quarantine / "unrelated").read_text(encoding="utf-8") == "preserve"


def test_awaiting_login_transition_failure_stops_launched_generation(tmp_path):
    async def run():
        db = FailingAwaitingTransitionDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        job = await service.create_job()
        with pytest.raises(Exception) as transition_error:
            await service.start_job(job.job_id)
        current = await service.get(job.job_id)
        return transition_error.value, current, processes

    transition_error, current, processes = asyncio.run(run())
    assert getattr(transition_error, "code", None) == "process_launch_failed"
    assert current.state == "failed"
    assert current.phase == "browser_start"
    assert current.browser_pid is None
    assert current.browser_start_ticks is None
    assert processes.processes == {}
    assert processes.stops == [(4321, signal.SIGTERM)]


def test_resumed_transition_failure_preserves_profile_and_remains_resumable(tmp_path):
    async def run():
        db = ToggleFailAwaitingTransitionDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
            cookie_reader=lambda **_kwargs: [],
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(Exception):
            await service.finalize(running.job_id)

        db.fail_next_awaiting_transition = True
        with pytest.raises(Exception) as transition_error:
            await service.start_job(running.job_id)
        failed = await service.get(running.job_id)
        resumed = await service.start_job(running.job_id)
        return transition_error.value, failed, resumed, temp_profile, processes

    transition_error, failed, resumed, temp_profile, processes = asyncio.run(run())
    assert getattr(transition_error, "code", None) == "process_launch_failed"
    assert failed.state == "failed"
    assert failed.phase == "verify_account"
    assert failed.browser_pid is None
    assert failed.browser_start_ticks is None
    assert resumed.state == "running"
    assert resumed.phase == "awaiting_login"
    assert (temp_profile / "operator-progress").read_text(encoding="utf-8") == "preserve"
    assert len(processes.launches) == 3


def test_start_retry_recovers_interrupted_browser_start(tmp_path):
    async def run():
        db = BlockingAwaitingStateDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        second_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        job = await first_service.create_job()
        start_task = asyncio.create_task(first_service.start_job(job.job_id))
        await db.awaiting_transition_started.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        interrupted = await first_service.get(job.job_id)
        resumed = await second_service.start_job(job.job_id)
        return interrupted, resumed, processes

    interrupted, resumed, processes = asyncio.run(run())
    assert interrupted.state == "running"
    assert interrupted.phase == "browser_start"
    assert resumed.state == "running"
    assert resumed.phase == "awaiting_login"
    assert resumed.browser_pid == interrupted.browser_pid
    assert resumed.browser_start_ticks == interrupted.browser_start_ticks
    assert len(processes.launches) == 1
    assert resumed.browser_pid in processes.processes


def test_recovery_transition_failure_stops_browser_and_remains_resumable(tmp_path):
    async def run():
        db = BlockingThenFailAwaitingTransitionDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        second_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        job = await first_service.create_job()
        start_task = asyncio.create_task(first_service.start_job(job.job_id))
        await db.awaiting_transition_started.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

        failed = await second_service.start_job(job.job_id)
        resumed = await second_service.start_job(job.job_id)
        return failed, resumed, processes

    failed, resumed, processes = asyncio.run(run())
    assert failed.state == "failed"
    assert failed.phase == "verify_account"
    assert failed.browser_pid is None
    assert failed.browser_start_ticks is None
    assert resumed.state == "running"
    assert resumed.phase == "awaiting_login"
    assert len(processes.launches) == 2
    assert processes.stops == [(4321, signal.SIGTERM)]
    assert resumed.browser_pid in processes.processes


def test_cross_process_cancel_cannot_race_start_awaiting_login_publication(tmp_path):
    async def run():
        db = BlockingAwaitingStateDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        second_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        job = await first_service.create_job()
        start_task = asyncio.create_task(first_service.start_job(job.job_id))
        await db.awaiting_transition_started.wait()
        try:
            await second_service.cancel(job.job_id)
        except Exception as error:
            cancel_error = error
        else:
            cancel_error = None
        db.allow_awaiting_transition.set()
        started = await start_task
        current = await first_service.get(job.job_id)
        return cancel_error, started, current, processes

    cancel_error, started, current, processes = asyncio.run(run())
    assert getattr(cancel_error, "code", None) == "active_job_exists"
    assert started.state == "running"
    assert started.phase == "awaiting_login"
    assert current.state == "running"
    assert current.phase == "awaiting_login"
    assert current.browser_pid in processes.processes


def test_start_never_overwrites_terminal_state_during_awaiting_login_publish(tmp_path):
    async def run():
        db = CancelledBeforeAwaitingTransitionDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        job = await service.create_job()
        with pytest.raises(Exception) as publish_error:
            await service.start_job(job.job_id)
        current = await service.get(job.job_id)
        return publish_error.value, current, processes

    publish_error, current, processes = asyncio.run(run())
    assert getattr(publish_error, "code", None) == "active_job_exists"
    assert current.state == "cancelled"
    assert current.phase == "cancelled"
    assert current.browser_pid is None
    assert current.browser_start_ticks is None
    assert processes.processes == {}
    assert processes.stops == [(4321, signal.SIGTERM)]


def test_cross_process_cancel_cannot_race_active_finalize(tmp_path):
    existing = Token(id=39, st=LONG_ST, email="owner@example.com")

    class BlockingInspectTokenManager(FakeTokenManager):
        def __init__(self, db):
            super().__init__(db)
            self.inspect_started = asyncio.Event()
            self.allow_inspect = asyncio.Event()
            self.blocked_once = False

        async def inspect_account(self, session_token):
            if not self.blocked_once:
                self.blocked_once = True
                self.inspect_started.set()
                await self.allow_inspect.wait()
            return await super().inspect_account(session_token)

    async def run():
        db = FakeDB([existing])
        manager = BlockingInspectTokenManager(db)
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        second_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        running = await create_and_start(first_service, target_token_id=existing.id)
        finalize_task = asyncio.create_task(first_service.finalize(running.job_id))
        await manager.inspect_started.wait()
        with pytest.raises(Exception) as cancel_error:
            await second_service.cancel(running.job_id)
        state_during_finalize = await first_service.get(running.job_id)
        manager.allow_inspect.set()
        completed = await finalize_task
        return cancel_error.value, state_during_finalize, completed

    cancel_error, state_during_finalize, completed = asyncio.run(run())
    assert getattr(cancel_error, "code", None) == "active_job_exists"
    assert state_during_finalize.state == "running"
    assert completed.state == "completed"
    assert completed.phase == "completed"


def test_recovery_skips_job_while_finalize_holds_operation_lease(tmp_path):
    existing = Token(id=41, st=LONG_ST, email="owner@example.com")

    class BlockingInspectTokenManager(FakeTokenManager):
        def __init__(self, db):
            super().__init__(db)
            self.inspect_started = asyncio.Event()
            self.allow_inspect = asyncio.Event()
            self.blocked_once = False

        async def inspect_account(self, session_token):
            if not self.blocked_once:
                self.blocked_once = True
                self.inspect_started.set()
                await self.allow_inspect.wait()
            return await super().inspect_account(session_token)

    async def run():
        db = FakeDB([existing])
        manager = BlockingInspectTokenManager(db)
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        second_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        running = await create_and_start(first_service, target_token_id=existing.id)
        finalize_task = asyncio.create_task(first_service.finalize(running.job_id))
        await manager.inspect_started.wait()
        recovered = await second_service.recover_incomplete()
        state_during_finalize = await first_service.get(running.job_id)
        manager.allow_inspect.set()
        completed = await finalize_task
        return recovered, state_during_finalize, completed

    recovered, state_during_finalize, completed = asyncio.run(run())
    matching = next(job for job in recovered if job.job_id == completed.job_id)
    assert matching.state == "running"
    assert matching.phase == "awaiting_login"
    assert state_during_finalize.state == "running"
    assert state_during_finalize.phase == "awaiting_login"
    assert completed.state == "completed"


def test_stale_cancel_cannot_overwrite_completed_job(tmp_path):
    existing = Token(id=40, st=LONG_ST, email="owner@example.com")

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        second_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        running = await create_and_start(first_service, target_token_id=existing.id)
        stale = await second_service.get(running.job_id)
        completed = await first_service.finalize(running.job_id)
        stale_cancel_result = await second_service._cancel_locked(stale)
        current = await first_service.get(running.job_id)
        return completed, stale_cancel_result, current

    completed, stale_cancel_result, current = asyncio.run(run())
    assert completed.state == "completed"
    assert stale_cancel_result.state == "completed"
    assert current.state == "completed"
    assert current.phase == "completed"


def test_cancel_adopts_and_stops_verified_successor_before_profile_cleanup(tmp_path):
    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("live profile", encoding="utf-8")
        replace_browser_owner(
            processes,
            running,
            temp_profile,
            pid=5432,
            ticks=888,
        )
        cancelled = await service.cancel(running.job_id)
        return cancelled, processes, temp_profile

    cancelled, processes, temp_profile = asyncio.run(run())
    assert cancelled.state == "cancelled"
    assert cancelled.browser_pid is None
    assert cancelled.browser_start_ticks is None
    assert processes.stops == [(5432, signal.SIGTERM)]
    assert not temp_profile.exists()


def test_finalize_stops_successor_that_appears_after_browser_identity_clear(tmp_path):
    existing = Token(id=36, st=LONG_ST, email="owner@example.com")
    successor_started = False

    async def run():
        nonlocal successor_started
        db = RacingIdentityClearDB([existing])
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        running = await create_and_start(service, target_token_id=existing.id)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("preserve", encoding="utf-8")

        def start_successor_after_clear():
            nonlocal successor_started
            if successor_started:
                return
            successor_started = True
            processes.processes[5432] = {
                "ticks": 888,
                "cmdline": (
                    "/configured/chrome",
                    f"--user-data-dir={temp_profile}",
                    "https://labs.google/fx/tools/flow",
                ),
            }
            (temp_profile / "SingletonLock").symlink_to(
                f"{socket.gethostname()}-5432"
            )

        db.on_identity_cleared = start_successor_after_clear
        completed = await service.finalize(running.job_id)
        return completed, processes, temp_profile

    completed, processes, temp_profile = asyncio.run(run())
    destination = tmp_path / "profiles" / str(existing.id)
    assert successor_started is True
    assert completed.state == "completed"
    assert completed.browser_pid is None
    assert completed.browser_start_ticks is None
    assert processes.stops == [
        (4321, signal.SIGTERM),
        (5432, signal.SIGTERM),
    ]
    assert not temp_profile.exists()
    assert (destination / "browser-data").read_text(encoding="utf-8") == "preserve"


def test_finalize_never_clears_browser_identity_changed_during_stop(tmp_path):
    existing = Token(id=37, st=LONG_ST, email="owner@example.com")

    class IdentityChangedDuringClearDB(FakeDB):
        def __init__(self):
            super().__init__([existing])
            self.changed = False

        async def clear_onboarding_browser_identity(
            self,
            job_id,
            *,
            expected_pid,
            expected_start_ticks,
        ):
            if not self.changed:
                self.changed = True
                current = self.jobs[str(job_id)]
                self.jobs[str(job_id)] = current.model_copy(
                    update={"browser_pid": 6543, "browser_start_ticks": 999}
                )
                return False
            return await super().clear_onboarding_browser_identity(
                job_id,
                expected_pid=expected_pid,
                expected_start_ticks=expected_start_ticks,
            )

    async def run():
        db = IdentityChangedDuringClearDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        running = await create_and_start(service, target_token_id=existing.id)
        processes.processes[6543] = {
            "ticks": 999,
            "cmdline": ("/usr/bin/unrelated-process",),
        }
        with pytest.raises(Exception) as identity_error:
            await service.finalize(running.job_id)
        failed = await service.get(running.job_id)
        return identity_error.value, failed, processes

    identity_error, failed, processes = asyncio.run(run())
    assert getattr(identity_error, "code", None) == "process_ownership_mismatch"
    assert failed.state == "failed"
    assert failed.phase == "stop_browser"
    assert failed.browser_pid == 6543
    assert failed.browser_start_ticks == 999
    assert processes.stops == [(4321, signal.SIGTERM)]
    assert processes.processes[6543]["cmdline"] == ("/usr/bin/unrelated-process",)


def test_cancel_maps_browser_identity_clear_database_failure_to_safe_state(tmp_path):
    class FailingIdentityClearDB(FakeDB):
        async def clear_onboarding_browser_identity(self, *_args, **_kwargs):
            raise RuntimeError("database failure with sensitive details")

    async def run():
        db = FailingIdentityClearDB()
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
        )
        running = await create_and_start(service)
        with pytest.raises(Exception) as clear_error:
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        return clear_error.value, failed, processes, running

    clear_error, failed, processes, running = asyncio.run(run())
    assert getattr(clear_error, "code", None) == "process_identity_unavailable"
    assert "sensitive" not in str(clear_error)
    assert failed.state == "failed"
    assert failed.phase == "cancel"
    assert failed.error_code == "process_identity_unavailable"
    assert failed.browser_pid == running.browser_pid
    assert failed.browser_start_ticks == running.browser_start_ticks
    assert processes.stops == [(running.browser_pid, signal.SIGTERM)]


def test_cancel_maps_reload_failure_after_identity_clear_to_safe_state(tmp_path):
    class FailingReloadAfterClearDB(FakeDB):
        def __init__(self):
            super().__init__()
            self.fail_next_get = False

        async def clear_onboarding_browser_identity(self, job_id, **fields):
            cleared = await super().clear_onboarding_browser_identity(job_id, **fields)
            self.fail_next_get = cleared
            return cleared

        async def get_onboarding_job(self, job_id):
            if self.fail_next_get:
                self.fail_next_get = False
                raise RuntimeError("reload failure with sensitive details")
            return await super().get_onboarding_job(job_id)

    async def run():
        db = FailingReloadAfterClearDB()
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
        )
        running = await create_and_start(service)
        with pytest.raises(Exception) as reload_error:
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        return reload_error.value, failed, processes, running

    reload_error, failed, processes, running = asyncio.run(run())
    assert getattr(reload_error, "code", None) == "process_identity_unavailable"
    assert "sensitive" not in str(reload_error)
    assert failed.state == "failed"
    assert failed.phase == "cancel"
    assert failed.error_code == "process_identity_unavailable"
    assert failed.browser_pid is None
    assert failed.browser_start_ticks is None
    assert processes.stops == [(running.browser_pid, signal.SIGTERM)]


def test_finalize_clears_dead_identity_repopulated_after_successful_cas(tmp_path):
    existing = Token(id=38, st=LONG_ST, email="owner@example.com")
    repopulated = False

    async def run():
        nonlocal repopulated
        db = RacingIdentityClearDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=FakeProcessTable(),
        )
        running = await create_and_start(service, target_token_id=existing.id)

        def repopulate_dead_identity():
            nonlocal repopulated
            if repopulated:
                return
            repopulated = True
            current = db.jobs[running.job_id]
            db.jobs[running.job_id] = current.model_copy(
                update={"browser_pid": 6543, "browser_start_ticks": 999}
            )

        db.on_identity_cleared = repopulate_dead_identity
        completed = await service.finalize(running.job_id)
        return completed, processes

    completed, processes = asyncio.run(run())
    assert repopulated is True
    assert completed.state == "completed"
    assert completed.browser_pid is None
    assert completed.browser_start_ticks is None
    assert processes.stops == [(4321, signal.SIGTERM)]


def test_finalize_adopts_and_stops_verified_successor_before_profile_migration(tmp_path):
    existing = Token(id=26, st=LONG_ST, email="owner@example.com")

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
        )
        running = await create_and_start(service, target_token_id=26)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("live profile", encoding="utf-8")
        replace_browser_owner(
            processes,
            running,
            temp_profile,
            pid=6543,
            ticks=999,
        )
        completed = await service.finalize(running.job_id)
        return completed, processes, temp_profile

    completed, processes, temp_profile = asyncio.run(run())
    destination = tmp_path / "profiles" / "26"
    assert completed.state == "completed"
    assert completed.browser_pid is None
    assert completed.browser_start_ticks is None
    assert processes.stops == [(6543, signal.SIGTERM)]
    assert not temp_profile.exists()
    assert (destination / "browser-data").read_text(encoding="utf-8") == "live profile"


@pytest.mark.parametrize(
    "recover_before_finalize",
    [False, True],
    ids=["direct-retry", "retry-after-recovery"],
)
def test_finalize_retries_failed_stop_after_proven_stale_lock_without_signaling_reused_pid(
    tmp_path,
    recover_before_finalize,
):
    from src.services.keepalive.profile import (
        acquire_profile_path_lease,
        prepare_profile,
    )

    existing = Token(id=35, st=LONG_ST, email="owner@example.com")
    lease_observations = []

    def acquire_onboarding_lease(base_dir, profile_path, lease_key):
        lease = acquire_profile_path_lease(base_dir, profile_path, lease_key)
        lease_observations.append(("acquired", lease))
        return lease

    def prepare_while_leased(lease, **kwargs):
        assert lease.active is True
        lease_observations.append(("prepared", lease))
        return prepare_profile(lease, **kwargs)

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            onboarding_profile_lease_acquirer=acquire_onboarding_lease,
            profile_preparer=prepare_while_leased,
        )
        running = await create_and_start(service, target_token_id=existing.id)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "Default").mkdir()
        (temp_profile / "Default" / "Cookies").write_bytes(b"logged-in-profile")
        (temp_profile / "Preferences").write_text("preserve", encoding="utf-8")
        (temp_profile / "SingletonCookie").symlink_to("stale-cookie-secret")
        (temp_profile / "SingletonSocket").symlink_to(
            "/tmp/nonexistent-flow2api-onboarding-socket"
        )
        (temp_profile / "SingletonLock").symlink_to(
            f"{socket.gethostname()}-{running.browser_pid}"
        )

        processes.processes[running.browser_pid] = {
            "ticks": running.browser_start_ticks + 1,
            "cmdline": ("/usr/bin/unrelated-reused-process",),
        }
        await db.update_onboarding_job_state(
            running.job_id,
            "failed",
            phase="stop_browser",
            error_code="process_ownership_mismatch",
            error_message="The recorded browser process is not owned by this onboarding job.",
        )

        recovered = (
            await service.recover_incomplete() if recover_before_finalize else []
        )
        before_retry = await service.get(running.job_id)
        completed = await service.finalize(running.job_id)
        return (
            recovered,
            before_retry,
            completed,
            processes,
            temp_profile,
            running.browser_pid,
        )

    (
        recovered,
        before_retry,
        completed,
        processes,
        temp_profile,
        recorded_pid,
    ) = asyncio.run(run())
    destination = tmp_path / "profiles" / str(existing.id)

    assert before_retry.state == "failed"
    assert before_retry.phase == "stop_browser"
    if recover_before_finalize:
        recovered_job = next(job for job in recovered if job.job_id == completed.job_id)
        assert recovered_job.state == "failed"
        assert recovered_job.phase == "stop_browser"
        assert before_retry.browser_pid is None
        assert before_retry.browser_start_ticks is None
    else:
        assert recovered == []
        assert before_retry.browser_pid == recorded_pid
        assert before_retry.browser_start_ticks is not None
    assert completed.state == "completed"
    assert completed.resolved_token_id == existing.id
    assert not temp_profile.exists()
    assert (destination / "Preferences").read_text(encoding="utf-8") == "preserve"
    assert not os.path.lexists(destination / "SingletonCookie")
    assert not os.path.lexists(destination / "SingletonSocket")
    assert not os.path.lexists(destination / "SingletonLock")
    assert processes.stops == []
    assert processes.pidfd_opens == []
    assert recorded_pid in processes.processes
    expected_lease_events = ["acquired", "prepared"]
    if recover_before_finalize:
        expected_lease_events *= 2
    assert [event for event, _lease in lease_observations] == expected_lease_events
    for index in range(0, len(lease_observations), 2):
        assert lease_observations[index][1] is lease_observations[index + 1][1]
        assert lease_observations[index][1].active is False


def test_account_inspection_failure_clears_stopped_identity_and_resumes_safely(
    tmp_path,
):
    async def run():
        service, _db, manager, processes = build_service(tmp_path)
        manager.inspect_account = AsyncMock(
            side_effect=RuntimeError("simulated rejected credits bearer")
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")

        with pytest.raises(Exception) as inspection_error:
            await service.finalize(running.job_id)
        failed = await service.get(running.job_id)

        processes.processes[running.browser_pid] = {
            "ticks": running.browser_start_ticks + 1,
            "cmdline": ("/usr/bin/unrelated-reused-process",),
        }
        processes.next_pid = 5432
        processes.next_ticks = 888
        resumed = await service.start_job(running.job_id)
        return inspection_error.value, failed, resumed, processes, temp_profile

    inspection_error, failed, resumed, processes, temp_profile = asyncio.run(run())
    assert getattr(inspection_error, "code", None) == "account_inspection_failed"
    assert failed.state == "failed"
    assert failed.phase == "verify_account"
    assert failed.error_code == "account_inspection_failed"
    assert failed.browser_pid is None
    assert failed.browser_start_ticks is None
    assert resumed.state == "running"
    assert resumed.phase == "awaiting_login"
    assert resumed.browser_pid == 5432
    assert resumed.browser_start_ticks == 888
    assert processes.stops == [(4321, signal.SIGTERM)]
    assert processes.processes[4321]["cmdline"] == (
        "/usr/bin/unrelated-reused-process",
    )
    assert (temp_profile / "operator-progress").read_text(encoding="utf-8") == "preserve"


def test_failed_login_can_resume_same_profile_after_stale_owner_cleanup(tmp_path):
    signed_in = False

    def mutable_cookie_reader(**kwargs):
        if not signed_in:
            return []
        return cookie_reader_for()(**kwargs)

    async def run():
        nonlocal signed_in
        service, _db, manager, processes = build_service(
            tmp_path,
            cookie_reader=mutable_cookie_reader,
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")
        original_profile_inode = temp_profile.stat().st_ino
        close_browser(processes, running)

        with pytest.raises(Exception) as login_error:
            await service.finalize(running.job_id)
        failed = await service.get(running.job_id)

        (temp_profile / "SingletonCookie").symlink_to("stale-cookie-secret")
        (temp_profile / "SingletonSocket").symlink_to(
            "/tmp/nonexistent-flow2api-resume-socket"
        )
        (temp_profile / "SingletonLock").symlink_to(
            f"{socket.gethostname()}-{running.browser_pid}"
        )
        processes.processes[running.browser_pid] = {
            "ticks": running.browser_start_ticks + 1,
            "cmdline": ("/usr/bin/unrelated-reused-process",),
        }
        processes.next_pid = 5432
        processes.next_ticks = 888

        resumed = await service.start_job(running.job_id)
        profile_inode_after_resume = temp_profile.stat().st_ino
        stale_artifacts_after_resume = tuple(
            os.path.lexists(temp_profile / artifact)
            for artifact in ("SingletonCookie", "SingletonSocket", "SingletonLock")
        )
        signed_in = True
        close_browser(processes, resumed)
        completed = await service.finalize(running.job_id)
        return (
            login_error.value,
            failed,
            resumed,
            completed,
            manager,
            processes,
            original_profile_inode,
            profile_inode_after_resume,
            stale_artifacts_after_resume,
        )

    (
        login_error,
        failed,
        resumed,
        completed,
        manager,
        processes,
        original_profile_inode,
        profile_inode_after_resume,
        stale_artifacts_after_resume,
    ) = asyncio.run(run())
    destination = tmp_path / "profiles" / str(completed.resolved_token_id)

    assert getattr(login_error, "code", None) == "login_required"
    assert failed.state == "failed"
    assert failed.phase == "verify_account"
    assert failed.error_code == "login_required"
    assert failed.resolved_token_id is None
    assert resumed.state == "running"
    assert resumed.phase == "awaiting_login"
    assert resumed.error_code is None
    assert resumed.browser_pid == 5432
    assert resumed.browser_start_ticks == 888
    assert original_profile_inode == profile_inode_after_resume
    assert stale_artifacts_after_resume == (False, False, False)
    assert completed.state == "completed"
    assert (destination / "operator-progress").read_text(encoding="utf-8") == "preserve"
    assert len(processes.launches) == 2
    first_profile_argument = next(
        argument
        for argument in processes.launches[0][0]
        if argument.startswith("--user-data-dir=")
    )
    second_profile_argument = next(
        argument
        for argument in processes.launches[1][0]
        if argument.startswith("--user-data-dir=")
    )
    assert second_profile_argument == first_profile_argument
    assert processes.stops == []
    assert processes.pidfd_opens == []
    assert 4321 in processes.processes
    assert manager.inspect_calls == 2


def test_failed_login_resume_fails_closed_when_onboarding_profile_lease_is_busy(
    tmp_path,
):
    from src.services.keepalive.profile import acquire_profile_path_lease

    async def run():
        service, _db, manager, processes = build_service(
            tmp_path,
            cookie_reader=lambda **_kwargs: [],
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(Exception):
            await service.finalize(running.job_id)

        with acquire_profile_path_lease(
            tmp_path / "profiles",
            temp_profile,
            f"onboarding-{running.job_id}",
        ):
            with pytest.raises(Exception) as lease_error:
                await service.start_job(running.job_id)
        preserved = await service.get(running.job_id)
        return lease_error.value, preserved, manager, processes, temp_profile

    lease_error, preserved, manager, processes, temp_profile = asyncio.run(run())
    assert getattr(lease_error, "code", None) == "process_ownership_mismatch"
    assert preserved.state == "failed"
    assert preserved.phase == "verify_account"
    assert preserved.error_code == "login_required"
    assert (temp_profile / "operator-progress").read_text(encoding="utf-8") == "preserve"
    assert len(processes.launches) == 1
    assert processes.stops == []
    assert manager.inspect_calls == 0


def test_failed_login_resume_launch_failure_preserves_existing_profile(tmp_path):
    async def run():
        service, _db, manager, processes = build_service(
            tmp_path,
            cookie_reader=lambda **_kwargs: [],
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")
        original_inode = temp_profile.stat().st_ino
        close_browser(processes, running)
        with pytest.raises(Exception):
            await service.finalize(running.job_id)

        def fail_launch(*_args, **_kwargs):
            raise OSError("simulated Chrome launch failure")

        service.process_launcher = fail_launch
        with pytest.raises(Exception) as launch_error:
            await service.start_job(running.job_id)
        failed = await service.get(running.job_id)
        return launch_error.value, failed, manager, processes, temp_profile, original_inode

    launch_error, failed, manager, processes, temp_profile, original_inode = asyncio.run(
        run()
    )
    assert getattr(launch_error, "code", None) == "process_launch_failed"
    assert failed.state == "failed"
    assert failed.phase == "browser_start"
    assert temp_profile.stat().st_ino == original_inode
    assert (temp_profile / "operator-progress").read_text(encoding="utf-8") == "preserve"
    assert len(processes.launches) == 1
    assert processes.stops == []
    assert manager.inspect_calls == 0


def test_failed_login_resume_is_blocked_by_another_failed_job(tmp_path):
    async def run():
        service, db, manager, processes = build_service(
            tmp_path,
            cookie_reader=lambda **_kwargs: [],
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(Exception):
            await service.finalize(running.job_id)
        blocker_id = await db.create_onboarding_job(OnboardingJob())
        await db.update_onboarding_job_state(
            blocker_id,
            "failed",
            phase="stop_browser",
            error_code="process_ownership_mismatch",
            error_message="safe failure",
        )

        with pytest.raises(Exception) as blocked_error:
            await service.start_job(running.job_id)
        preserved = await service.get(running.job_id)
        return blocked_error.value, preserved, manager, processes, temp_profile

    blocked_error, preserved, manager, processes, temp_profile = asyncio.run(run())
    assert getattr(blocked_error, "code", None) == "active_job_exists"
    assert preserved.state == "failed"
    assert preserved.phase == "verify_account"
    assert preserved.error_code == "login_required"
    assert (temp_profile / "operator-progress").read_text(encoding="utf-8") == "preserve"
    assert len(processes.launches) == 1
    assert processes.stops == []
    assert manager.inspect_calls == 0


@pytest.mark.parametrize(
    ("owner_state", "expected_code"),
    [
        ("busy", "process_ownership_mismatch"),
        ("unsafe", "process_identity_unavailable"),
    ],
)
def test_failed_login_resume_rejects_live_or_uncertain_profile_ownership(
    tmp_path,
    owner_state,
    expected_code,
):
    async def run():
        service, _db, manager, processes = build_service(
            tmp_path,
            cookie_reader=lambda **_kwargs: [],
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(Exception):
            await service.finalize(running.job_id)

        if owner_state == "busy":
            processes.processes[running.browser_pid] = {
                "ticks": running.browser_start_ticks,
                "cmdline": (
                    "/configured/chrome",
                    f"--user-data-dir={temp_profile}",
                ),
            }
            (temp_profile / "SingletonLock").symlink_to(
                f"{socket.gethostname()}-{running.browser_pid}"
            )
        else:
            (temp_profile / "SingletonLock").symlink_to(
                f"foreign-host-{running.browser_pid}"
            )

        with pytest.raises(Exception) as ownership_error:
            await service.start_job(running.job_id)
        preserved = await service.get(running.job_id)
        return ownership_error.value, preserved, manager, processes, temp_profile

    ownership_error, preserved, manager, processes, temp_profile = asyncio.run(run())
    assert getattr(ownership_error, "code", None) == expected_code
    assert preserved.state == "failed"
    assert preserved.phase == "verify_account"
    assert preserved.error_code == "login_required"
    assert (temp_profile / "operator-progress").read_text(encoding="utf-8") == "preserve"
    assert len(processes.launches) == 1
    assert processes.stops == []
    assert processes.pidfd_opens == []
    assert manager.inspect_calls == 0


@pytest.mark.parametrize(
    "failed_phase",
    ["migrate_profile", "account_commit"],
)
def test_start_never_relaunches_failed_migration_or_account_commit_job(
    tmp_path,
    failed_phase,
):
    async def run():
        service, db, manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")
        close_browser(processes, running)
        await db.update_onboarding_job_state(
            running.job_id,
            "failed",
            phase=failed_phase,
            error_code="finalize_failed",
            error_message="safe failure",
        )

        with pytest.raises(Exception) as invalid_error:
            await service.start_job(running.job_id)
        preserved = await service.get(running.job_id)
        return invalid_error.value, preserved, manager, processes, temp_profile

    invalid_error, preserved, manager, processes, temp_profile = asyncio.run(run())
    assert getattr(invalid_error, "code", None) == "invalid_job_state"
    assert preserved.state == "failed"
    assert preserved.phase == failed_phase
    assert (temp_profile / "operator-progress").read_text(encoding="utf-8") == "preserve"
    assert len(processes.launches) == 1
    assert processes.stops == []
    assert manager.inspect_calls == 0


@pytest.mark.parametrize(
    ("lock_target", "expected_code"),
    [
        (None, "process_ownership_mismatch"),
        ("foreign-host", "process_identity_unavailable"),
    ],
    ids=["lock-absent", "lock-unsafe"],
)
def test_finalize_does_not_bypass_unproven_or_unsafe_lock_state(
    tmp_path,
    lock_target,
    expected_code,
):
    existing = Token(id=36, st=LONG_ST, email="owner@example.com")

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
        )
        running = await create_and_start(service, target_token_id=existing.id)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "Preferences").write_text("preserve", encoding="utf-8")
        (temp_profile / "SingletonCookie").symlink_to("do-not-remove")
        processes.processes[running.browser_pid] = {
            "ticks": running.browser_start_ticks + 1,
            "cmdline": ("/usr/bin/unrelated-reused-process",),
        }
        if lock_target is not None:
            (temp_profile / "SingletonLock").symlink_to(
                f"{lock_target}-{running.browser_pid}"
            )

        with pytest.raises(Exception) as finalize_error:
            await service.finalize(running.job_id)
        failed = await service.get(running.job_id)
        return finalize_error.value, failed, manager, processes, temp_profile

    finalize_error, failed, manager, processes, temp_profile = asyncio.run(run())

    assert getattr(finalize_error, "code", None) == expected_code
    assert failed.state == "failed"
    assert failed.phase == "stop_browser"
    assert failed.error_code == expected_code
    assert (temp_profile / "Preferences").read_text(encoding="utf-8") == "preserve"
    assert os.path.lexists(temp_profile / "SingletonCookie")
    if lock_target is not None:
        assert os.path.lexists(temp_profile / "SingletonLock")
    assert manager.inspect_calls == 0
    assert manager.update_calls == []
    assert processes.stops == []
    assert processes.pidfd_opens == []


def test_finalize_holds_token_profile_lease_through_account_publication(tmp_path):
    existing = Token(id=30, st=LONG_ST, email="owner@example.com")

    class TrackingLease:
        def __init__(self, profile_path):
            self.profile_path = profile_path
            self.active = True

        def release(self):
            self.active = False

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        lease = TrackingLease(tmp_path / "profiles" / str(existing.id))
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            profile_lease_acquirer=lambda _base, _token_id: lease,
        )
        original_update = manager.update_token

        async def update_while_leased(token_id, **kwargs):
            assert lease.active is True
            await original_update(token_id, **kwargs)

        manager.update_token = update_while_leased
        running = await create_and_start(service, target_token_id=existing.id)
        close_browser(processes, running)
        completed = await service.finalize(running.job_id)
        return completed, lease

    completed, lease = asyncio.run(run())
    assert completed.state == "completed"
    assert lease.active is False


def test_finalize_respects_shared_token_profile_lease_before_migration(tmp_path):
    from src.services.keepalive.profile import acquire_profile_lease

    existing = Token(id=28, st=LONG_ST, email="owner@example.com")

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
        )
        running = await create_and_start(service, target_token_id=existing.id)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "browser-data").write_text("preserve", encoding="utf-8")
        close_browser(processes, running)
        profile_base = tmp_path / "profiles"
        with acquire_profile_lease(profile_base, existing.id):
            with pytest.raises(Exception) as lease_conflict:
                await service.finalize(running.job_id)
        failed = await service.get(running.job_id)
        return lease_conflict.value, failed, manager, temp_profile

    lease_conflict, failed, manager, temp_profile = asyncio.run(run())
    assert getattr(lease_conflict, "code", None) == "process_ownership_mismatch"
    assert failed.state == "failed"
    assert manager.update_calls == []
    assert (temp_profile / "browser-data").read_text(encoding="utf-8") == "preserve"
    assert not (tmp_path / "profiles" / str(existing.id)).exists()


def test_pid_only_recovery_uses_full_tuple_cas_and_preserves_successor(tmp_path):
    async def run():
        db = RacingIdentityReplaceDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        await db.update_onboarding_job(
            running.job_id,
            browser_pid=running.browser_pid,
            browser_start_ticks=None,
        )
        await db.update_onboarding_job_state(
            running.job_id,
            "failed",
            phase="browser_start",
            error_code="process_identity_unavailable",
            error_message="safe failure",
        )

        def publish_successor_before_backfill():
            current = db.jobs[running.job_id]
            db.jobs[running.job_id] = current.model_copy(
                update={"browser_pid": 5432, "browser_start_ticks": 888}
            )
            processes.processes.pop(running.browser_pid, None)
            processes.processes[5432] = {
                "ticks": 888,
                "cmdline": (
                    "/configured/chrome",
                    f"--user-data-dir={temp_profile}",
                    "https://labs.google/fx/tools/flow",
                ),
            }
            (temp_profile / "SingletonLock").symlink_to(
                f"{socket.gethostname()}-5432"
            )

        db.before_identity_replace = publish_successor_before_backfill
        recovered = await service.recover_incomplete()
        current = await service.get(running.job_id)
        return recovered, current, processes

    recovered, current, processes = asyncio.run(run())
    assert current.browser_pid == 5432
    assert current.browser_start_ticks == 888
    assert any(job.job_id == current.job_id for job in recovered)
    assert 4321 not in processes.processes
    assert processes.processes[5432]["ticks"] == 888


def test_failed_job_with_verified_live_browser_blocks_new_start_until_reconciled(tmp_path):
    def refuse_stop(_pid, _stop_signal):
        raise OSError("simulated stop refusal")

    async def run():
        service, db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        processes.signal_hook = refuse_stop
        with pytest.raises(Exception) as stop_failure:
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        recovered_live = await service.recover_incomplete()

        next_job = await service.create_job()
        with pytest.raises(Exception) as active_conflict:
            await service.start_job(next_job.job_id)
        launches_while_live = len(processes.launches)

        processes.processes.pop(running.browser_pid, None)
        processes.signal_hook = processes.stop
        recovered_stale = await service.recover_incomplete()
        started = await service.start_job(next_job.job_id)
        reconciled = await service.get(running.job_id)
        return (
            stop_failure.value,
            failed,
            recovered_live,
            active_conflict.value,
            launches_while_live,
            recovered_stale,
            started,
            reconciled,
            db,
        )

    (
        stop_failure,
        failed,
        recovered_live,
        active_conflict,
        launches_while_live,
        recovered_stale,
        started,
        reconciled,
        db,
    ) = asyncio.run(run())
    assert getattr(stop_failure, "code", None) == "process_stop_failed"
    assert failed.state == "failed"
    assert failed.browser_pid is not None
    assert failed.browser_start_ticks is not None
    live_record = next(job for job in recovered_live if job.job_id == failed.job_id)
    assert live_record.browser_pid == failed.browser_pid
    assert live_record.browser_start_ticks == failed.browser_start_ticks
    stale_record = next(job for job in recovered_stale if job.job_id == failed.job_id)
    assert stale_record.browser_pid is None
    assert stale_record.browser_start_ticks is None
    assert getattr(active_conflict, "code", None) == "active_job_exists"
    assert launches_while_live == 1
    assert started.state == "running"
    assert reconciled.browser_pid is None
    assert reconciled.browser_start_ticks is None
    assert any(
        update == {
            "browser_pid": None,
            "browser_start_ticks": None,
        }
        for job_id, update in db.job_updates
        if job_id == failed.job_id
    )


def test_failed_job_adopts_verified_successor_before_clearing_reused_pid(tmp_path):
    def refuse_stop(_pid, _stop_signal):
        raise OSError("simulated stop refusal")

    async def run():
        service, db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        processes.signal_hook = refuse_stop
        with pytest.raises(Exception):
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        temp_profile = tmp_path / "profiles" / ".onboarding" / failed.job_id

        replace_browser_owner(
            processes,
            failed,
            temp_profile,
            pid=5432,
            ticks=888,
        )
        processes.processes[failed.browser_pid] = {
            "ticks": failed.browser_start_ticks + 1,
            "cmdline": ("/usr/bin/unrelated-process",),
        }

        next_job = await service.create_job()
        with pytest.raises(Exception) as active_conflict:
            await service.start_job(next_job.job_id)
        reconciled = await service.get(failed.job_id)
        blocked = await service.get(next_job.job_id)
        return active_conflict.value, reconciled, blocked, processes, db

    active_conflict, reconciled, blocked, processes, db = asyncio.run(run())
    assert getattr(active_conflict, "code", None) == "active_job_exists"
    assert blocked.state == "pending"
    assert reconciled.state == "failed"
    assert reconciled.browser_pid == 5432
    assert reconciled.browser_start_ticks == 888
    assert len(processes.launches) == 1
    assert not any(
        update == {
            "browser_pid": None,
            "browser_start_ticks": None,
        }
        for job_id, update in db.job_updates
        if job_id == reconciled.job_id
    )


def test_failed_identity_clear_reinspects_successor_before_new_claim(tmp_path):
    def refuse_stop(_pid, _stop_signal):
        raise OSError("simulated stop refusal")

    async def run():
        db = RacingIdentityClearDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        running = await create_and_start(service)
        processes.signal_hook = refuse_stop
        with pytest.raises(Exception):
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        temp_profile = tmp_path / "profiles" / ".onboarding" / failed.job_id
        processes.processes.pop(failed.browser_pid, None)

        def start_successor_after_clear():
            processes.processes[5432] = {
                "ticks": 888,
                "cmdline": (
                    "/configured/chrome",
                    f"--user-data-dir={temp_profile}",
                    "https://labs.google/fx/tools/flow",
                ),
            }
            (temp_profile / "SingletonLock").symlink_to(
                f"{socket.gethostname()}-5432"
            )

        db.on_identity_cleared = start_successor_after_clear
        next_job = await service.create_job()
        with pytest.raises(Exception) as active_conflict:
            await service.start_job(next_job.job_id)
        reconciled = await service.get(failed.job_id)
        blocked = await service.get(next_job.job_id)
        return active_conflict.value, reconciled, blocked, processes

    active_conflict, reconciled, blocked, processes = asyncio.run(run())
    assert getattr(active_conflict, "code", None) == "active_job_exists"
    assert reconciled.browser_pid == 5432
    assert reconciled.browser_start_ticks == 888
    assert blocked.state == "pending"
    assert len(processes.launches) == 1


def test_singleton_appearing_at_claim_entry_cannot_claim_or_launch(tmp_path):
    def refuse_stop(_pid, _stop_signal):
        raise OSError("simulated stop refusal")

    async def run():
        db = ClaimEntryRaceDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        running = await create_and_start(service)
        processes.signal_hook = refuse_stop
        with pytest.raises(Exception):
            await service.cancel(running.job_id)
        failed = await service.get(running.job_id)
        temp_profile = tmp_path / "profiles" / ".onboarding" / failed.job_id
        processes.processes.pop(failed.browser_pid, None)
        assert await db.clear_onboarding_browser_identity(
            failed.job_id,
            expected_pid=failed.browser_pid,
            expected_start_ticks=failed.browser_start_ticks,
        )

        def start_successor_at_claim_entry():
            processes.processes[6543] = {
                "ticks": 999,
                "cmdline": (
                    "/configured/chrome",
                    f"--user-data-dir={temp_profile}",
                    "https://labs.google/fx/tools/flow",
                ),
            }
            (temp_profile / "SingletonLock").symlink_to(
                f"{socket.gethostname()}-6543"
            )

        db.on_claim_entry = start_successor_at_claim_entry
        next_job = await service.create_job()
        with pytest.raises(Exception) as blocked_error:
            await service.start_job(next_job.job_id)
        blocked = await service.get(next_job.job_id)
        return blocked_error.value, blocked, processes

    blocked_error, blocked, processes = asyncio.run(run())
    assert getattr(blocked_error, "code", None) == "active_job_exists"
    assert blocked.state == "pending"
    assert len(processes.launches) == 1


def test_existing_profile_validation_reads_retained_cookie_and_never_mutates_state(tmp_path):
    existing = Token(
        id=23,
        st=LONG_ST,
        at="stored-access-token",
        email="Owner@Example.com",
        credits=120,
        user_paygate_tier="PAYGATE_TIER_ONE",
        is_active=False,
        ban_reason=TOKEN_REASON_MANUAL_DISABLED,
    )
    observed_cookie_paths = []

    def cookie_reader(**kwargs):
        observed_cookie_paths.append(kwargs["cookie_file"])
        return cookie_reader_for(LONG_ST)(**kwargs)

    async def run():
        db = FakeDB([existing])
        db.lifecycles[23] = TokenLifecycle(
            token_id=23,
            profile_state="ready",
            verified_email=" owner@example.COM ",
        )
        db.projects[23] = [
            Project(id=1, project_id="project-1", token_id=23, project_name="Pool P1"),
            Project(id=2, project_id="project-2", token_id=23, project_name="Pool P2"),
            Project(
                id=3,
                project_id="inactive-project",
                token_id=23,
                project_name="Old P3",
                is_active=False,
            ),
        ]
        manager = FakeTokenManager(
            db,
            [snapshot(email="owner@example.com", tier="PAYGATE_TIER_TWO", st=LONG_ST)],
        )
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            cookie_reader=cookie_reader,
        )
        profile = tmp_path / "profiles" / "23" / "Default"
        profile.mkdir(parents=True)
        token_before = db.tokens[23].model_copy(deep=True)
        lifecycle_before = db.lifecycles[23].model_copy(deep=True)
        result = await service.validate_profile(23)
        return result, db, manager, token_before, lifecycle_before

    result, db, manager, token_before, lifecycle_before = asyncio.run(run())
    assert result.email == "owner@example.com"
    assert result.tier == "PAYGATE_TIER_TWO"
    assert result.credits == 900
    assert result.expiry == NOW + timedelta(hours=1)
    assert result.project_count == 2
    assert result.profile_ready is True
    assert observed_cookie_paths == [
        str(tmp_path / "profiles" / "23" / "Default" / "Cookies")
    ]
    assert manager.inspected_session_tokens == [LONG_ST]
    assert manager.add_calls == []
    assert manager.update_calls == []
    assert manager.ensure_calls == []
    assert db.desired_updates == []
    assert db.tokens[23] == token_before
    assert db.lifecycles[23] == lifecycle_before


def test_existing_profile_validation_requires_token_and_lifecycle_identity_match(tmp_path):
    existing = Token(id=24, st=LONG_ST, email="owner@example.com")

    async def run():
        db = FakeDB([existing])
        db.lifecycles[24] = TokenLifecycle(
            token_id=24,
            profile_state="ready",
            verified_email="different@example.com",
        )
        manager = FakeTokenManager(
            db,
            [snapshot(email="owner@example.com", st=LONG_ST)],
        )
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
        )
        (tmp_path / "profiles" / "24").mkdir(parents=True)
        token_before = db.tokens[24].model_copy(deep=True)
        lifecycle_before = db.lifecycles[24].model_copy(deep=True)
        with pytest.raises(Exception) as mismatch:
            await service.validate_profile(24)
        return mismatch.value, db, manager, token_before, lifecycle_before

    mismatch, db, manager, token_before, lifecycle_before = asyncio.run(run())
    assert getattr(mismatch, "code", None) == "profile_identity_mismatch"
    assert manager.add_calls == []
    assert manager.update_calls == []
    assert manager.ensure_calls == []
    assert db.tokens[24] == token_before
    assert db.lifecycles[24] == lifecycle_before


@pytest.mark.parametrize(
    ("tier", "expected_enabled"),
    [("PAYGATE_TIER_ONE", True), ("PAYGATE_TIER_NOT_PAID", False), (None, False)],
)
def test_new_account_creation_enables_only_exact_paid_tier(tmp_path, tier, expected_enabled):
    async def run():
        db = FakeDB()
        manager = FakeTokenManager(db, [snapshot(tier=tier), snapshot(tier=tier)])
        service, _db, _manager, processes = build_service(
            tmp_path, db=db, token_manager=manager
        )
        running = await create_and_start(
            service,
            requested_business_enabled=True,
            requested_keepalive_enabled=True,
            requested_runtime_mode="persistent",
        )
        close_browser(processes, running)
        completed = await service.finalize(running.job_id)
        return completed, db, manager

    completed, db, manager = asyncio.run(run())
    token = db.tokens[completed.resolved_token_id]
    lifecycle = db.lifecycles[token.id]
    assert manager.add_calls[0]["is_active"] is False
    assert manager.add_calls[0]["ban_reason"] == TOKEN_REASON_ONBOARDING_PENDING
    assert token.is_active is expected_enabled
    assert token.ban_reason != TOKEN_REASON_ONBOARDING_PENDING
    assert (token.id in manager.enable_calls) is expected_enabled
    assert manager.ensure_calls == [token.id]
    assert lifecycle.profile_state == "ready"
    assert lifecycle.keepalive_enabled is True
    assert lifecycle.runtime_mode == "persistent"
    assert (tmp_path / "profiles" / str(token.id)).exists()
    assert completed.state == "completed"
    assert completed.project_count == 0
    assert completed.profile_ready is True
    assert completed.conflict_status == "no_conflict"


@pytest.mark.parametrize(
    "ban_reason", [TOKEN_REASON_MANUAL_DISABLED, TOKEN_REASON_429_RATE_LIMIT]
)
def test_existing_match_preserves_ban_when_enable_not_requested(tmp_path, ban_reason):
    existing = Token(
        id=9,
        st=LONG_ST,
        email="Owner@Example.com",
        is_active=False,
        ban_reason=ban_reason,
    )

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path, db=db, token_manager=manager
        )
        running = await create_and_start(
            service, target_token_id=9, requested_business_enabled=False
        )
        close_browser(processes, running)
        completed = await service.finalize(running.job_id)
        return completed, db, manager

    completed, db, manager = asyncio.run(run())
    token = db.tokens[9]
    assert completed.resolved_token_id == 9
    assert token.is_active is False
    assert token.ban_reason == ban_reason
    assert manager.enable_calls == []
    assert manager.update_calls[0][1]["allow_auth_reactivate"] is False


@pytest.mark.parametrize(
    "start_kwargs",
    [{}, {"requested_business_enabled": True}],
    ids=["default-enabled", "explicit-enabled"],
)
@pytest.mark.parametrize(
    "ban_reason", [TOKEN_REASON_MANUAL_DISABLED, TOKEN_REASON_429_RATE_LIMIT]
)
def test_existing_ban_is_preserved_when_business_enable_is_requested(
    tmp_path, ban_reason, start_kwargs
):
    existing = Token(
        id=11,
        st=LONG_ST,
        email="owner@example.com",
        is_active=False,
        ban_reason=ban_reason,
        banned_at=NOW - timedelta(days=1),
    )

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path, db=db, token_manager=manager
        )
        running = await create_and_start(
            service,
            target_token_id=existing.id,
            **start_kwargs,
        )
        close_browser(processes, running)
        completed = await service.finalize(running.job_id)
        return completed, db.tokens[existing.id], manager

    completed, token, manager = asyncio.run(run())
    assert completed.state == "completed"
    assert token.is_active is False
    assert token.ban_reason == ban_reason
    assert token.banned_at == existing.banned_at
    assert manager.enable_calls == []


def test_existing_active_account_is_not_disabled_when_enable_flag_is_false(tmp_path):
    async def run():
        existing = Token(id=10, st=LONG_ST, email="owner@example.com", is_active=True)
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path, db=db, token_manager=manager
        )
        running = await create_and_start(
            service, target_token_id=10, requested_business_enabled=False
        )
        close_browser(processes, running)
        await service.finalize(running.job_id)
        return db.tokens[10], manager

    token, manager = asyncio.run(run())
    assert token.is_active is True
    assert token.ban_reason is None
    assert manager.enable_calls == []


def test_target_mismatch_has_zero_token_and_profile_writes(tmp_path):
    target = Token(id=4, st=LONG_ST, email="target@example.com")

    async def run():
        db = FakeDB([target])
        manager = FakeTokenManager(
            db,
            [snapshot(email="other@example.com"), snapshot(email="other@example.com")],
        )
        service, _db, _manager, processes = build_service(
            tmp_path, db=db, token_manager=manager
        )
        running = await create_and_start(service, target_token_id=4)
        close_browser(processes, running)
        with pytest.raises(Exception) as mismatch:
            await service.finalize(running.job_id)
        return mismatch.value, db, manager, running

    mismatch, db, manager, running = asyncio.run(run())
    assert getattr(mismatch, "code", None) == "target_identity_mismatch"
    assert manager.add_calls == []
    assert manager.update_calls == []
    assert manager.ensure_calls == []
    assert db.tokens[4] == target
    assert (tmp_path / "profiles" / ".onboarding" / running.job_id).exists()
    assert not (tmp_path / "profiles" / "4").exists()


def test_duplicate_normalized_email_ambiguity_fails_without_writes(tmp_path):
    async def run():
        db = FakeDB()
        manager = FakeTokenManager(db)
        manager.duplicate_email = "owner@example.com"
        service, _db, _manager, processes = build_service(
            tmp_path, db=db, token_manager=manager
        )
        running = await create_and_start(service)
        close_browser(processes, running)
        with pytest.raises(Exception) as duplicate:
            await service.finalize(running.job_id)
        return duplicate.value, manager

    duplicate, manager = asyncio.run(run())
    assert getattr(duplicate, "code", None) == "duplicate_email"
    assert manager.add_calls == []
    assert manager.update_calls == []


def test_existing_destination_conflicts_without_overwrite(tmp_path):
    existing = Token(id=7, st=LONG_ST, email="owner@example.com")
    destination = tmp_path / "profiles" / "7"
    destination.mkdir(parents=True)
    original = destination / "keep.txt"
    original.write_text("preserve", encoding="utf-8")

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path, db=db, token_manager=manager
        )
        running = await create_and_start(service, target_token_id=7)
        close_browser(processes, running)
        with pytest.raises(Exception) as conflict:
            await service.finalize(running.job_id)
        return conflict.value, manager, running

    conflict, manager, running = asyncio.run(run())
    assert getattr(conflict, "code", None) == "destination_conflict"
    assert original.read_text(encoding="utf-8") == "preserve"
    assert manager.update_calls == []
    assert (tmp_path / "profiles" / ".onboarding" / running.job_id).exists()


def test_archive_and_replace_retains_archive_and_moves_profile(tmp_path):
    existing = Token(id=8, st=LONG_ST, email="owner@example.com")
    destination = tmp_path / "profiles" / "8"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("old", encoding="utf-8")

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        manager.ensure_projects = [
            Project(id=1, project_id="project-1", token_id=8, project_name="Pool P1"),
            Project(id=2, project_id="project-2", token_id=8, project_name="Pool P2"),
        ]
        service, _db, _manager, processes = build_service(
            tmp_path, db=db, token_manager=manager
        )
        running = await create_and_start(
            service, target_token_id=8, conflict_policy="archive_and_replace"
        )
        temp = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp / "new.txt").write_text("new", encoding="utf-8")
        close_browser(processes, running)
        completed = await service.finalize(running.job_id)
        return completed

    completed = asyncio.run(run())
    archive = tmp_path / "profiles" / ".archive" / "8" / completed.job_id
    assert (tmp_path / "profiles" / "8" / "new.txt").read_text(encoding="utf-8") == "new"
    assert (archive / "old.txt").read_text(encoding="utf-8") == "old"
    assert completed.project_count == 2
    assert completed.profile_ready is True
    assert completed.conflict_status == "archived_and_replaced"


@pytest.mark.parametrize(
    "conflict_policy", ["reject", "archive_and_replace"]
)
def test_restart_resumes_after_temp_profile_was_renamed_to_destination(
    tmp_path, conflict_policy
):
    existing = Token(id=12, st=LONG_ST, email="owner@example.com")
    destination = tmp_path / "profiles" / str(existing.id)
    if conflict_policy == "archive_and_replace":
        destination.mkdir(parents=True)
        (destination / "old.txt").write_text("old", encoding="utf-8")

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path, db=db, token_manager=manager, processes=processes
        )
        running = await create_and_start(
            first_service,
            target_token_id=existing.id,
            conflict_policy=conflict_policy,
        )
        temp = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp / "new.txt").write_text("new", encoding="utf-8")
        close_browser(processes, running)
        await db.update_onboarding_job(
            running.job_id,
            resolved_token_id=existing.id,
            discovered_email="owner@example.com",
            discovered_tier="PAYGATE_TIER_ONE",
            discovered_credits=900,
            discovered_at_expires=NOW + timedelta(hours=1),
        )

        archive = None
        if conflict_policy == "archive_and_replace":
            archive = (
                tmp_path
                / "profiles"
                / ".archive"
                / str(existing.id)
                / running.job_id
            )
            archive.parent.mkdir(parents=True)
            os.rename(destination, archive)
        os.rename(temp, destination)

        restarted_service, _db, _manager, _processes = build_service(
            tmp_path, db=db, token_manager=manager, processes=processes
        )
        completed = await restarted_service.finalize(running.job_id)
        return completed, archive

    completed, archive = asyncio.run(run())
    assert completed.state == "completed"
    assert completed.resolved_token_id == existing.id
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (destination / ".flow2api-onboarding").exists()
    if archive is not None:
        assert (archive / "old.txt").read_text(encoding="utf-8") == "old"


def test_archive_replace_resets_conflict_status_after_final_validation_rollback(tmp_path):
    existing = Token(id=18, st=LONG_ST, email="owner@example.com")
    destination = tmp_path / "profiles" / "18"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("old", encoding="utf-8")

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(
            db,
            [
                snapshot(email="owner@example.com"),
                snapshot(email="other@example.com"),
            ],
        )
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
        )
        running = await create_and_start(
            service,
            target_token_id=18,
            conflict_policy="archive_and_replace",
        )
        temp = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp / "new.txt").write_text("new", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(Exception) as validation_failure:
            await service.finalize(running.job_id)
        failed = await service.get(running.job_id)
        return validation_failure.value, failed, temp

    validation_failure, failed, temp = asyncio.run(run())
    assert getattr(validation_failure, "code", None) == "final_validation_failed"
    assert failed.state == "failed"
    assert failed.conflict_status is None
    assert failed.profile_ready is False
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (temp / "new.txt").read_text(encoding="utf-8") == "new"


def test_migration_rechecks_temp_liveness_after_archive_exchange(tmp_path):
    import src.services.onboarding as onboarding_module

    existing = Token(id=29, st=LONG_ST, email="owner@example.com")
    destination = tmp_path / "profiles" / "29"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("old", encoding="utf-8")
    calls = 0
    temp_profile = None
    processes = FakeProcessTable()

    def racing_exchange(left, right):
        nonlocal calls
        calls += 1
        result = onboarding_module._linux_rename_exchange(left, right)
        if calls == 1:
            processes.processes[7654] = {
                "ticks": 999,
                "cmdline": (
                    "/configured/chrome",
                    f"--user-data-dir={temp_profile}",
                    "https://labs.google/fx/tools/flow",
                ),
            }
            (temp_profile / "SingletonLock").symlink_to(
                f"{socket.gethostname()}-7654"
            )
        return result

    async def run():
        nonlocal temp_profile
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
            path_exchanger=racing_exchange,
        )
        running = await create_and_start(
            service,
            target_token_id=existing.id,
            conflict_policy="archive_and_replace",
        )
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "new.txt").write_text("new", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(Exception) as live_profile:
            await service.finalize(running.job_id)
        failed = await service.get(running.job_id)
        return live_profile.value, failed

    live_profile, failed = asyncio.run(run())
    assert getattr(live_profile, "code", None) == "process_ownership_mismatch"
    assert failed.state == "failed"
    assert failed.phase == "migrate_profile"
    assert calls == 2
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (temp_profile / "new.txt").read_text(encoding="utf-8") == "new"


def test_path_fence_detects_successor_winning_postcheck_prerename_race(tmp_path):
    import src.services.onboarding as onboarding_module

    existing = Token(id=31, st=LONG_ST, email="owner@example.com")

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        temp_profile = None
        injected = False

        def racing_exchange(left, right):
            nonlocal injected
            if not injected and temp_profile is not None and Path(left) == temp_profile:
                injected = True
                processes.processes[8765] = {
                    "ticks": 1234,
                    "cmdline": (
                        "/configured/chrome",
                        f"--user-data-dir={temp_profile}",
                        "https://labs.google/fx/tools/flow",
                    ),
                }
                (temp_profile / "SingletonLock").symlink_to(
                    f"{socket.gethostname()}-8765"
                )
            return onboarding_module._linux_rename_exchange(left, right)

        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
            path_exchanger=racing_exchange,
        )
        running = await create_and_start(service, target_token_id=existing.id)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "new.txt").write_text("new", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(Exception) as race_error:
            await service.finalize(running.job_id)
        failed = await service.get(running.job_id)
        return race_error.value, failed, manager, temp_profile, injected

    race_error, failed, manager, temp_profile, injected = asyncio.run(run())
    assert injected is True
    assert getattr(race_error, "code", None) == "process_ownership_mismatch"
    assert failed.state == "failed"
    assert failed.phase == "migrate_profile"
    assert manager.update_calls == []
    assert (temp_profile / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "profiles" / str(existing.id)).exists()


def test_restart_uses_destination_after_crash_post_profile_exchange(tmp_path):
    import src.services.onboarding as onboarding_module

    class SimulatedCrash(BaseException):
        pass

    existing = Token(id=33, st=LONG_ST, email="owner@example.com")
    calls = 0

    def crashing_exchange(left, right):
        nonlocal calls
        calls += 1
        onboarding_module._linux_rename_exchange(left, right)
        if calls == 1:
            raise SimulatedCrash()

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(
            db,
            [snapshot(), snapshot(), snapshot()],
        )
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
            cookie_reader=directory_cookie_reader_for(),
            path_exchanger=crashing_exchange,
        )
        running = await create_and_start(
            first_service,
            target_token_id=existing.id,
        )
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "new.txt").write_text("new", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(SimulatedCrash):
            await first_service.finalize(running.job_id)
        crashed = await first_service.get(running.job_id)
        blocker_after_crash = temp_profile.is_file()

        restarted_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
            cookie_reader=directory_cookie_reader_for(),
        )
        completed = await restarted_service.finalize(running.job_id)
        return crashed, completed, temp_profile, blocker_after_crash

    crashed, completed, temp_profile, blocker_after_crash = asyncio.run(run())
    destination = tmp_path / "profiles" / str(existing.id)
    assert crashed.resolved_token_id == existing.id
    assert crashed.discovered_email == "owner@example.com"
    assert blocker_after_crash is True
    assert completed.state == "completed"
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert not temp_profile.exists()


def test_cancel_rolls_back_and_removes_post_exchange_migration_blocker(tmp_path):
    import src.services.onboarding as onboarding_module

    class SimulatedCrash(BaseException):
        pass

    existing = Token(id=34, st=LONG_ST, email="owner@example.com")

    def crashing_exchange(left, right):
        onboarding_module._linux_rename_exchange(left, right)
        raise SimulatedCrash()

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db, [snapshot()])
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
            path_exchanger=crashing_exchange,
        )
        running = await create_and_start(service, target_token_id=existing.id)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "new.txt").write_text("new", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(SimulatedCrash):
            await service.finalize(running.job_id)
        service.path_exchanger = onboarding_module._linux_rename_exchange
        cancelled = await service.cancel(running.job_id)
        return cancelled, temp_profile

    cancelled, temp_profile = asyncio.run(run())
    assert cancelled.state == "cancelled"
    assert not temp_profile.exists()
    assert not (tmp_path / "profiles" / str(existing.id)).exists()


def test_restart_resumes_after_crash_between_archive_and_profile_exchange(tmp_path):
    import src.services.onboarding as onboarding_module

    class SimulatedCrash(BaseException):
        pass

    existing = Token(id=32, st=LONG_ST, email="owner@example.com")
    destination = tmp_path / "profiles" / "32"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("old", encoding="utf-8")
    calls = 0

    def crashing_exchange(left, right):
        nonlocal calls
        calls += 1
        onboarding_module._linux_rename_exchange(left, right)
        if calls == 1:
            raise SimulatedCrash()

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(
            db,
            [snapshot(), snapshot(), snapshot()],
        )
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
            path_exchanger=crashing_exchange,
        )
        running = await create_and_start(
            first_service,
            target_token_id=existing.id,
            conflict_policy="archive_and_replace",
        )
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "new.txt").write_text("new", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(SimulatedCrash):
            await first_service.finalize(running.job_id)

        archive = (
            tmp_path
            / "profiles"
            / ".archive"
            / str(existing.id)
            / running.job_id
        )
        crash_state = {
            "destination_blocker": destination.is_file(),
            "archive_data": (archive / "old.txt").read_text(encoding="utf-8"),
            "temp_data": (temp_profile / "new.txt").read_text(encoding="utf-8"),
        }
        restarted_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        completed = await restarted_service.finalize(running.job_id)
        return completed, crash_state, archive, temp_profile

    completed, crash_state, archive, temp_profile = asyncio.run(run())
    assert crash_state == {
        "destination_blocker": True,
        "archive_data": "old",
        "temp_data": "new",
    }
    assert completed.state == "completed"
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert (archive / "old.txt").read_text(encoding="utf-8") == "old"
    assert not temp_profile.exists()


def test_archive_replace_rolls_back_if_second_exchange_fails(tmp_path):
    import src.services.onboarding as onboarding_module

    existing = Token(id=6, st=LONG_ST, email="owner@example.com")
    destination = tmp_path / "profiles" / "6"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("old", encoding="utf-8")
    calls = 0

    def failing_exchange(left, right):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("sensitive path must not escape")
        return onboarding_module._linux_rename_exchange(left, right)

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            path_exchanger=failing_exchange,
        )
        running = await create_and_start(
            service, target_token_id=6, conflict_policy="archive_and_replace"
        )
        temp = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp / "new.txt").write_text("new", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(Exception) as migration_error:
            await service.finalize(running.job_id)
        return migration_error.value, running

    migration_error, running = asyncio.run(run())
    temp = tmp_path / "profiles" / ".onboarding" / running.job_id
    archive = tmp_path / "profiles" / ".archive" / "6" / running.job_id
    assert calls == 3
    assert getattr(migration_error, "code", None) == "profile_migration_failed"
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (temp / "new.txt").read_text(encoding="utf-8") == "new"
    assert not archive.exists()
    assert "sensitive" not in str(migration_error)
    assert migration_error.__cause__ is None
    assert migration_error.__suppress_context__ is True


def test_finalize_is_idempotent_and_restart_resumes_from_persisted_job(tmp_path):
    async def run():
        db = FakeDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path, db=db, token_manager=manager, processes=processes
        )
        running = await create_and_start(first_service)
        close_browser(processes, running)

        restarted_service, _db, _manager, _processes = build_service(
            tmp_path, db=db, token_manager=manager, processes=processes
        )
        recovered = await restarted_service.recover_incomplete()
        completed = await restarted_service.finalize(running.job_id)
        repeated = await restarted_service.finalize(running.job_id)
        return recovered, completed, repeated, manager

    recovered, completed, repeated, manager = asyncio.run(run())
    assert any(job.job_id == completed.job_id for job in recovered)
    assert repeated == completed
    assert manager.inspect_calls == 2
    assert len(manager.add_calls) == 1
    assert len(manager.update_calls) == 1


def test_commit_marker_makes_completion_write_failure_resumable(tmp_path):
    async def run():
        db = FakeDB()
        db.fail_completion_once = True
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path, db=db, token_manager=manager
        )
        running = await create_and_start(service)
        close_browser(processes, running)
        with pytest.raises(Exception) as first_failure:
            await service.finalize(running.job_id)
        committed = await service.get(running.job_id)
        completed = await service.finalize(running.job_id)
        return first_failure.value, committed, completed, manager

    first_failure, committed, completed, manager = asyncio.run(run())
    assert getattr(first_failure, "code", None) == "finalize_failed"
    assert committed.state == "running"
    assert committed.phase == "commit_complete"
    assert completed.state == "completed"
    assert manager.inspect_calls == 2
    assert len(manager.update_calls) == 1


def test_account_commit_failure_preserves_validated_archive_metadata_for_forward_resume(tmp_path):
    existing = Token(id=27, st=LONG_ST, email="owner@example.com", is_active=True)
    destination = tmp_path / "profiles" / "27"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("old", encoding="utf-8")

    async def run():
        db = FakeDB([existing])
        manager = FakeTokenManager(
            db,
            [snapshot(), snapshot(), snapshot(), snapshot()],
        )
        manager.fail_account_commit_once = True
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
            cookie_reader=directory_cookie_reader_for(),
        )
        running = await create_and_start(
            service,
            target_token_id=27,
            conflict_policy="archive_and_replace",
        )
        temp = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp / "new.txt").write_text("new", encoding="utf-8")
        close_browser(processes, running)

        with pytest.raises(Exception) as commit_failure:
            await service.finalize(running.job_id)
        failed = await service.get(running.job_id)
        recovered = await service.recover_incomplete()
        recovered_failed = await service.get(running.job_id)
        completed = await service.finalize(running.job_id)
        return commit_failure.value, failed, recovered, recovered_failed, completed

    commit_failure, failed, recovered, recovered_failed, completed = asyncio.run(run())
    archive = tmp_path / "profiles" / ".archive" / "27" / failed.job_id
    assert getattr(commit_failure, "code", None) == "token_persistence_failed"
    assert failed.state == "failed"
    assert failed.phase == "account_commit"
    assert recovered_failed.state == "failed"
    assert recovered_failed.phase == "account_commit"
    assert any(job == recovered_failed for job in recovered)
    assert failed.conflict_status == "archived_and_replaced"
    assert failed.profile_ready is True
    assert not (tmp_path / "profiles" / ".onboarding" / failed.job_id).exists()
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert (archive / "old.txt").read_text(encoding="utf-8") == "old"
    assert completed.state == "completed"
    assert completed.conflict_status == "archived_and_replaced"
    assert completed.profile_ready is True


def test_failure_after_account_state_mutation_keeps_profile_for_forward_resume(tmp_path):
    existing = Token(
        id=13,
        st=LONG_ST,
        email="owner@example.com",
        is_active=True,
    )

    async def run():
        db = FakeDB([existing])
        db.fail_commit_marker_once = True
        manager = FakeTokenManager(db, [snapshot(), snapshot(), snapshot(), snapshot()])
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path, db=db, token_manager=manager, processes=processes
        )
        running = await create_and_start(
            first_service,
            target_token_id=existing.id,
            requested_business_enabled=False,
        )
        temp = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp / "valid.txt").write_text("valid migrated profile", encoding="utf-8")
        close_browser(processes, running)

        with pytest.raises(Exception) as first_failure:
            await first_service.finalize(running.job_id)
        failed = await first_service.get(running.job_id)
        destination = tmp_path / "profiles" / str(existing.id)
        state_after_failure = {
            "destination_exists": destination.is_dir(),
            "temp_exists": temp.exists(),
            "temp_is_file": temp.is_file(),
            "destination_data": (
                (destination / "valid.txt").read_text(encoding="utf-8")
                if destination.is_dir()
                else None
            ),
        }
        token_after_failure = db.tokens[existing.id].model_copy(deep=True)
        lifecycle_after_failure = db.lifecycles[existing.id].model_copy(deep=True)

        restarted_service, _db, _manager, _processes = build_service(
            tmp_path, db=db, token_manager=manager, processes=processes
        )
        completed = await restarted_service.finalize(running.job_id)
        return (
            first_failure.value,
            failed,
            state_after_failure,
            token_after_failure,
            lifecycle_after_failure,
            completed,
            destination,
        )

    (
        first_failure,
        failed,
        state_after_failure,
        token_after_failure,
        lifecycle_after_failure,
        completed,
        destination,
    ) = asyncio.run(run())
    assert getattr(first_failure, "code", None) == "finalize_failed"
    assert str(first_failure) == "Onboarding finalization failed safely."
    assert failed.state == "failed"
    assert state_after_failure == {
        "destination_exists": True,
        "temp_exists": True,
        "temp_is_file": True,
        "destination_data": "valid migrated profile",
    }
    assert token_after_failure.st == ROTATED_ST
    assert lifecycle_after_failure.profile_state == "ready"
    assert completed.state == "completed"
    assert (destination / "valid.txt").read_text(encoding="utf-8") == "valid migrated profile"


def test_failed_recovery_preserves_reused_pid_when_singleton_is_absent(tmp_path):
    def refuse_stop(_pid, _stop_signal):
        raise OSError("simulated stop refusal")

    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        processes.signal_hook = refuse_stop
        with pytest.raises(Exception):
            await service.finalize(running.job_id)
        failed = await service.get(running.job_id)
        processes.processes[running.browser_pid] = {
            "ticks": running.browser_start_ticks + 1,
            "cmdline": ("/usr/bin/unrelated-reused-process",),
        }
        recovered = await service.recover_incomplete()
        current = await service.get(running.job_id)
        return failed, recovered, current, processes

    failed, recovered, current, processes = asyncio.run(run())
    assert failed.state == "failed"
    assert failed.phase == "stop_browser"
    assert current.browser_pid == failed.browser_pid
    assert current.browser_start_ticks == failed.browser_start_ticks
    assert current.error_code == failed.error_code
    assert any(job.job_id == current.job_id for job in recovered)
    assert processes.processes[failed.browser_pid]["cmdline"] == (
        "/usr/bin/unrelated-reused-process",
    )


def test_awaiting_login_recovery_preserves_reused_pid_without_stale_lock_proof(
    tmp_path,
):
    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        processes.processes[running.browser_pid] = {
            "ticks": running.browser_start_ticks + 1,
            "cmdline": ("/usr/bin/unrelated-reused-process",),
        }
        recovered = await service.recover_incomplete()
        current = await service.get(running.job_id)
        return running, recovered, current, processes

    running, recovered, current, processes = asyncio.run(run())
    assert current.state == "failed"
    assert current.phase == "recovery"
    assert current.error_code == "process_ownership_mismatch"
    assert current.browser_pid == running.browser_pid
    assert current.browser_start_ticks == running.browser_start_ticks
    assert any(job == current for job in recovered)
    assert processes.processes[running.browser_pid]["cmdline"] == (
        "/usr/bin/unrelated-reused-process",
    )


def test_failed_recovery_maps_identity_clear_database_failure_safely(tmp_path):
    class FailingRecoveryClearDB(FakeDB):
        async def clear_onboarding_browser_identity(self, *_args, **_kwargs):
            raise RuntimeError("recovery database failure with sensitive details")

    async def run():
        db = FailingRecoveryClearDB()
        manager = FakeTokenManager(db)
        service, _db, _manager, processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
        )
        running = await create_and_start(service)
        processes.processes.pop(running.browser_pid, None)
        await db.update_onboarding_job_state(
            running.job_id,
            "failed",
            phase="stop_browser",
            error_code="process_stop_failed",
            error_message="safe failure",
        )
        recovered = await service.recover_incomplete()
        current = await service.get(running.job_id)
        return recovered, current

    recovered, current = asyncio.run(run())
    assert current.state == "failed"
    assert current.phase == "stop_browser"
    assert current.error_code == "process_stop_failed"
    assert current.browser_pid == 4321
    assert current.browser_start_ticks == 777
    assert any(job.job_id == current.job_id for job in recovered)


def test_stale_singleton_adoption_cannot_repopulate_cleared_identity(tmp_path):
    async def run():
        db = RacingIdentityReplaceDB()
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        replace_browser_owner(
            processes,
            running,
            temp_profile,
            pid=5432,
            ticks=888,
        )

        def clear_identity_and_stop_successor_before_adoption():
            current = db.jobs[running.job_id]
            db.jobs[running.job_id] = current.model_copy(
                update={"browser_pid": None, "browser_start_ticks": None}
            )
            processes.processes.pop(5432, None)
            (temp_profile / "SingletonLock").unlink()

        db.before_identity_replace = clear_identity_and_stop_successor_before_adoption
        recovered = await service.recover_incomplete()
        current = await service.get(running.job_id)
        return recovered, current, processes

    recovered, current, processes = asyncio.run(run())
    assert current.state == "failed"
    assert current.phase == "recovery"
    assert current.browser_pid is None
    assert current.browser_start_ticks is None
    assert any(job == current for job in recovered)
    assert 5432 not in processes.processes


def test_recovery_adopts_verified_successor_for_running_awaiting_login(tmp_path):
    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        replace_browser_owner(
            processes,
            running,
            temp_profile,
            pid=5432,
            ticks=888,
        )
        processes.processes[running.browser_pid] = {
            "ticks": running.browser_start_ticks + 1,
            "cmdline": ("/usr/bin/unrelated-process",),
        }
        recovered = await service.recover_incomplete()
        current = await service.get(running.job_id)
        return recovered, current

    recovered, current = asyncio.run(run())
    assert current.state == "running"
    assert current.phase == "awaiting_login"
    assert current.browser_pid == 5432
    assert current.browser_start_ticks == 888
    assert any(job == current for job in recovered)


def test_recovery_fails_and_clears_stale_running_browser_identity(tmp_path):
    async def run():
        service, _db, _manager, processes = build_service(tmp_path)
        running = await create_and_start(service)
        close_browser(processes, running)
        recovered = await service.recover_incomplete()
        current = await service.get(running.job_id)
        return recovered, current

    recovered, current = asyncio.run(run())
    assert current.state == "failed"
    assert current.phase == "recovery"
    assert current.error_code == "process_identity_unavailable"
    assert current.browser_pid is None
    assert current.browser_start_ticks is None
    assert any(job == current for job in recovered)


def test_expired_pending_job_still_cancels_instead_of_resuming(tmp_path):
    current = NOW

    def clock():
        return current

    async def run():
        nonlocal current
        service, _db, _manager, processes = build_service(tmp_path, clock=clock)
        pending = await service.create_job()
        current = NOW + timedelta(hours=1)
        cancelled = await service.start_job(pending.job_id)
        return cancelled, processes

    cancelled, processes = asyncio.run(run())
    assert cancelled.state == "cancelled"
    assert cancelled.phase == "cancelled"
    assert processes.launches == []


def test_expired_failed_login_can_resume_and_refresh_ttl_atomically(tmp_path):
    current = NOW

    def clock():
        return current

    async def run():
        nonlocal current
        service, _db, manager, processes = build_service(
            tmp_path,
            clock=clock,
            cookie_reader=lambda **_kwargs: [],
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")
        original_inode = temp_profile.stat().st_ino
        original_expires_at = running.expires_at
        close_browser(processes, running)
        with pytest.raises(Exception):
            await service.finalize(running.job_id)
        current = NOW + timedelta(hours=1)
        processes.next_pid = 5432
        processes.next_ticks = 888

        resumed = await service.start_job(running.job_id)
        return (
            resumed,
            manager,
            processes,
            temp_profile,
            original_inode,
            original_expires_at,
            current,
        )

    (
        resumed,
        manager,
        processes,
        temp_profile,
        original_inode,
        original_expires_at,
        resumed_at,
    ) = asyncio.run(run())
    assert resumed.state == "running"
    assert resumed.phase == "awaiting_login"
    assert resumed.expires_at == resumed_at + timedelta(seconds=600)
    assert resumed.expires_at > original_expires_at
    assert temp_profile.stat().st_ino == original_inode
    assert (temp_profile / "operator-progress").read_text(encoding="utf-8") == "preserve"
    assert len(processes.launches) == 2
    assert processes.stops == []
    assert manager.inspect_calls == 0


def test_concurrent_expired_failed_login_resume_launches_once_and_refreshes_once(
    tmp_path,
):
    current = NOW

    def clock():
        return current

    async def run():
        nonlocal current
        db = FakeDB()
        manager = FakeTokenManager(db)
        first_processes = FakeProcessTable(pid=4321, ticks=777)
        second_processes = FakeProcessTable(pid=6543, ticks=999)
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=first_processes,
            clock=clock,
            cookie_reader=lambda **_kwargs: [],
        )
        second_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=second_processes,
            clock=clock,
            cookie_reader=lambda **_kwargs: [],
        )
        running = await create_and_start(first_service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")
        close_browser(first_processes, running)
        with pytest.raises(Exception):
            await first_service.finalize(running.job_id)
        current = NOW + timedelta(hours=1)
        first_processes.next_pid = 5432
        first_processes.next_ticks = 888

        results = await asyncio.gather(
            first_service.start_job(running.job_id),
            second_service.start_job(running.job_id),
            return_exceptions=True,
        )
        return (
            results,
            await first_service.get(running.job_id),
            first_processes,
            second_processes,
            temp_profile,
            current,
        )

    results, resumed, first_processes, second_processes, temp_profile, resumed_at = (
        asyncio.run(run())
    )
    successful = [result for result in results if isinstance(result, OnboardingJob)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert successful
    assert all(result.job_id == resumed.job_id for result in successful)
    assert all(
        getattr(error, "code", None) == "process_ownership_mismatch"
        for error in failures
    )
    assert resumed.state == "running"
    assert resumed.phase == "awaiting_login"
    assert resumed.expires_at == resumed_at + timedelta(seconds=600)
    assert len(first_processes.launches) + len(second_processes.launches) == 2
    assert (temp_profile / "operator-progress").read_text(encoding="utf-8") == "preserve"


def test_expired_failed_login_recovery_preserves_profile_for_manual_resolution(tmp_path):
    current = NOW

    def clock():
        return current

    async def run():
        nonlocal current
        service, _db, manager, processes = build_service(
            tmp_path,
            clock=clock,
            cookie_reader=lambda **_kwargs: [],
        )
        running = await create_and_start(service)
        temp_profile = tmp_path / "profiles" / ".onboarding" / running.job_id
        (temp_profile / "operator-progress").write_text("preserve", encoding="utf-8")
        close_browser(processes, running)
        with pytest.raises(Exception):
            await service.finalize(running.job_id)
        current = NOW + timedelta(hours=1)

        recovered = await service.recover_incomplete()
        preserved = await service.get(running.job_id)
        return recovered, preserved, manager, processes, temp_profile

    recovered, preserved, manager, processes, temp_profile = asyncio.run(run())
    assert preserved.state == "failed"
    assert preserved.phase == "verify_account"
    assert preserved.error_code == "login_required"
    assert any(job == preserved for job in recovered)
    assert (temp_profile / "operator-progress").read_text(encoding="utf-8") == "preserve"
    assert processes.stops == []
    assert manager.inspect_calls == 0


def test_expired_commit_complete_job_completes_during_recovery(tmp_path):
    current = NOW

    def clock():
        return current

    async def run():
        nonlocal current
        db = FakeDB()
        db.fail_completion_once = True
        manager = FakeTokenManager(db)
        processes = FakeProcessTable()
        first_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
            clock=clock,
        )
        running = await create_and_start(first_service)
        close_browser(processes, running)
        with pytest.raises(Exception) as completion_failure:
            await first_service.finalize(running.job_id)
        committed = await first_service.get(running.job_id)
        current = NOW + timedelta(hours=1)

        restarted_service, _db, _manager, _processes = build_service(
            tmp_path,
            db=db,
            token_manager=manager,
            processes=processes,
            clock=clock,
        )
        recovered = await restarted_service.recover_incomplete()
        completed = await restarted_service.get(running.job_id)
        return completion_failure.value, committed, recovered, completed

    completion_failure, committed, recovered, completed = asyncio.run(run())
    assert getattr(completion_failure, "code", None) == "finalize_failed"
    assert committed.state == "running"
    assert committed.phase == "commit_complete"
    assert completed.state == "completed"
    assert completed.phase == "completed"
    assert any(job.job_id == completed.job_id and job.state == "completed" for job in recovered)


def test_expired_recovery_cancels_safely_and_job_records_never_leak_secrets_or_paths(tmp_path):
    current = NOW

    def clock():
        return current

    async def run():
        nonlocal current
        service, db, _manager, processes = build_service(tmp_path, clock=clock)
        running = await create_and_start(service)
        current = NOW + timedelta(hours=1)
        recovered = await service.recover_incomplete()
        db.jobs[running.job_id] = db.jobs[running.job_id].model_copy(
            update={"error_code": "legacy", "error_message": f"{LONG_ST} {tmp_path}"}
        )
        record = await service.get(running.job_id)
        listed = await service.list()
        return recovered, record, listed, db, processes, running.browser_pid

    recovered, record, listed, db, processes, original_pid = asyncio.run(run())
    serialized = record.model_dump_json()
    assert record.state == "cancelled"
    assert any(job.job_id == record.job_id and job.state == "cancelled" for job in recovered)
    assert LONG_ST not in serialized
    assert ROTATED_ST not in serialized
    assert str(tmp_path) not in serialized
    assert "/configured/chrome" not in serialized
    assert record.error_message == "Onboarding operation failed safely."
    assert listed[0].error_message == "Onboarding operation failed safely."
    assert record.browser_pid is None
    assert record.browser_start_ticks is None
    assert processes.stops == [(original_pid, signal.SIGTERM)]
    assert all(
        LONG_ST not in str(update) and str(tmp_path) not in str(update)
        for update in db.job_updates
    )


def test_symlinked_temp_profile_is_rejected_without_following_it(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()

    async def run():
        service, _db, _manager, _processes = build_service(tmp_path)
        job = await service.create_job()
        onboarding_root = tmp_path / "profiles" / ".onboarding"
        onboarding_root.mkdir(parents=True, mode=0o700)
        (onboarding_root / job.job_id).symlink_to(outside, target_is_directory=True)
        with pytest.raises(Exception) as unsafe:
            await service.start_job(job.job_id)
        return unsafe.value

    unsafe = asyncio.run(run())
    assert getattr(unsafe, "code", None) == "unsafe_profile_path"
    assert outside.exists()
