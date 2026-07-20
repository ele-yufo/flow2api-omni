"""Safe, resumable server-side XRDP account onboarding."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import inspect
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Awaitable, Callable, Optional, Sequence

from ..core.account_identity import VerifiedAccountSnapshot, normalize_account_email
from ..core.account_lifecycle import classify_account_tier
from ..core.models import OnboardingJob, ProfileValidationResult, Token
from ..core.token_states import TOKEN_REASON_ONBOARDING_PENDING, TierClassification
from .keepalive.profile import (
    ProcessSnapshot,
    ProfileBusyError,
    ProfileLeaseBusyError,
    ProfileLockUncertainError,
    SingletonLockState,
    acquire_profile_lease,
    acquire_profile_path_lease,
    inspect_singleton_lock,
    prepare_profile,
    read_proc_cmdline,
    read_proc_start_ticks,
    read_session_token,
    validate_proxy_server,
    verify_process_ownership,
)

FLOW_URL = "https://labs.google/fx/tools/flow"
_ALLOWED_CONFLICT_POLICIES = {"reject", "archive_and_replace"}
_ALLOWED_RUNTIME_MODES = {"persistent", "warm"}
_RESUMABLE_FAILED_PHASES = {"stop_browser", "verify_account"}
_JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_DISPLAY_PATTERN = re.compile(r":[0-9]+(?:\.[0-9]+)?\Z")
_MARKER_NAME = ".flow2api-onboarding"
_MIGRATION_BLOCKER_PREFIX = b"flow2api-onboarding-migration-blocker:"
_CLEANUP_BLOCKER_PREFIX = b"flow2api-onboarding-cleanup-blocker:v2:"
_CLEANUP_NONCE_XATTR = "user.flow2api-cleanup-nonce"
_TERMINAL_STATES = {"completed", "cancelled"}
_RECOVERABLE_BROWSER_FAILURE_PHASES = {
    "browser_start",
    "awaiting_login",
    "cancel",
    "recovery",
}

_SAFE_MESSAGES = {
    "job_not_found": "Onboarding job was not found.",
    "invalid_job_state": "Onboarding job is not in a valid state for this operation.",
    "active_job_exists": "Another XRDP onboarding browser is already active.",
    "target_not_found": "The requested target account does not exist.",
    "target_identity_mismatch": "The signed-in account does not match the requested target.",
    "profile_identity_mismatch": "The retained profile identity does not match its account binding.",
    "duplicate_email": "The signed-in email has ambiguous account records.",
    "login_required": "A valid signed-in Flow session was not found.",
    "account_inspection_failed": "The signed-in Flow account could not be verified.",
    "process_launch_failed": "The onboarding browser could not be started.",
    "process_identity_unavailable": "The onboarding browser identity could not be verified.",
    "process_ownership_mismatch": "The recorded browser process is not owned by this onboarding job.",
    "process_stop_failed": "The onboarding browser could not be stopped safely.",
    "unsafe_profile_path": "The onboarding profile path failed safety validation.",
    "profile_not_found": "The onboarding profile is not available.",
    "destination_conflict": "A retained profile already exists for this account.",
    "archive_conflict": "The retained archive destination already exists.",
    "profile_migration_failed": "The onboarding profile could not be migrated safely.",
    "profile_rollback_failed": "The onboarding profile migration could not be rolled back safely.",
    "token_persistence_failed": "The verified account could not be persisted.",
    "final_validation_failed": "The migrated profile could not be validated.",
    "finalize_failed": "Onboarding finalization failed safely.",
}


class OnboardingServiceError(RuntimeError):
    """Public onboarding failure carrying only a stable code and safe message."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_SAFE_MESSAGES.get(code, "Onboarding operation failed safely."))


@dataclass(frozen=True)
class _ProfileMigration:
    temp: Path
    destination: Path
    archive: Optional[Path]
    conflict_status: str


@contextmanager
def _private_umask():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


class _LinuxPidfdHandle:
    def __init__(self, file_descriptor: int):
        self._file_descriptor = file_descriptor

    def send_signal(self, stop_signal: signal.Signals) -> None:
        file_descriptor = self._file_descriptor
        if file_descriptor is None:
            raise OSError(errno.EBADF, "pidfd is closed")
        native_sender = getattr(signal, "pidfd_send_signal", None)
        if native_sender is not None:
            native_sender(file_descriptor, stop_signal)
            return
        libc = ctypes.CDLL(None, use_errno=True)
        sender = getattr(libc, "pidfd_send_signal", None)
        if sender is not None:
            sender.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint,
            ]
            sender.restype = ctypes.c_int
            result = sender(file_descriptor, int(stop_signal), None, 0)
        else:
            syscall = getattr(libc, "syscall", None)
            if syscall is None:
                raise OSError(errno.ENOSYS, "pidfd_send_signal is unavailable")
            syscall.restype = ctypes.c_long
            result = syscall(424, file_descriptor, int(stop_signal), None, 0)
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))

    def close(self) -> None:
        file_descriptor = self._file_descriptor
        if file_descriptor is None:
            return
        self._file_descriptor = None
        os.close(file_descriptor)


def _open_linux_pidfd(pid: int):
    native_opener = getattr(os, "pidfd_open", None)
    if native_opener is not None:
        return _LinuxPidfdHandle(native_opener(pid, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    opener = getattr(libc, "pidfd_open", None)
    if opener is not None:
        opener.argtypes = [ctypes.c_int, ctypes.c_uint]
        opener.restype = ctypes.c_int
        file_descriptor = opener(pid, 0)
    else:
        syscall = getattr(libc, "syscall", None)
        if syscall is None:
            raise OSError(errno.ENOSYS, "pidfd_open is unavailable")
        syscall.restype = ctypes.c_long
        file_descriptor = syscall(434, pid, 0)
    if file_descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return _LinuxPidfdHandle(file_descriptor)


def _default_cleanup_nonce() -> str:
    return secrets.token_hex(32)


def _linux_rename_exchange(left: os.PathLike[str], right: os.PathLike[str]) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    exchanger = getattr(libc, "renameat2", None)
    if exchanger is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    exchanger.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    exchanger.restype = ctypes.c_int
    if exchanger(
        -100,
        os.fsencode(left),
        -100,
        os.fsencode(right),
        2,
    ) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _serialize_onboarding_operation(method):
    """Hold one cross-process job lease around a terminal onboarding operation."""

    @wraps(method)
    async def serialized(self, job_id: str, *args, **kwargs):
        job = await self._get_job(job_id)
        temp_path = self._temp_path(job.job_id)
        lease = await self._acquire_onboarding_operation_lease(
            job.job_id,
            temp_path,
        )
        try:
            return await method(self, job_id, *args, **kwargs)
        finally:
            try:
                release_result = lease.release()
                if inspect.isawaitable(release_result):
                    await release_result
            except Exception:
                pass

    return serialized


class OnboardingService:
    """Orchestrate visible Chrome login, strict verification, and profile adoption."""

    def __init__(
        self,
        *,
        db,
        token_manager,
        profile_base: os.PathLike[str] | str,
        browser_executable: os.PathLike[str] | str,
        display: str,
        proxy: Optional[str],
        session_ttl_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        process_launcher: Callable = subprocess.Popen,
        process_handle_opener: Callable = _open_linux_pidfd,
        path_exchanger: Callable = _linux_rename_exchange,
        cleanup_nonce_factory: Callable = _default_cleanup_nonce,
        cookie_reader: Optional[Callable] = None,
        profile_lease_acquirer: Callable = acquire_profile_lease,
        onboarding_profile_lease_acquirer: Callable = acquire_profile_path_lease,
        onboarding_operation_lease_acquirer: Callable = acquire_profile_path_lease,
        profile_preparer: Callable = prepare_profile,
        process_cmdline_reader: Callable[[int], Optional[Sequence[str]]] = read_proc_cmdline,
        process_start_ticks_reader: Callable[[int], Optional[int]] = read_proc_start_ticks,
    ):
        self.db = db
        self.token_manager = token_manager
        self.browser_executable = str(browser_executable or "").strip()
        self.display = str(display or "").strip()
        self.proxy = validate_proxy_server(proxy)
        self.session_ttl_seconds = self._positive_int(session_ttl_seconds)
        self.clock = clock
        self.sleep = sleep
        self.process_launcher = process_launcher
        self.process_handle_opener = process_handle_opener
        self.path_exchanger = path_exchanger
        self.cleanup_nonce_factory = cleanup_nonce_factory
        self.cookie_reader = cookie_reader
        self.profile_lease_acquirer = profile_lease_acquirer
        self.onboarding_profile_lease_acquirer = onboarding_profile_lease_acquirer
        self.onboarding_operation_lease_acquirer = onboarding_operation_lease_acquirer
        self.profile_preparer = profile_preparer
        self.process_cmdline_reader = process_cmdline_reader
        self.process_start_ticks_reader = process_start_ticks_reader
        self._operation_lock = asyncio.Lock()

        if not self.browser_executable or any(
            character in self.browser_executable for character in ("\0", "\n", "\r")
        ):
            raise ValueError("browser_executable must be configured safely")
        if not _DISPLAY_PATTERN.fullmatch(self.display):
            raise ValueError("display must be a configured X display")

        raw_base = Path(profile_base).expanduser().absolute()
        try:
            if raw_base.is_symlink():
                raise OnboardingServiceError("unsafe_profile_path")
            if raw_base.exists():
                if not raw_base.is_dir():
                    raise OnboardingServiceError("unsafe_profile_path")
            else:
                with _private_umask():
                    raw_base.mkdir(mode=0o700, parents=True, exist_ok=False)
        except OnboardingServiceError:
            raise
        except OSError as error:
            raise OnboardingServiceError("unsafe_profile_path") from None
        self.profile_base = raw_base.resolve(strict=True)

    @staticmethod
    def _positive_int(value) -> int:
        if isinstance(value, bool):
            raise TypeError("session_ttl_seconds must be a positive integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("session_ttl_seconds must be a positive integer") from None
        if parsed <= 0:
            raise ValueError("session_ttl_seconds must be a positive integer")
        return parsed

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _job_component(self, job_id: str) -> str:
        component = str(job_id or "")
        if not _JOB_ID_PATTERN.fullmatch(component):
            raise OnboardingServiceError("unsafe_profile_path")
        return component

    def _token_component(self, token_id: int) -> str:
        if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id <= 0:
            raise OnboardingServiceError("unsafe_profile_path")
        return str(token_id)

    def _assert_within_base(self, candidate: Path) -> None:
        try:
            candidate.resolve(strict=False).relative_to(self.profile_base)
        except (OSError, ValueError) as error:
            raise OnboardingServiceError("unsafe_profile_path") from None

    def _assert_no_structural_symlink(self, candidate: Path) -> None:
        self._assert_within_base(candidate)
        try:
            relative = candidate.absolute().relative_to(self.profile_base)
        except ValueError as error:
            raise OnboardingServiceError("unsafe_profile_path") from None
        current = self.profile_base
        for component in relative.parts:
            current = current / component
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as error:
                raise OnboardingServiceError("unsafe_profile_path") from None
            if stat.S_ISLNK(mode):
                raise OnboardingServiceError("unsafe_profile_path")

    def _temp_path(self, job_id: str) -> Path:
        path = self.profile_base / ".onboarding" / self._job_component(job_id)
        self._assert_no_structural_symlink(path)
        return path

    def _final_path(self, token_id: int) -> Path:
        path = self.profile_base / self._token_component(token_id)
        self._assert_no_structural_symlink(path)
        return path

    def _archive_path(self, token_id: int, job_id: str) -> Path:
        path = (
            self.profile_base
            / ".archive"
            / self._token_component(token_id)
            / self._job_component(job_id)
        )
        self._assert_no_structural_symlink(path)
        return path

    def _secure_directory(self, path: Path) -> None:
        self._assert_no_structural_symlink(path)
        try:
            with _private_umask():
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(path, 0o700)
        except OSError as error:
            raise OnboardingServiceError("unsafe_profile_path") from None
        self._assert_no_structural_symlink(path)

    def _marker_path(self, profile_path: Path) -> Path:
        return profile_path / _MARKER_NAME

    def _migration_blocker_payload(self, job_id: str) -> bytes:
        return _MIGRATION_BLOCKER_PREFIX + self._job_component(job_id).encode("ascii")

    def _is_migration_blocker(self, path: Path, job_id: str) -> bool:
        try:
            path_stat = path.lstat()
            if not stat.S_ISREG(path_stat.st_mode):
                return False
            return path.read_bytes() == self._migration_blocker_payload(job_id)
        except (OSError, OnboardingServiceError):
            return False

    def _create_migration_blocker(self, path: Path, job_id: str) -> None:
        self._assert_no_structural_symlink(path)
        payload = self._migration_blocker_payload(job_id)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o400)
            try:
                os.write(descriptor, payload)
                os.fchmod(descriptor, 0o400)
            finally:
                os.close(descriptor)
        except OSError:
            raise OnboardingServiceError("profile_migration_failed") from None

    def _remove_migration_blocker(self, path: Path, job_id: str) -> None:
        if not self._is_migration_blocker(path, job_id):
            raise OnboardingServiceError("profile_migration_failed")
        try:
            path.unlink()
        except OSError:
            raise OnboardingServiceError("profile_migration_failed") from None

    def _cleanup_quarantine_path(self, job_id: str) -> Path:
        path = (
            self.profile_base
            / ".onboarding"
            / f".{self._job_component(job_id)}.cleanup-quarantine"
        )
        self._assert_no_structural_symlink(path)
        return path

    def _cleanup_blocker_staging_path(self, job_id: str) -> Path:
        path = (
            self.profile_base
            / ".onboarding"
            / f".{self._job_component(job_id)}.cleanup-blocker-staging"
        )
        self._assert_no_structural_symlink(path)
        return path

    def _new_cleanup_nonce(self) -> bytes:
        try:
            value = self.cleanup_nonce_factory()
        except Exception:
            raise OnboardingServiceError("unsafe_profile_path") from None
        if isinstance(value, bytes):
            try:
                text = value.decode("ascii")
            except UnicodeError:
                raise OnboardingServiceError("unsafe_profile_path") from None
        else:
            text = str(value)
        if re.fullmatch(r"[0-9a-f]{64}", text) is None:
            raise OnboardingServiceError("unsafe_profile_path")
        return text.encode("ascii")

    def _cleanup_blocker_payload(
        self,
        job_id: str,
        expected_device: int,
        expected_inode: int,
        nonce: bytes,
    ) -> bytes:
        job_component = self._job_component(job_id).encode("ascii")
        return b":".join(
            (
                _CLEANUP_BLOCKER_PREFIX.rstrip(b":"),
                job_component,
                str(expected_device).encode("ascii"),
                str(expected_inode).encode("ascii"),
                b"dir",
                nonce,
            )
        )

    def _read_cleanup_blocker_identity(
        self,
        path: Path,
        job_id: str,
    ) -> Optional[tuple[int, int, bytes]]:
        try:
            path_stat = path.lstat()
            if not stat.S_ISREG(path_stat.st_mode):
                return None
            fields = path.read_bytes().split(b":")
            expected_prefix = _CLEANUP_BLOCKER_PREFIX.rstrip(b":").split(b":")
            if fields[: len(expected_prefix)] != expected_prefix:
                return None
            remainder = fields[len(expected_prefix) :]
            if len(remainder) != 5 or remainder[0] != self._job_component(job_id).encode("ascii"):
                return None
            if remainder[3] != b"dir" or not remainder[1].isdigit() or not remainder[2].isdigit():
                return None
            if re.fullmatch(rb"[0-9a-f]{64}", remainder[4]) is None:
                return None
            expected_device = int(remainder[1])
            expected_inode = int(remainder[2])
            if expected_device < 0 or expected_inode <= 0:
                return None
            return expected_device, expected_inode, remainder[4]
        except (OSError, ValueError, OnboardingServiceError):
            return None

    def _is_cleanup_blocker(self, path: Path, job_id: str) -> bool:
        return self._read_cleanup_blocker_identity(path, job_id) is not None

    def _cleanup_directory_stat(self, path: Path):
        return path.lstat()

    def _read_cleanup_nonce(self, path: Path) -> Optional[bytes]:
        try:
            return os.getxattr(
                path,
                _CLEANUP_NONCE_XATTR,
                follow_symlinks=False,
            )
        except (AttributeError, OSError):
            return None

    def _set_cleanup_nonce(self, path: Path, nonce: bytes) -> None:
        try:
            os.setxattr(
                path,
                _CLEANUP_NONCE_XATTR,
                nonce,
                follow_symlinks=False,
            )
            if self._read_cleanup_nonce(path) != nonce:
                raise OSError(errno.EIO, "cleanup nonce verification failed")
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(path, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except (AttributeError, OSError):
            raise OnboardingServiceError("unsafe_profile_path") from None

    def _remove_cleanup_nonce(self, path: Path, nonce: bytes) -> None:
        if self._read_cleanup_nonce(path) != nonce:
            raise OnboardingServiceError("unsafe_profile_path")
        try:
            os.removexattr(
                path,
                _CLEANUP_NONCE_XATTR,
                follow_symlinks=False,
            )
        except (AttributeError, OSError):
            raise OnboardingServiceError("unsafe_profile_path") from None

    def _cleanup_blocker_matches_directory(
        self,
        blocker_path: Path,
        directory_path: Path,
        job_id: str,
    ) -> bool:
        identity = self._read_cleanup_blocker_identity(blocker_path, job_id)
        if identity is None:
            return False
        try:
            directory_stat = self._cleanup_directory_stat(directory_path)
        except OSError:
            return False
        return (
            stat.S_ISDIR(directory_stat.st_mode)
            and not stat.S_ISLNK(directory_stat.st_mode)
            and identity[:2] == (directory_stat.st_dev, directory_stat.st_ino)
            and self._read_cleanup_nonce(directory_path) == identity[2]
        )

    def _open_cleanup_staging(self, path: Path) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags, 0o400)

    def _write_cleanup_staging(self, descriptor: int, payload: bytes) -> None:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError(errno.EIO, "cleanup blocker write failed")
            written += count

    def _fsync_cleanup_file(self, descriptor: int) -> None:
        os.fsync(descriptor)

    def _publish_cleanup_staging(self, staging: Path, final_path: Path) -> None:
        os.link(staging, final_path, follow_symlinks=False)

    def _fsync_cleanup_parent(self, parent: Path) -> None:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(parent, directory_flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _cleanup_stale_cleanup_staging(self, job_id: str) -> None:
        staging = self._cleanup_blocker_staging_path(job_id)
        try:
            staging_stat = staging.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise OnboardingServiceError("unsafe_profile_path") from None
        if stat.S_ISLNK(staging_stat.st_mode) or not stat.S_ISREG(staging_stat.st_mode):
            raise OnboardingServiceError("unsafe_profile_path")
        try:
            staging.unlink()
            self._fsync_cleanup_parent(staging.parent)
        except OSError:
            raise OnboardingServiceError("unsafe_profile_path") from None

    def _create_cleanup_blocker(
        self,
        path: Path,
        job_id: str,
        expected_directory_stat: os.stat_result,
        nonce: bytes,
    ) -> None:
        self._assert_no_structural_symlink(path)
        if not stat.S_ISDIR(expected_directory_stat.st_mode):
            raise OnboardingServiceError("unsafe_profile_path")
        payload = self._cleanup_blocker_payload(
            job_id,
            expected_directory_stat.st_dev,
            expected_directory_stat.st_ino,
            nonce,
        )
        expected_identity = (
            expected_directory_stat.st_dev,
            expected_directory_stat.st_ino,
            nonce,
        )
        staging = self._cleanup_blocker_staging_path(job_id)
        self._cleanup_stale_cleanup_staging(job_id)
        try:
            descriptor = self._open_cleanup_staging(staging)
            try:
                os.fchmod(descriptor, 0o400)
                self._write_cleanup_staging(descriptor, payload)
                self._fsync_cleanup_file(descriptor)
            finally:
                os.close(descriptor)

            try:
                self._publish_cleanup_staging(staging, path)
            except FileExistsError:
                if self._read_cleanup_blocker_identity(path, job_id) != expected_identity:
                    raise
            if self._read_cleanup_blocker_identity(path, job_id) != expected_identity:
                raise OSError(errno.EIO, "cleanup blocker publication failed")
            staging.unlink()
            self._fsync_cleanup_parent(path.parent)
        except Exception:
            try:
                staging_stat = staging.lstat()
            except OSError:
                pass
            else:
                if stat.S_ISREG(staging_stat.st_mode) and not stat.S_ISLNK(
                    staging_stat.st_mode
                ):
                    try:
                        staging.unlink()
                    except OSError:
                        pass
            raise OnboardingServiceError("unsafe_profile_path") from None

    def _remove_cleanup_blocker(self, path: Path, job_id: str) -> None:
        if not self._is_cleanup_blocker(path, job_id):
            raise OnboardingServiceError("unsafe_profile_path")
        try:
            path.unlink()
        except OSError:
            raise OnboardingServiceError("unsafe_profile_path") from None

    def _exchange_paths(
        self,
        left: Path,
        right: Path,
        *,
        error_code: str = "profile_migration_failed",
    ) -> None:
        try:
            result = self.path_exchanger(left, right)
        except OSError:
            raise OnboardingServiceError(error_code) from None
        if inspect.isawaitable(result):
            raise OnboardingServiceError(error_code)

    def _assert_fenced_profile_not_live(
        self,
        profile_path: Path,
        original_temp_path: Path,
    ) -> None:
        inspection = inspect_singleton_lock(
            profile_path,
            process_reader=self._read_process_snapshot,
        )
        if inspection.state is SingletonLockState.UNSAFE:
            raise OnboardingServiceError("process_identity_unavailable")
        if inspection.state is SingletonLockState.BUSY:
            raise OnboardingServiceError("process_ownership_mismatch")
        if inspection.pid is not None and verify_process_ownership(
            inspection.pid,
            original_temp_path,
            cmdline_reader=self.process_cmdline_reader,
            start_ticks_reader=self.process_start_ticks_reader,
        ):
            raise OnboardingServiceError("process_ownership_mismatch")

    def _prepare_temp_profile(self, job_id: str) -> Path:
        temp_path = self._temp_path(job_id)
        self._secure_directory(temp_path.parent)
        if temp_path.exists():
            self._assert_no_structural_symlink(temp_path)
            if not temp_path.is_dir() or not self._has_valid_marker(temp_path, job_id):
                raise OnboardingServiceError("unsafe_profile_path")
            os.chmod(temp_path, 0o700)
            return temp_path

        self._secure_directory(temp_path)
        marker_path = self._marker_path(temp_path)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(marker_path, flags, 0o600)
            try:
                os.write(descriptor, self._job_component(job_id).encode("ascii"))
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise OnboardingServiceError("unsafe_profile_path") from None
        return temp_path

    def _require_existing_temp_profile(self, job_id: str) -> Path:
        temp_path = self._temp_path(job_id)
        try:
            temp_stat = temp_path.lstat()
        except FileNotFoundError:
            raise OnboardingServiceError("profile_not_found") from None
        except OSError:
            raise OnboardingServiceError("unsafe_profile_path") from None
        if stat.S_ISLNK(temp_stat.st_mode) or not stat.S_ISDIR(temp_stat.st_mode):
            raise OnboardingServiceError("unsafe_profile_path")
        if not self._has_valid_marker(temp_path, job_id):
            raise OnboardingServiceError("unsafe_profile_path")
        return temp_path

    def _has_valid_marker(self, profile_path: Path, job_id: str) -> bool:
        marker_path = self._marker_path(profile_path)
        try:
            marker_stat = marker_path.lstat()
            if not stat.S_ISREG(marker_stat.st_mode):
                return False
            return marker_path.read_text(encoding="ascii") == self._job_component(job_id)
        except (OSError, UnicodeError, OnboardingServiceError):
            return False

    def _assert_profile_not_live(self, profile_path: Path) -> None:
        inspection = inspect_singleton_lock(
            profile_path,
            process_reader=self._read_process_snapshot,
        )
        if inspection.state is SingletonLockState.BUSY:
            raise OnboardingServiceError("process_ownership_mismatch")
        if inspection.state is SingletonLockState.UNSAFE:
            raise OnboardingServiceError("process_identity_unavailable")

    def _remove_service_temp(self, job_id: str) -> None:
        temp_path = self._temp_path(job_id)
        quarantine = self._cleanup_quarantine_path(job_id)
        self._cleanup_stale_cleanup_staging(job_id)

        blocker_identity = self._read_cleanup_blocker_identity(temp_path, job_id)
        if blocker_identity is not None:
            if not quarantine.exists():
                self._remove_cleanup_blocker(temp_path, job_id)
                return
            if not self._cleanup_blocker_matches_directory(
                temp_path, quarantine, job_id
            ):
                raise OnboardingServiceError("unsafe_profile_path")
            marker_path = self._marker_path(quarantine)
            try:
                marker_path.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                raise OnboardingServiceError("unsafe_profile_path") from None
            else:
                if not self._has_valid_marker(quarantine, job_id):
                    raise OnboardingServiceError("unsafe_profile_path")
            try:
                self._assert_fenced_profile_not_live(quarantine, temp_path)
            except OnboardingServiceError as error:
                try:
                    self._exchange_paths(
                        temp_path,
                        quarantine,
                        error_code="profile_rollback_failed",
                    )
                    self._remove_cleanup_blocker(quarantine, job_id)
                    self._remove_cleanup_nonce(temp_path, blocker_identity[2])
                except OnboardingServiceError:
                    raise OnboardingServiceError("profile_rollback_failed") from None
                raise error from None
            try:
                shutil.rmtree(quarantine)
            except OSError:
                raise OnboardingServiceError("unsafe_profile_path") from None
            self._remove_cleanup_blocker(temp_path, job_id)
            return

        try:
            temp_stat = temp_path.lstat()
        except FileNotFoundError:
            if self._is_cleanup_blocker(quarantine, job_id):
                self._remove_cleanup_blocker(quarantine, job_id)
            return
        except OSError:
            raise OnboardingServiceError("unsafe_profile_path") from None
        if stat.S_ISLNK(temp_stat.st_mode) or not stat.S_ISDIR(temp_stat.st_mode):
            raise OnboardingServiceError("unsafe_profile_path")
        if not self._has_valid_marker(temp_path, job_id):
            raise OnboardingServiceError("unsafe_profile_path")

        if quarantine.exists():
            if not self._cleanup_blocker_matches_directory(
                quarantine, temp_path, job_id
            ):
                raise OnboardingServiceError("unsafe_profile_path")
        else:
            nonce = self._new_cleanup_nonce()
            self._set_cleanup_nonce(temp_path, nonce)
            try:
                self._create_cleanup_blocker(
                    quarantine,
                    job_id,
                    temp_stat,
                    nonce,
                )
            except OnboardingServiceError:
                if not self._cleanup_blocker_matches_directory(
                    quarantine, temp_path, job_id
                ):
                    try:
                        self._remove_cleanup_nonce(temp_path, nonce)
                    except OnboardingServiceError:
                        pass
                raise

        prepared_identity = self._read_cleanup_blocker_identity(
            quarantine, job_id
        )
        if prepared_identity is None:
            raise OnboardingServiceError("unsafe_profile_path")
        self._assert_profile_not_live(temp_path)
        self._exchange_paths(
            temp_path,
            quarantine,
            error_code="unsafe_profile_path",
        )
        try:
            if not self._cleanup_blocker_matches_directory(
                temp_path, quarantine, job_id
            ):
                raise OnboardingServiceError("unsafe_profile_path")
            self._assert_fenced_profile_not_live(quarantine, temp_path)
        except OnboardingServiceError as error:
            try:
                self._exchange_paths(
                    temp_path,
                    quarantine,
                    error_code="profile_rollback_failed",
                )
                self._remove_cleanup_blocker(quarantine, job_id)
                self._remove_cleanup_nonce(temp_path, prepared_identity[2])
            except OnboardingServiceError:
                raise OnboardingServiceError("profile_rollback_failed") from None
            raise error from None

        try:
            shutil.rmtree(quarantine)
        except OSError:
            raise OnboardingServiceError("unsafe_profile_path") from None
        self._remove_cleanup_blocker(temp_path, job_id)

    async def _signal_browser_generation(
        self,
        pid: int,
        start_ticks: int,
        profile_path: Path,
        stop_signal: signal.Signals,
    ) -> bool:
        """Open a pidfd, verify ownership, then signal that bound generation."""
        try:
            handle = self.process_handle_opener(pid)
            if inspect.isawaitable(handle):
                handle = await handle
        except ProcessLookupError:
            return False
        except OSError:
            raise OnboardingServiceError("process_identity_unavailable") from None
        except Exception:
            raise OnboardingServiceError("process_identity_unavailable") from None

        if not callable(getattr(handle, "send_signal", None)) or not callable(
            getattr(handle, "close", None)
        ):
            try:
                close = getattr(handle, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            raise OnboardingServiceError("process_identity_unavailable")

        try:
            observed_ticks = self.process_start_ticks_reader(pid)
            if observed_ticks is None:
                return False
            if observed_ticks != start_ticks or not verify_process_ownership(
                pid,
                profile_path,
                expected_start_ticks=start_ticks,
                cmdline_reader=self.process_cmdline_reader,
                start_ticks_reader=self.process_start_ticks_reader,
            ):
                raise OnboardingServiceError("process_ownership_mismatch")
            signal_result = handle.send_signal(stop_signal)
            if inspect.isawaitable(signal_result):
                await signal_result
            return True
        except ProcessLookupError:
            return False
        except OSError as error:
            if error.errno in {errno.ENOSYS, errno.EOPNOTSUPP, errno.EBADF}:
                raise OnboardingServiceError("process_identity_unavailable") from None
            raise
        finally:
            try:
                close_result = handle.close()
                if inspect.isawaitable(close_result):
                    await close_result
            except Exception:
                pass

    @staticmethod
    def _safe_job(job: OnboardingJob) -> OnboardingJob:
        if job.error_message is None:
            return job
        message = _SAFE_MESSAGES.get(job.error_code, "Onboarding operation failed safely.")
        return job.model_copy(update={"error_message": message})

    async def _get_job(self, job_id: str) -> OnboardingJob:
        job = await self.db.get_onboarding_job(job_id)
        if job is None:
            raise OnboardingServiceError("job_not_found")
        self._job_component(job.job_id)
        return self._safe_job(job)

    async def get(self, job_id: str) -> OnboardingJob:
        return await self._get_job(job_id)

    def get_safe_config(self) -> dict[str, str]:
        """Return only the validated display identifier needed by the admin UI."""
        return {"display": self.display}

    async def validate_profile(self, token_id: int) -> ProfileValidationResult:
        """Read and verify one retained profile without mutating account state."""
        self._token_component(token_id)
        account = await self.db.get_token(token_id)
        lifecycle = await self.db.get_token_lifecycle(token_id)
        if account is None:
            raise OnboardingServiceError("target_not_found")
        if lifecycle is None:
            raise OnboardingServiceError("profile_identity_mismatch")

        profile_path = self._final_path(token_id)
        try:
            profile_stat = profile_path.lstat()
        except FileNotFoundError:
            raise OnboardingServiceError("profile_not_found") from None
        except OSError:
            raise OnboardingServiceError("unsafe_profile_path") from None
        if stat.S_ISLNK(profile_stat.st_mode) or not stat.S_ISDIR(profile_stat.st_mode):
            raise OnboardingServiceError("unsafe_profile_path")

        session_token = self._read_profile_session(profile_path)
        verified = await self._inspect_session(session_token)
        account_email = normalize_account_email(account.email)
        bound_email = normalize_account_email(lifecycle.verified_email)
        if verified.normalized_email != account_email:
            raise OnboardingServiceError("target_identity_mismatch")
        if not bound_email or verified.normalized_email != bound_email:
            raise OnboardingServiceError("profile_identity_mismatch")

        try:
            projects = await self.db.get_projects_by_token(token_id)
        except Exception:
            raise OnboardingServiceError("final_validation_failed") from None
        project_count = sum(1 for project in projects if project.is_active)
        return ProfileValidationResult(
            email=verified.email,
            tier=verified.user_paygate_tier,
            credits=verified.credits,
            expiry=verified.at_expires,
            project_count=project_count,
            profile_ready=lifecycle.profile_state == "ready",
        )

    async def list(
        self,
        *,
        target_token_id: Optional[int] = None,
        resolved_token_id: Optional[int] = None,
        state: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> list[OnboardingJob]:
        jobs = await self.db.list_onboarding_jobs(
            target_token_id=target_token_id,
            resolved_token_id=resolved_token_id,
            state=state,
            phase=phase,
        )
        return [self._safe_job(job) for job in jobs]

    async def create_job(
        self,
        *,
        target_token_id: Optional[int] = None,
        conflict_policy: str = "reject",
        requested_business_enabled: bool = False,
        requested_keepalive_enabled: bool = False,
        requested_runtime_mode: str = "warm",
    ) -> OnboardingJob:
        if target_token_id is not None:
            self._token_component(target_token_id)
            if await self.db.get_token(target_token_id) is None:
                raise OnboardingServiceError("target_not_found")
        if conflict_policy not in _ALLOWED_CONFLICT_POLICIES:
            raise ValueError("conflict_policy must be reject or archive_and_replace")
        if not isinstance(requested_business_enabled, bool):
            raise TypeError("requested_business_enabled must be a bool")
        if not isinstance(requested_keepalive_enabled, bool):
            raise TypeError("requested_keepalive_enabled must be a bool")
        if requested_runtime_mode not in _ALLOWED_RUNTIME_MODES:
            raise ValueError("requested_runtime_mode must be persistent or warm")

        job = OnboardingJob(
            target_token_id=target_token_id,
            conflict_policy=conflict_policy,
            requested_business_enabled=requested_business_enabled,
            requested_keepalive_enabled=requested_keepalive_enabled,
            requested_runtime_mode=requested_runtime_mode,
            expires_at=self._now() + timedelta(seconds=self.session_ttl_seconds),
        )
        job_id = await self.db.create_onboarding_job(job)
        return await self._get_job(job_id)

    def _browser_argv(self, temp_path: Path) -> list[str]:
        argv = [
            self.browser_executable,
            f"--user-data-dir={temp_path}",
            "--no-first-run",
            "--password-store=basic",
        ]
        if self.proxy:
            argv.append(f"--proxy-server={self.proxy}")
        argv.append(FLOW_URL)
        return argv

    def _is_expired(self, job: OnboardingJob) -> bool:
        expires_at = job.expires_at
        if expires_at is None:
            return False
        expires_at = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        return self._now() >= expires_at

    async def _get_process_control_job(self, job_id: str) -> OnboardingJob:
        try:
            return await self._get_job(job_id)
        except OnboardingServiceError:
            raise
        except Exception:
            raise OnboardingServiceError("process_identity_unavailable") from None

    async def _replace_browser_identity(
        self,
        job: OnboardingJob,
        *,
        browser_pid: Optional[int],
        browser_start_ticks: Optional[int],
    ) -> bool:
        try:
            return await self.db.replace_onboarding_browser_identity(
                job.job_id,
                expected_pid=job.browser_pid,
                expected_start_ticks=job.browser_start_ticks,
                browser_pid=browser_pid,
                browser_start_ticks=browser_start_ticks,
            )
        except Exception:
            raise OnboardingServiceError("process_identity_unavailable") from None

    async def _clear_failed_browser_identity(self, job: OnboardingJob) -> bool:
        try:
            return await self.db.clear_onboarding_browser_identity(
                job.job_id,
                expected_pid=job.browser_pid,
                expected_start_ticks=job.browser_start_ticks,
            )
        except Exception:
            raise OnboardingServiceError("process_identity_unavailable") from None

    async def _clear_identity_with_stale_lock_proof(
        self,
        job: OnboardingJob,
        temp_path: Path,
    ) -> Optional[OnboardingJob]:
        lease = await self._acquire_onboarding_profile_lease(job.job_id, temp_path)
        try:
            report = await self._prepare_stopped_onboarding_profile(lease)
            if report.inspection.state is not SingletonLockState.STALE:
                raise OnboardingServiceError("process_ownership_mismatch")
            return await self._clear_stopped_browser_identity(job, temp_path)
        finally:
            try:
                release_result = lease.release()
                if inspect.isawaitable(release_result):
                    await release_result
            except Exception:
                pass

    async def _reconcile_failed_browser_job(self, job: OnboardingJob) -> bool:
        """Return whether a failed job still owns a live onboarding browser."""
        temp_path = self._temp_path(job.job_id)
        current = job
        for _ in range(4):
            pid = current.browser_pid
            expected_ticks = current.browser_start_ticks
            observed_ticks = (
                self.process_start_ticks_reader(pid) if pid is not None else None
            )
            owns_profile = (
                pid is not None
                and observed_ticks is not None
                and (expected_ticks is None or observed_ticks == expected_ticks)
                and verify_process_ownership(
                    pid,
                    temp_path,
                    expected_start_ticks=expected_ticks,
                    cmdline_reader=self.process_cmdline_reader,
                    start_ticks_reader=self.process_start_ticks_reader,
                )
            )
            if owns_profile:
                if expected_ticks is None:
                    await self._replace_browser_identity(
                        current,
                        browser_pid=pid,
                        browser_start_ticks=observed_ticks,
                    )
                    current = await self._get_process_control_job(current.job_id)
                    continue
                return True

            successor = await self._adopt_singleton_owner(current, temp_path)
            if successor is not None:
                return True
            if observed_ticks is not None:
                successor = await self._clear_identity_with_stale_lock_proof(
                    current,
                    temp_path,
                )
                return successor is not None
            if pid is not None and self.process_cmdline_reader(pid) is not None:
                raise OnboardingServiceError("process_identity_unavailable")
            if pid is None and expected_ticks is None:
                return False

            successor = await self._clear_stopped_browser_identity(
                current,
                temp_path,
            )
            return successor is not None
        raise OnboardingServiceError("process_identity_unavailable")

    async def _reconcile_failed_browser_jobs(self) -> None:
        for failed_job in await self.db.list_onboarding_jobs(state="failed"):
            try:
                if await self._reconcile_failed_browser_job(failed_job):
                    raise OnboardingServiceError("active_job_exists")
            except OnboardingServiceError:
                raise
            except Exception:
                raise OnboardingServiceError("process_identity_unavailable") from None

    @staticmethod
    def _failed_job_can_resume_login(job: OnboardingJob) -> bool:
        if job.state != "failed" or job.phase not in _RESUMABLE_FAILED_PHASES:
            return False
        return all(
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

    async def _prepare_failed_resume_profile(
        self,
        job: OnboardingJob,
        temp_path: Path,
        lease,
    ) -> None:
        ownership_error = None
        try:
            owner = await self._resolve_owned_browser(job, temp_path)
        except OnboardingServiceError as error:
            if error.code != "process_ownership_mismatch":
                raise
            ownership_error = error
            owner = None
        if owner is not None:
            raise OnboardingServiceError("process_ownership_mismatch")

        report = await self._prepare_stopped_onboarding_profile(lease)
        if (
            ownership_error is not None
            and report.inspection.state is not SingletonLockState.STALE
        ):
            raise ownership_error

    @_serialize_onboarding_operation
    async def start_job(self, job_id: str) -> OnboardingJob:
        async with self._operation_lock:
            job = await self._get_job(job_id)
            if job.state == "running":
                if job.phase == "browser_start":
                    return await self._recover_launch_ownership(
                        job,
                        self._temp_path(job.job_id),
                    )
                return job
            if job.state == "failed":
                return await self._resume_failed_login_job(job)
            if job.state in _TERMINAL_STATES or job.state != "pending":
                raise OnboardingServiceError("invalid_job_state")
            if self._is_expired(job):
                return await self._cancel_locked(job)

            await self._reconcile_failed_browser_jobs()
            try:
                claimed = await self.db.claim_onboarding_job(job.job_id)
            except Exception:
                raise OnboardingServiceError("process_launch_failed") from None
            if not claimed:
                current = await self._get_job(job.job_id)
                if current.state == "running":
                    return current
                raise OnboardingServiceError("active_job_exists")
            job = await self._get_job(job.job_id)

            try:
                temp_path = self._prepare_temp_profile(job.job_id)
            except OnboardingServiceError as error:
                await self._fail_job(job, error.code, "browser_start")
                raise
            return await self._launch_claimed_job(
                job,
                temp_path,
                preserve_profile_on_failure=False,
            )

    async def _resume_failed_login_job(self, job: OnboardingJob) -> OnboardingJob:
        if not self._failed_job_can_resume_login(job):
            raise OnboardingServiceError("invalid_job_state")
        temp_path = self._require_existing_temp_profile(job.job_id)
        lease = await self._acquire_onboarding_profile_lease(job.job_id, temp_path)
        try:
            await self._prepare_failed_resume_profile(job, temp_path, lease)
            refreshed_expires_at = self._now() + timedelta(
                seconds=self.session_ttl_seconds
            )
            try:
                claimed = await self.db.claim_failed_onboarding_job_resume(
                    job.job_id,
                    expected_phase=job.phase,
                    expected_error_code=job.error_code,
                    expected_pid=job.browser_pid,
                    expected_start_ticks=job.browser_start_ticks,
                    expected_expires_at=job.expires_at,
                    refreshed_expires_at=refreshed_expires_at,
                )
            except Exception:
                raise OnboardingServiceError("process_launch_failed") from None
            if not claimed:
                current = await self._get_job(job.job_id)
                if current.state == "running":
                    return current
                if not self._failed_job_can_resume_login(current):
                    raise OnboardingServiceError("invalid_job_state")
                raise OnboardingServiceError("active_job_exists")

            claimed_job = await self._get_job(job.job_id)
            try:
                await self._prepare_failed_resume_profile(
                    claimed_job,
                    temp_path,
                    lease,
                )
            except OnboardingServiceError as error:
                await self._fail_job(claimed_job, error.code, "browser_start")
                raise
            return await self._launch_claimed_job(
                claimed_job,
                temp_path,
                preserve_profile_on_failure=True,
            )
        finally:
            try:
                release_result = lease.release()
                if inspect.isawaitable(release_result):
                    await release_result
            except Exception:
                pass

    async def _launch_claimed_job(
        self,
        job: OnboardingJob,
        temp_path: Path,
        *,
        preserve_profile_on_failure: bool,
    ) -> OnboardingJob:
        environment = os.environ.copy()
        environment["DISPLAY"] = self.display
        try:
            with _private_umask():
                process = self.process_launcher(
                    self._browser_argv(temp_path),
                    shell=False,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
        except Exception:
            if not preserve_profile_on_failure:
                self._remove_service_temp(job.job_id)
            await self._fail_job(job, "process_launch_failed", "browser_start")
            raise OnboardingServiceError("process_launch_failed") from None

        pid = getattr(process, "pid", None)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            if not preserve_profile_on_failure:
                self._remove_service_temp(job.job_id)
            await self._fail_job(job, "process_identity_unavailable", "browser_start")
            raise OnboardingServiceError("process_identity_unavailable")

        pid_only_job = job.model_copy(
            update={"browser_pid": pid, "browser_start_ticks": None}
        )
        try:
            published = await self._replace_browser_identity(
                job,
                browser_pid=pid,
                browser_start_ticks=None,
            )
            if not published:
                raise OnboardingServiceError("process_identity_unavailable")
        except Exception:
            await self._stop_recently_launched(pid, temp_path)
            if not preserve_profile_on_failure:
                self._remove_service_temp(job.job_id)
            raise OnboardingServiceError("process_launch_failed") from None

        start_ticks = None
        for attempt in range(5):
            start_ticks = self.process_start_ticks_reader(pid)
            if start_ticks is not None:
                break
            if attempt < 4:
                await self.sleep(0.05)
        if isinstance(start_ticks, bool) or not isinstance(start_ticks, int) or start_ticks < 0:
            await self._fail_job(job, "process_identity_unavailable", "browser_start")
            raise OnboardingServiceError("process_identity_unavailable")

        launched_job = job.model_copy(
            update={"browser_pid": pid, "browser_start_ticks": start_ticks}
        )
        try:
            published = await self._replace_browser_identity(
                pid_only_job,
                browser_pid=pid,
                browser_start_ticks=start_ticks,
            )
            if not published:
                raise OnboardingServiceError("process_identity_unavailable")
        except Exception:
            try:
                await self._stop_recently_launched(pid, temp_path)
            except OnboardingServiceError:
                pass
            await self._fail_job(
                pid_only_job,
                "process_identity_unavailable",
                "browser_start",
            )
            raise OnboardingServiceError("process_launch_failed") from None

        if not verify_process_ownership(
            pid,
            temp_path,
            expected_start_ticks=start_ticks,
            cmdline_reader=self.process_cmdline_reader,
            start_ticks_reader=self.process_start_ticks_reader,
        ):
            try:
                await self._fail_job(
                    launched_job,
                    "process_ownership_mismatch",
                    "browser_start",
                )
                await self._reconcile_failed_browser_job(
                    await self._get_job(job.job_id)
                )
            except Exception:
                try:
                    await self._fail_job(
                        launched_job,
                        "process_ownership_mismatch",
                        "browser_start",
                    )
                except Exception:
                    pass
            raise OnboardingServiceError("process_ownership_mismatch")

        try:
            if not verify_process_ownership(
                pid,
                temp_path,
                expected_start_ticks=start_ticks,
                cmdline_reader=self.process_cmdline_reader,
                start_ticks_reader=self.process_start_ticks_reader,
            ):
                await self._fail_job(
                    launched_job,
                    "process_ownership_mismatch",
                    "browser_start",
                )
                await self._reconcile_failed_browser_job(
                    await self._get_job(job.job_id)
                )
                raise OnboardingServiceError("process_ownership_mismatch")

            current = await self._get_process_control_job(job.job_id)
            expected_identity = (pid, start_ticks)
            current_identity = (
                current.browser_pid,
                current.browser_start_ticks,
            )
            if current_identity != expected_identity:
                await self._retire_launched_generation(launched_job, temp_path)
                raise OnboardingServiceError("process_identity_unavailable")
            if current.state != "running" or current.phase != "browser_start":
                await self._retire_launched_generation(launched_job, temp_path)
                raise OnboardingServiceError("active_job_exists")

            transitioned = await self._transition_job_state(
                current,
                state="running",
                phase="awaiting_login",
                failure_code="process_launch_failed",
            )
            if not transitioned:
                latest = await self._get_process_control_job(job.job_id)
                await self._retire_launched_generation(launched_job, temp_path)
                if latest.state in _TERMINAL_STATES:
                    raise OnboardingServiceError("active_job_exists")
                raise OnboardingServiceError("process_identity_unavailable")

            latest = await self._get_process_control_job(job.job_id)
            latest_identity = (
                latest.browser_pid,
                latest.browser_start_ticks,
            )
            if (
                latest.state != "running"
                or latest.phase != "awaiting_login"
                or latest_identity != expected_identity
            ):
                await self._retire_launched_generation(launched_job, temp_path)
                if latest.state in _TERMINAL_STATES:
                    raise OnboardingServiceError("active_job_exists")
                raise OnboardingServiceError("process_identity_unavailable")
            return latest
        except OnboardingServiceError as error:
            if error.code == "process_launch_failed":
                await self._retire_launched_generation(launched_job, temp_path)
                try:
                    current = await self._get_process_control_job(job.job_id)
                    if current.state == "running" and current.phase == "browser_start":
                        failure_phase = (
                            "verify_account"
                            if preserve_profile_on_failure
                            else "browser_start"
                        )
                        await self._fail_job(
                            current,
                            "process_launch_failed",
                            failure_phase,
                        )
                except Exception:
                    pass
            raise
        except Exception:
            await self._retire_launched_generation(launched_job, temp_path)
            raise OnboardingServiceError("process_launch_failed") from None

    async def _stop_recently_launched(self, pid: int, temp_path: Path) -> None:
        """Stop a just-launched PID only while its exact generation remains owned."""
        start_ticks = self.process_start_ticks_reader(pid)
        if isinstance(start_ticks, bool) or not isinstance(start_ticks, int) or start_ticks < 0:
            raise OnboardingServiceError("process_identity_unavailable")
        try:
            signaled = await self._signal_browser_generation(
                pid,
                start_ticks,
                temp_path,
                signal.SIGTERM,
            )
        except (OSError, ProcessLookupError):
            if self.process_start_ticks_reader(pid) != start_ticks:
                return
            raise OnboardingServiceError("process_stop_failed") from None
        if not signaled:
            return
        for _ in range(20):
            if self.process_start_ticks_reader(pid) != start_ticks:
                return
            await self.sleep(0.1)
        try:
            signaled = await self._signal_browser_generation(
                pid,
                start_ticks,
                temp_path,
                signal.SIGKILL,
            )
        except (OSError, ProcessLookupError):
            if self.process_start_ticks_reader(pid) != start_ticks:
                return
            raise OnboardingServiceError("process_stop_failed") from None
        if not signaled:
            return
        await self.sleep(0.1)
        if self.process_start_ticks_reader(pid) == start_ticks:
            raise OnboardingServiceError("process_stop_failed")

    async def _retire_launched_generation(
        self,
        launched_job: OnboardingJob,
        temp_path: Path,
    ) -> None:
        """Stop and clear only the exact Chrome generation launched by this call."""
        try:
            await self._stop_browser_generation(launched_job, temp_path)
        except Exception:
            return
        if (
            self.process_start_ticks_reader(launched_job.browser_pid)
            == launched_job.browser_start_ticks
        ):
            return
        try:
            await self._clear_failed_browser_identity(launched_job)
        except Exception:
            pass

    async def _fail_recoverable_launch(
        self,
        launched_job: OnboardingJob,
        temp_path: Path,
    ) -> OnboardingJob:
        await self._retire_launched_generation(launched_job, temp_path)
        current = await self._get_process_control_job(launched_job.job_id)
        if current.state in _TERMINAL_STATES or current.state == "failed":
            return current
        if current.state != "running" or current.phase != "browser_start":
            raise OnboardingServiceError("process_identity_unavailable")
        await self._fail_job(
            current,
            "process_identity_unavailable",
            "verify_account",
        )
        return await self._get_job(launched_job.job_id)

    def _read_process_snapshot(self, pid: int) -> Optional[ProcessSnapshot]:
        initial_ticks = self.process_start_ticks_reader(pid)
        if initial_ticks is None:
            return None
        cmdline = self.process_cmdline_reader(pid)
        if cmdline is None:
            return None
        final_ticks = self.process_start_ticks_reader(pid)
        if final_ticks is None or final_ticks != initial_ticks:
            return None
        return ProcessSnapshot(pid=pid, start_ticks=initial_ticks, cmdline=tuple(cmdline))

    async def _recover_launch_ownership(
        self,
        job: OnboardingJob,
        temp_path: Path,
    ) -> OnboardingJob:
        """Recover ownership recorded before or immediately after Chrome launch."""
        pid = job.browser_pid
        if pid is None:
            inspection = None
            for attempt in range(5):
                inspection = inspect_singleton_lock(
                    temp_path,
                    process_reader=self._read_process_snapshot,
                )
                if (
                    inspection.state is SingletonLockState.BUSY
                    and inspection.pid is not None
                ):
                    break
                if attempt < 4:
                    await self.sleep(0.1)
            if (
                inspection is None
                or inspection.state is not SingletonLockState.BUSY
                or inspection.pid is None
            ):
                await self._fail_job(
                    job, "process_identity_unavailable", "browser_start"
                )
                return await self._get_job(job.job_id)
            pid = inspection.pid

        start_ticks = self.process_start_ticks_reader(pid)
        if start_ticks is None:
            await self._fail_job(job, "process_identity_unavailable", "browser_start")
            return await self._get_job(job.job_id)
        if not verify_process_ownership(
            pid,
            temp_path,
            expected_start_ticks=start_ticks,
            cmdline_reader=self.process_cmdline_reader,
            start_ticks_reader=self.process_start_ticks_reader,
        ):
            await self._fail_job(job, "process_ownership_mismatch", "recovery")
            return await self._get_job(job.job_id)
        replaced = await self._replace_browser_identity(
            job,
            browser_pid=pid,
            browser_start_ticks=start_ticks,
        )
        current = await self._get_process_control_job(job.job_id)
        if not replaced or (
            current.browser_pid != pid
            or current.browser_start_ticks != start_ticks
        ):
            await self._fail_job(
                current,
                "process_identity_unavailable",
                "recovery",
            )
            return await self._get_job(job.job_id)
        if current.state != "running" or current.phase != "browser_start":
            if current.state in _TERMINAL_STATES:
                return current
            await self._fail_job(
                current,
                "process_identity_unavailable",
                "recovery",
            )
            return await self._get_job(job.job_id)

        try:
            transitioned = await self._transition_job_state(
                current,
                state="running",
                phase="awaiting_login",
                failure_code="process_identity_unavailable",
            )
        except OnboardingServiceError:
            return await self._fail_recoverable_launch(current, temp_path)
        if not transitioned:
            latest = await self._get_process_control_job(job.job_id)
            if (
                latest.state == "running"
                and latest.phase == "awaiting_login"
                and latest.browser_pid == pid
                and latest.browser_start_ticks == start_ticks
            ):
                return latest
            if latest.state in _TERMINAL_STATES:
                return latest
            if (
                latest.state == "running"
                and latest.phase == "browser_start"
                and latest.browser_pid == pid
                and latest.browser_start_ticks == start_ticks
            ):
                return await self._fail_recoverable_launch(latest, temp_path)
            raise OnboardingServiceError("process_identity_unavailable")
        return await self._get_job(job.job_id)

    def _owns_browser(self, job: OnboardingJob, temp_path: Path) -> bool:
        return verify_process_ownership(
            job.browser_pid,
            temp_path,
            expected_start_ticks=job.browser_start_ticks,
            cmdline_reader=self.process_cmdline_reader,
            start_ticks_reader=self.process_start_ticks_reader,
        )

    async def _adopt_singleton_owner(
        self,
        job: OnboardingJob,
        temp_path: Path,
    ) -> Optional[OnboardingJob]:
        inspection = inspect_singleton_lock(
            temp_path,
            process_reader=self._read_process_snapshot,
        )
        if inspection.state is SingletonLockState.UNSAFE:
            raise OnboardingServiceError("process_identity_unavailable")
        if inspection.state is not SingletonLockState.BUSY or inspection.pid is None:
            return None
        start_ticks = self.process_start_ticks_reader(inspection.pid)
        if start_ticks is None or not verify_process_ownership(
            inspection.pid,
            temp_path,
            expected_start_ticks=start_ticks,
            cmdline_reader=self.process_cmdline_reader,
            start_ticks_reader=self.process_start_ticks_reader,
        ):
            raise OnboardingServiceError("process_ownership_mismatch")
        await self._replace_browser_identity(
            job,
            browser_pid=inspection.pid,
            browser_start_ticks=start_ticks,
        )
        current = await self._get_process_control_job(job.job_id)
        if (
            current.browser_pid == inspection.pid
            and current.browser_start_ticks == start_ticks
        ):
            return current
        raise OnboardingServiceError("process_identity_unavailable")

    async def _resolve_owned_browser(
        self,
        job: OnboardingJob,
        temp_path: Path,
    ) -> Optional[OnboardingJob]:
        pid = job.browser_pid
        start_ticks = job.browser_start_ticks
        observed_ticks = self.process_start_ticks_reader(pid) if pid is not None else None
        if (
            pid is not None
            and start_ticks is not None
            and observed_ticks == start_ticks
            and self._owns_browser(job, temp_path)
        ):
            return job

        successor = await self._adopt_singleton_owner(job, temp_path)
        if successor is not None:
            return successor
        if observed_ticks is not None:
            raise OnboardingServiceError("process_ownership_mismatch")
        if pid is not None and self.process_cmdline_reader(pid) is not None:
            raise OnboardingServiceError("process_identity_unavailable")
        return None

    async def _stop_browser_generation(
        self,
        owner: OnboardingJob,
        temp_path: Path,
    ) -> None:
        try:
            signaled = await self._signal_browser_generation(
                owner.browser_pid,
                owner.browser_start_ticks,
                temp_path,
                signal.SIGTERM,
            )
        except (OSError, ProcessLookupError):
            if self.process_start_ticks_reader(owner.browser_pid) != owner.browser_start_ticks:
                return
            raise OnboardingServiceError("process_stop_failed") from None
        if not signaled:
            return

        for _ in range(20):
            observed_ticks = self.process_start_ticks_reader(owner.browser_pid)
            if observed_ticks is None or observed_ticks != owner.browser_start_ticks:
                return
            await self.sleep(0.1)

        try:
            signaled = await self._signal_browser_generation(
                owner.browser_pid,
                owner.browser_start_ticks,
                temp_path,
                signal.SIGKILL,
            )
        except (OSError, ProcessLookupError):
            if self.process_start_ticks_reader(owner.browser_pid) != owner.browser_start_ticks:
                return
            raise OnboardingServiceError("process_stop_failed") from None
        if not signaled:
            return
        await self.sleep(0.1)
        if self.process_start_ticks_reader(owner.browser_pid) == owner.browser_start_ticks:
            raise OnboardingServiceError("process_stop_failed")

    async def _clear_stopped_browser_identity(
        self,
        owner: OnboardingJob,
        temp_path: Path,
    ) -> Optional[OnboardingJob]:
        current = owner
        for _ in range(4):
            await self._clear_failed_browser_identity(current)
            current = await self._get_process_control_job(owner.job_id)
            successor = await self._resolve_owned_browser(current, temp_path)
            if successor is not None:
                return successor
            if (
                current.browser_pid is None
                and current.browser_start_ticks is None
            ):
                return None
        raise OnboardingServiceError("process_identity_unavailable")

    async def _stop_owned_browser(self, job: OnboardingJob, temp_path: Path) -> None:
        owner = await self._resolve_owned_browser(job, temp_path)
        if owner is None and (
            job.browser_pid is not None or job.browser_start_ticks is not None
        ):
            owner = await self._clear_stopped_browser_identity(job, temp_path)

        for _ in range(3):
            if owner is None:
                return
            await self._stop_browser_generation(owner, temp_path)
            owner = await self._clear_stopped_browser_identity(owner, temp_path)
        if owner is not None:
            raise OnboardingServiceError("process_stop_failed")

    async def _acquire_onboarding_path_lease(
        self,
        job_id: str,
        temp_path: Path,
        *,
        acquirer: Callable,
        lease_key_prefix: str,
        busy_code: str,
    ):
        lease_key = f"{lease_key_prefix}-{self._job_component(job_id)}"
        try:
            lease = acquirer(self.profile_base, temp_path, lease_key)
            if inspect.isawaitable(lease):
                lease = await lease
        except ProfileLeaseBusyError:
            raise OnboardingServiceError(busy_code) from None
        except Exception:
            raise OnboardingServiceError("unsafe_profile_path") from None

        release = getattr(lease, "release", None)
        try:
            lease_path = Path(lease.profile_path).expanduser().resolve(strict=False)
            active = bool(lease.active)
            valid = (
                lease_path == temp_path.resolve(strict=False)
                and active
                and callable(release)
            )
        except Exception:
            valid = False
        if not valid:
            if callable(release):
                try:
                    release()
                except Exception:
                    pass
            raise OnboardingServiceError("unsafe_profile_path")
        return lease

    async def _acquire_onboarding_profile_lease(
        self,
        job_id: str,
        temp_path: Path,
    ):
        return await self._acquire_onboarding_path_lease(
            job_id,
            temp_path,
            acquirer=self.onboarding_profile_lease_acquirer,
            lease_key_prefix="onboarding",
            busy_code="process_ownership_mismatch",
        )

    async def _acquire_onboarding_operation_lease(
        self,
        job_id: str,
        temp_path: Path,
    ):
        return await self._acquire_onboarding_path_lease(
            job_id,
            temp_path,
            acquirer=self.onboarding_operation_lease_acquirer,
            lease_key_prefix="onboarding-operation",
            busy_code="active_job_exists",
        )

    async def _prepare_stopped_onboarding_profile(self, lease):
        try:
            report = self.profile_preparer(
                lease,
                process_reader=self._read_process_snapshot,
            )
            if inspect.isawaitable(report):
                report = await report
        except ProfileBusyError:
            raise OnboardingServiceError("process_ownership_mismatch") from None
        except ProfileLockUncertainError:
            raise OnboardingServiceError("process_identity_unavailable") from None
        except OnboardingServiceError:
            raise
        except Exception:
            raise OnboardingServiceError("process_identity_unavailable") from None

        try:
            state = report.inspection.state
        except Exception:
            raise OnboardingServiceError("process_identity_unavailable") from None
        if state not in {SingletonLockState.ABSENT, SingletonLockState.STALE}:
            raise OnboardingServiceError("process_identity_unavailable")
        return report

    async def _stop_and_prepare_onboarding_profile(
        self,
        job: OnboardingJob,
        temp_path: Path,
    ) -> None:
        lease = await self._acquire_onboarding_profile_lease(job.job_id, temp_path)
        try:
            ownership_error = None
            try:
                await self._stop_owned_browser(job, temp_path)
            except OnboardingServiceError as error:
                if error.code != "process_ownership_mismatch":
                    raise
                ownership_error = error

            report = await self._prepare_stopped_onboarding_profile(lease)
            if ownership_error is not None:
                if report.inspection.state is not SingletonLockState.STALE:
                    raise ownership_error
                successor = await self._clear_stopped_browser_identity(
                    job,
                    temp_path,
                )
                if successor is not None:
                    raise OnboardingServiceError("process_ownership_mismatch")
        finally:
            try:
                release_result = lease.release()
                if inspect.isawaitable(release_result):
                    await release_result
            except Exception:
                pass

    async def _fail_job(self, job: OnboardingJob, code: str, phase: str) -> None:
        await self.db.update_onboarding_job_state(
            job.job_id,
            "failed",
            phase=phase,
            error_code=code,
            error_message=_SAFE_MESSAGES[code],
        )

    async def _transition_job_state(
        self,
        job: OnboardingJob,
        *,
        state: str,
        phase: str,
        failure_code: str = "finalize_failed",
    ) -> bool:
        try:
            return await self.db.transition_onboarding_job_state(
                job.job_id,
                expected_state=job.state,
                expected_phase=job.phase,
                state=state,
                phase=phase,
                clear_error=True,
            )
        except Exception:
            raise OnboardingServiceError(failure_code) from None

    def _read_profile_session(self, profile_path: Path) -> str:
        try:
            return read_session_token(profile_path, cookie_reader=self.cookie_reader)
        except Exception as error:
            raise OnboardingServiceError("login_required") from None

    async def _inspect_session(self, session_token: str) -> VerifiedAccountSnapshot:
        try:
            verified = await self.token_manager.inspect_account(session_token)
        except Exception as error:
            raise OnboardingServiceError("account_inspection_failed") from None
        if not isinstance(verified, VerifiedAccountSnapshot):
            raise OnboardingServiceError("account_inspection_failed")
        if not verified.normalized_email or (
            verified.normalized_email != normalize_account_email(verified.email)
        ):
            raise OnboardingServiceError("account_inspection_failed")
        return verified

    async def _resolve_account(
        self, job: OnboardingJob, verified: VerifiedAccountSnapshot
    ) -> Token:
        target = None
        if job.target_token_id is not None:
            target = await self.db.get_token(job.target_token_id)
            if target is None:
                raise OnboardingServiceError("target_not_found")
            if normalize_account_email(target.email) != verified.normalized_email:
                raise OnboardingServiceError("target_identity_mismatch")

        try:
            existing = await self.token_manager.find_token_by_email(verified.email)
        except ValueError as error:
            raise OnboardingServiceError("duplicate_email") from None
        if target is not None and (existing is None or existing.id != target.id):
            raise OnboardingServiceError("target_identity_mismatch")
        if existing is not None:
            return existing

        try:
            created = await self.token_manager.add_token(
                st=verified.st,
                is_active=False,
                ban_reason=TOKEN_REASON_ONBOARDING_PENDING,
                verified_snapshot=verified,
            )
        except ValueError as error:
            try:
                existing = await self.token_manager.find_token_by_email(verified.email)
            except ValueError as duplicate_error:
                raise OnboardingServiceError("duplicate_email") from None
            if existing is not None:
                return existing
            raise OnboardingServiceError("token_persistence_failed") from None
        except Exception as error:
            raise OnboardingServiceError("token_persistence_failed") from None
        return created

    async def _acquire_token_profile_lease(self, token_id: int):
        try:
            lease = self.profile_lease_acquirer(self.profile_base, token_id)
            if inspect.isawaitable(lease):
                lease = await lease
        except ProfileLeaseBusyError:
            raise OnboardingServiceError("process_ownership_mismatch") from None
        except Exception:
            raise OnboardingServiceError("unsafe_profile_path") from None

        release = getattr(lease, "release", None)
        try:
            expected_path = self._final_path(token_id).resolve(strict=False)
            lease_path = Path(lease.profile_path).expanduser().resolve(strict=False)
            active = bool(lease.active)
            valid = lease_path == expected_path and active and callable(release)
        except Exception:
            valid = False
        if not valid:
            if callable(release):
                try:
                    release()
                except Exception:
                    pass
            raise OnboardingServiceError("unsafe_profile_path")
        return lease

    def _preflight_destination(
        self, job: OnboardingJob, token_id: int, temp_path: Path
    ) -> tuple[Path, Optional[Path], str]:
        try:
            if not temp_path.is_dir() or temp_path.is_symlink():
                raise OnboardingServiceError("profile_not_found")
            if not self._has_valid_marker(temp_path, job.job_id):
                raise OnboardingServiceError("unsafe_profile_path")
            self._assert_profile_not_live(temp_path)
            base_device = self.profile_base.stat().st_dev
            if temp_path.stat().st_dev != base_device:
                raise OnboardingServiceError("profile_migration_failed")
        except FileNotFoundError as error:
            raise OnboardingServiceError("profile_not_found") from None
        except OSError as error:
            raise OnboardingServiceError("unsafe_profile_path") from None

        destination = self._final_path(token_id)
        archive = None
        conflict_status = "no_conflict"
        try:
            destination_exists = destination.lstat() is not None
        except FileNotFoundError:
            destination_exists = False
        except OSError:
            raise OnboardingServiceError("unsafe_profile_path") from None

        try:
            archive_candidate = None
            if job.conflict_policy == "archive_and_replace":
                archive_candidate = self._archive_path(token_id, job.job_id)
                self._secure_directory(archive_candidate.parent)
                if archive_candidate.parent.stat().st_dev != base_device:
                    raise OnboardingServiceError("profile_migration_failed")

            destination_blocker = destination_exists and self._is_migration_blocker(
                destination, job.job_id
            )
            if destination_exists and not destination_blocker:
                if destination.is_symlink() or not destination.is_dir():
                    raise OnboardingServiceError("unsafe_profile_path")
                self._assert_profile_not_live(destination)
                if destination.stat().st_dev != base_device:
                    raise OnboardingServiceError("profile_migration_failed")
                if job.conflict_policy == "reject":
                    raise OnboardingServiceError("destination_conflict")

            archive_exists = False
            archive_blocker = False
            if archive_candidate is not None:
                try:
                    archive_candidate.lstat()
                    archive_exists = True
                    archive_blocker = self._is_migration_blocker(
                        archive_candidate, job.job_id
                    )
                except FileNotFoundError:
                    pass

            if job.conflict_policy == "archive_and_replace":
                if destination_exists and not destination_blocker:
                    if archive_exists and not archive_blocker:
                        raise OnboardingServiceError("archive_conflict")
                    archive = archive_candidate
                    conflict_status = "archived_and_replaced"
                elif destination_blocker:
                    if archive_exists and archive_blocker:
                        raise OnboardingServiceError("profile_migration_failed")
                    if archive_exists:
                        if archive_candidate.is_symlink() or not archive_candidate.is_dir():
                            raise OnboardingServiceError("unsafe_profile_path")
                        archive = archive_candidate
                        conflict_status = "archived_and_replaced"
                elif archive_exists:
                    if archive_blocker:
                        raise OnboardingServiceError("profile_migration_failed")
                    if archive_candidate.is_symlink() or not archive_candidate.is_dir():
                        raise OnboardingServiceError("unsafe_profile_path")
                    archive = archive_candidate
                    conflict_status = "archived_and_replaced"

            if (
                archive is not None
                and archive.exists()
                and not self._is_migration_blocker(archive, job.job_id)
                and archive.stat().st_dev != base_device
            ):
                raise OnboardingServiceError("profile_migration_failed")
        except OnboardingServiceError:
            raise
        except OSError:
            raise OnboardingServiceError("unsafe_profile_path") from None
        return destination, archive, conflict_status

    def _resumed_profile_migration(
        self,
        job: OnboardingJob,
        temp_path: Path,
    ) -> Optional[_ProfileMigration]:
        """Detect a crash after the service-owned profile reached its destination."""
        if job.resolved_token_id is None:
            return None
        if temp_path.exists() and not self._is_migration_blocker(
            temp_path, job.job_id
        ):
            return None
        destination = self._final_path(job.resolved_token_id)
        try:
            if destination.is_symlink() or not destination.is_dir():
                return None
            self._assert_profile_not_live(destination)
            if not self._has_valid_marker(destination, job.job_id):
                return None
            if destination.stat().st_dev != self.profile_base.stat().st_dev:
                raise OnboardingServiceError("profile_migration_failed")
            archive = None
            conflict_status = job.conflict_status or "no_conflict"
            if job.conflict_policy == "archive_and_replace":
                archive_candidate = self._archive_path(
                    job.resolved_token_id, job.job_id
                )
                if archive_candidate.exists():
                    if archive_candidate.is_symlink() or not archive_candidate.is_dir():
                        raise OnboardingServiceError("unsafe_profile_path")
                    archive = archive_candidate
                    conflict_status = "archived_and_replaced"
            return _ProfileMigration(
                temp_path,
                destination,
                archive,
                conflict_status,
            )
        except OnboardingServiceError:
            raise
        except OSError:
            raise OnboardingServiceError("unsafe_profile_path") from None

    def _migrate_profile(
        self,
        job_id: str,
        temp_path: Path,
        destination: Path,
        archive: Optional[Path],
        conflict_status: str,
    ) -> _ProfileMigration:
        archive_exchanged = False
        profile_exchanged = False
        try:
            if archive is not None:
                if archive.exists() and not self._is_migration_blocker(archive, job_id):
                    archive_exchanged = True
                else:
                    if not archive.exists():
                        self._create_migration_blocker(archive, job_id)
                    self._exchange_paths(destination, archive)
                    archive_exchanged = True

            if not destination.exists():
                self._create_migration_blocker(destination, job_id)
            if not self._is_migration_blocker(destination, job_id):
                raise OnboardingServiceError("profile_migration_failed")

            self._assert_profile_not_live(temp_path)
            self._exchange_paths(temp_path, destination)
            profile_exchanged = True
            self._assert_fenced_profile_not_live(destination, temp_path)
        except OnboardingServiceError as error:
            try:
                if profile_exchanged:
                    self._exchange_paths(temp_path, destination)
                if archive_exchanged and archive is not None:
                    self._exchange_paths(destination, archive)
                if self._is_migration_blocker(destination, job_id):
                    self._remove_migration_blocker(destination, job_id)
                if archive is not None and self._is_migration_blocker(archive, job_id):
                    self._remove_migration_blocker(archive, job_id)
            except OnboardingServiceError:
                raise OnboardingServiceError("profile_rollback_failed") from None
            raise error from None
        return _ProfileMigration(
            temp_path,
            destination,
            archive,
            conflict_status,
        )

    def _rollback_migration(self, migration: _ProfileMigration) -> None:
        job_id = migration.temp.name
        try:
            if self._is_migration_blocker(migration.temp, job_id):
                self._assert_profile_not_live(migration.destination)
                self._exchange_paths(migration.temp, migration.destination)
                if migration.archive is not None:
                    self._assert_profile_not_live(migration.archive)
                    self._exchange_paths(migration.destination, migration.archive)
                    self._remove_migration_blocker(migration.archive, job_id)
                else:
                    self._remove_migration_blocker(migration.destination, job_id)
                return

            if migration.destination.exists() and not migration.temp.exists():
                self._assert_profile_not_live(migration.destination)
                os.rename(migration.destination, migration.temp)
            if migration.archive is not None and migration.archive.exists():
                self._assert_profile_not_live(migration.archive)
                os.rename(migration.archive, migration.destination)
        except (OSError, OnboardingServiceError):
            raise OnboardingServiceError("profile_rollback_failed") from None

    async def _apply_final_account_state(
        self,
        job: OnboardingJob,
        token: Token,
        verified: VerifiedAccountSnapshot,
    ) -> int:
        try:
            await self.token_manager.update_token(
                token.id,
                verified_snapshot=verified,
                allow_auth_reactivate=False,
            )
            projects = await self.token_manager.ensure_project_pool(token.id)
            is_paid = (
                classify_account_tier(verified.user_paygate_tier)
                is TierClassification.PAID
            )
            await self.token_manager.finalize_onboarding_account_state(
                token.id,
                keepalive_enabled=job.requested_keepalive_enabled,
                runtime_mode=job.requested_runtime_mode,
                enable_business_if_pending=(
                    job.requested_business_enabled and is_paid
                ),
                completed_at=self._now(),
            )
            return sum(1 for project in projects if project.is_active)
        except Exception:
            raise OnboardingServiceError("token_persistence_failed") from None

    async def _complete_committed_job(self, job: OnboardingJob) -> OnboardingJob:
        if job.resolved_token_id is None:
            raise OnboardingServiceError("final_validation_failed")
        destination = self._final_path(job.resolved_token_id)
        if not destination.is_dir() or destination.is_symlink():
            raise OnboardingServiceError("final_validation_failed")
        marker = self._marker_path(destination)
        try:
            temp_path = self._temp_path(job.job_id)
            if self._is_migration_blocker(temp_path, job.job_id):
                self._remove_migration_blocker(temp_path, job.job_id)
            elif temp_path.exists():
                raise OnboardingServiceError("profile_migration_failed")
            if marker.exists() and not marker.is_symlink():
                marker.unlink()
            current = await self._get_job(job.job_id)
            if current.state == "completed":
                return current
            transitioned = await self._transition_job_state(
                current,
                state="completed",
                phase="completed",
            )
            if not transitioned:
                latest = await self._get_job(job.job_id)
                if latest.state == "completed":
                    return latest
                raise OnboardingServiceError("active_job_exists")
        except OnboardingServiceError:
            raise
        except Exception:
            raise OnboardingServiceError("finalize_failed") from None
        return await self._get_job(job.job_id)

    async def _finish_finalization_with_profile_lease(
        self,
        job: OnboardingJob,
        temp_path: Path,
        migration: Optional[_ProfileMigration],
        initial_verified: VerifiedAccountSnapshot,
        token: Token,
    ) -> OnboardingJob:
        if migration is None:
            try:
                destination, archive, conflict_status = self._preflight_destination(
                    job, token.id, temp_path
                )
                migration = self._migrate_profile(
                    job.job_id,
                    temp_path,
                    destination,
                    archive,
                    conflict_status,
                )
            except OnboardingServiceError as error:
                if error.code == "destination_conflict":
                    try:
                        await self.db.update_onboarding_job(
                            job.job_id,
                            conflict_status="rejected",
                            profile_ready=False,
                        )
                    except Exception:
                        raise OnboardingServiceError("finalize_failed") from None
                await self._fail_job(job, error.code, "migrate_profile")
                raise
            try:
                await self.db.update_onboarding_job(
                    job.job_id,
                    conflict_status=None,
                    profile_ready=False,
                )
                await self.db.update_onboarding_job_state(
                    job.job_id,
                    "running",
                    phase="validating_destination",
                    clear_error=True,
                )
            except Exception:
                raise OnboardingServiceError("finalize_failed") from None
            job = await self._get_job(job.job_id)

        forward_only = job.phase == "account_commit"
        try:
            final_session = self._read_profile_session(migration.destination)
            final_verified = await self._inspect_session(final_session)
            if final_verified.normalized_email != initial_verified.normalized_email:
                raise OnboardingServiceError("final_validation_failed")
            try:
                final_match = await self.token_manager.find_token_by_email(
                    final_verified.email
                )
            except ValueError:
                raise OnboardingServiceError("duplicate_email") from None
            if final_match is None or final_match.id != token.id:
                raise OnboardingServiceError("final_validation_failed")
        except OnboardingServiceError as error:
            if not forward_only:
                self._rollback_migration(migration)
                try:
                    await self.db.update_onboarding_job(
                        job.job_id,
                        conflict_status=None,
                        profile_ready=False,
                    )
                except Exception:
                    raise OnboardingServiceError("finalize_failed") from None
            await self._fail_job(
                job,
                error.code,
                "account_commit" if forward_only else "final_validation",
            )
            raise

        try:
            await self.db.update_onboarding_job(
                job.job_id,
                conflict_status=migration.conflict_status,
                profile_ready=True,
            )
            if not forward_only:
                await self.db.update_onboarding_job_state(
                    job.job_id,
                    "running",
                    phase="account_commit",
                    clear_error=True,
                )
        except Exception:
            raise OnboardingServiceError("finalize_failed") from None
        job = await self._get_job(job.job_id)

        try:
            project_count = await self._apply_final_account_state(
                job,
                token,
                final_verified,
            )
            await self.db.update_onboarding_job(
                job.job_id,
                discovered_email=final_verified.email,
                discovered_tier=final_verified.user_paygate_tier,
                discovered_credits=final_verified.credits,
                discovered_at_expires=final_verified.at_expires,
                project_count=project_count,
                profile_ready=True,
                conflict_status=migration.conflict_status,
            )
            await self.db.update_onboarding_job_state(
                job.job_id, "running", phase="commit_complete", clear_error=True
            )
        except OnboardingServiceError as error:
            await self._fail_job(job, error.code, "account_commit")
            raise
        except Exception:
            await self._fail_job(job, "finalize_failed", "account_commit")
            raise OnboardingServiceError("finalize_failed") from None

        return await self._complete_committed_job(
            await self._get_job(job.job_id)
        )

    @_serialize_onboarding_operation
    async def finalize(self, job_id: str) -> OnboardingJob:
        async with self._operation_lock:
            job = await self._get_job(job_id)
            if job.state == "completed":
                return job
            if job.state == "cancelled" or job.state not in {"running", "failed"}:
                raise OnboardingServiceError("invalid_job_state")
            if job.phase == "commit_complete":
                return await self._complete_committed_job(job)

            temp_path = self._temp_path(job.job_id)
            try:
                if not self._is_migration_blocker(temp_path, job.job_id):
                    await self._stop_and_prepare_onboarding_profile(job, temp_path)
            except OnboardingServiceError as error:
                await self._fail_job(job, error.code, "stop_browser")
                raise
            await self.sleep(0.2)

            migration = None
            lease = None
            token = None
            try:
                migration_blocker = self._is_migration_blocker(
                    temp_path, job.job_id
                )
                if not migration_blocker and temp_path.exists():
                    profile_path = temp_path
                else:
                    if job.resolved_token_id is None:
                        error = OnboardingServiceError("profile_not_found")
                        await self._fail_job(job, error.code, "migrate_profile")
                        raise error
                    token = await self.db.get_token(job.resolved_token_id)
                    if token is None:
                        error = OnboardingServiceError("target_not_found")
                        await self._fail_job(job, error.code, "verify_account")
                        raise error
                    try:
                        lease = await self._acquire_token_profile_lease(token.id)
                        migration = self._resumed_profile_migration(job, temp_path)
                    except OnboardingServiceError as error:
                        await self._fail_job(job, error.code, "migrate_profile")
                        raise
                    if migration is None:
                        error = OnboardingServiceError("profile_not_found")
                        await self._fail_job(job, error.code, "migrate_profile")
                        raise error
                    profile_path = migration.destination

                try:
                    initial_session = self._read_profile_session(profile_path)
                    initial_verified = await self._inspect_session(initial_session)
                    resolved_token = await self._resolve_account(job, initial_verified)
                    if token is not None and resolved_token.id != token.id:
                        raise OnboardingServiceError("final_validation_failed")
                    token = resolved_token
                    if (
                        job.resolved_token_id is not None
                        and token.id != job.resolved_token_id
                    ):
                        raise OnboardingServiceError("final_validation_failed")
                except OnboardingServiceError as error:
                    await self._fail_job(job, error.code, "verify_account")
                    raise

                try:
                    await self.db.update_onboarding_job(
                        job.job_id,
                        resolved_token_id=token.id,
                        discovered_email=initial_verified.email,
                        discovered_tier=initial_verified.user_paygate_tier,
                        discovered_credits=initial_verified.credits,
                        discovered_at_expires=initial_verified.at_expires,
                    )
                except Exception:
                    raise OnboardingServiceError("finalize_failed") from None
                job = await self._get_job(job.job_id)

                if lease is None:
                    try:
                        lease = await self._acquire_token_profile_lease(token.id)
                        migration = self._resumed_profile_migration(job, temp_path)
                    except OnboardingServiceError as error:
                        await self._fail_job(job, error.code, "migrate_profile")
                        raise

                return await self._finish_finalization_with_profile_lease(
                    job,
                    temp_path,
                    migration,
                    initial_verified,
                    token,
                )
            finally:
                if lease is not None:
                    try:
                        lease.release()
                    except Exception:
                        pass

    async def _cancel_with_operation_lease(
        self,
        job: OnboardingJob,
    ) -> OnboardingJob:
        temp_path = self._temp_path(job.job_id)
        lease = await self._acquire_onboarding_operation_lease(
            job.job_id,
            temp_path,
        )
        try:
            current = await self._get_job(job.job_id)
            return await self._cancel_locked(current)
        finally:
            try:
                release_result = lease.release()
                if inspect.isawaitable(release_result):
                    await release_result
            except Exception:
                pass

    async def _cancel_locked(self, job: OnboardingJob) -> OnboardingJob:
        if job.state == "cancelled" or job.state == "completed":
            return job
        if job.phase == "commit_complete":
            return await self._complete_committed_job(job)
        if job.phase == "account_commit":
            raise OnboardingServiceError("invalid_job_state")
        temp_path = self._temp_path(job.job_id)
        lease = None
        try:
            if self._is_migration_blocker(temp_path, job.job_id):
                if job.resolved_token_id is None:
                    raise OnboardingServiceError("profile_not_found")
                lease = await self._acquire_token_profile_lease(
                    job.resolved_token_id
                )
                migration = self._resumed_profile_migration(job, temp_path)
                if migration is None:
                    raise OnboardingServiceError("profile_not_found")
                self._rollback_migration(migration)
            if not self._is_cleanup_blocker(temp_path, job.job_id):
                await self._stop_owned_browser(job, temp_path)
            self._remove_service_temp(job.job_id)
        except OnboardingServiceError as error:
            await self._fail_job(job, error.code, "cancel")
            raise
        finally:
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    pass
        current = await self._get_job(job.job_id)
        if current.state in {"cancelled", "completed"}:
            return current
        transitioned = await self._transition_job_state(
            current,
            state="cancelled",
            phase="cancelled",
        )
        if not transitioned:
            latest = await self._get_job(job.job_id)
            if latest.state in {"cancelled", "completed"}:
                return latest
            raise OnboardingServiceError("active_job_exists")
        return await self._get_job(job.job_id)

    async def cancel(self, job_id: str) -> OnboardingJob:
        async with self._operation_lock:
            return await self._cancel_with_operation_lease(
                await self._get_job(job_id)
            )

    async def _recover_awaiting_login_browser(
        self,
        job: OnboardingJob,
    ) -> OnboardingJob:
        temp_path = self._temp_path(job.job_id)
        try:
            owner = await self._resolve_owned_browser(job, temp_path)
            if owner is not None:
                return await self._get_job(job.job_id)
            if job.browser_pid is not None or job.browser_start_ticks is not None:
                successor = await self._clear_stopped_browser_identity(
                    job,
                    temp_path,
                )
                if successor is not None:
                    return await self._get_job(job.job_id)
            failure_code = "process_identity_unavailable"
        except OnboardingServiceError as error:
            failure_code = error.code
            if error.code == "process_ownership_mismatch":
                try:
                    successor = await self._clear_identity_with_stale_lock_proof(
                        job,
                        temp_path,
                    )
                    if successor is not None:
                        return await self._get_job(job.job_id)
                except OnboardingServiceError as proof_error:
                    failure_code = proof_error.code

        await self._fail_job(job, failure_code, "recovery")
        return await self._get_job(job.job_id)

    async def _recover_incomplete_job(
        self,
        job: OnboardingJob,
    ) -> Optional[OnboardingJob]:
        temp_path = self._temp_path(job.job_id)
        try:
            cleanup_staging_exists = self._cleanup_blocker_staging_path(
                job.job_id
            ).exists()
        except OnboardingServiceError as error:
            await self._fail_job(job, error.code, "recovery")
            return await self._get_job(job.job_id)

        if self._is_cleanup_blocker(temp_path, job.job_id) or cleanup_staging_exists:
            try:
                return await self._cancel_locked(job)
            except OnboardingServiceError:
                return await self._get_job(job.job_id)
        if job.phase == "commit_complete":
            try:
                return await self._complete_committed_job(job)
            except OnboardingServiceError:
                return await self._get_job(job.job_id)
        if self._is_migration_blocker(temp_path, job.job_id):
            return job

        if job.state == "failed":
            browser_live = None
            try:
                browser_live = await self._reconcile_failed_browser_job(job)
            except OnboardingServiceError:
                pass
            job = await self._get_job(job.job_id)
            if self._failed_job_can_resume_login(job):
                return job
            if (
                browser_live is False
                and job.phase in _RECOVERABLE_BROWSER_FAILURE_PHASES
            ):
                try:
                    return await self._cancel_locked(job)
                except OnboardingServiceError:
                    return await self._get_job(job.job_id)

        if self._is_expired(job):
            if job.phase in {"validating_destination", "account_commit"}:
                return job
            try:
                return await self._cancel_locked(job)
            except OnboardingServiceError:
                return await self._get_job(job.job_id)
        if job.state == "running" and job.phase == "browser_start":
            try:
                return await self._recover_launch_ownership(job, temp_path)
            except Exception:
                await self._fail_job(
                    job,
                    "process_identity_unavailable",
                    "browser_start",
                )
                return await self._get_job(job.job_id)
        if job.state == "running" and job.phase == "awaiting_login":
            try:
                return await self._recover_awaiting_login_browser(job)
            except OnboardingServiceError as error:
                await self._fail_job(job, error.code, "recovery")
                return await self._get_job(job.job_id)
        return job

    async def recover_incomplete(self) -> list[OnboardingJob]:
        async with self._operation_lock:
            recovered: list[OnboardingJob] = []
            for listed_job in await self.db.list_onboarding_jobs():
                if listed_job.state in _TERMINAL_STATES:
                    continue
                temp_path = self._temp_path(listed_job.job_id)
                try:
                    lease = await self._acquire_onboarding_operation_lease(
                        listed_job.job_id,
                        temp_path,
                    )
                except OnboardingServiceError as error:
                    current = await self._get_job(listed_job.job_id)
                    if error.code != "active_job_exists":
                        await self._fail_job(current, error.code, "recovery")
                        current = await self._get_job(listed_job.job_id)
                    if current.state not in _TERMINAL_STATES:
                        recovered.append(current)
                    continue

                try:
                    current = await self._get_job(listed_job.job_id)
                    if current.state in _TERMINAL_STATES:
                        continue
                    result = await self._recover_incomplete_job(current)
                    if result is not None:
                        recovered.append(result)
                finally:
                    try:
                        release_result = lease.release()
                        if inspect.isawaitable(release_result):
                            await release_result
                    except Exception:
                        pass
            return [self._safe_job(job) for job in recovered]
