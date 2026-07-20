"""Safe ownership, cleanup, and cookie access for Chrome keepalive profiles.

Chrome's ``SingletonLock`` is normally a dangling symlink whose target is
``hostname-PID``.  A dangling filesystem target does not prove that Chrome is
stopped, so cleanup is permitted only after inspecting that exact PID and its
canonical ``--user-data-dir`` command-line argument.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import socket
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence
from urllib.parse import urlsplit

SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"
SESSION_COOKIE_DOMAIN = "labs.google"
MIN_SESSION_TOKEN_LENGTH = 100
_SERVICE_LOCK_DIRECTORY = ".flow2api-locks"
_SINGLETON_ARTIFACTS = ("SingletonCookie", "SingletonSocket", "SingletonLock")
_TOKEN_ID_PATTERN = re.compile(r"[1-9][0-9]*\Z")
_LEASE_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")

CmdlineReader = Callable[[int], Optional[Sequence[str]]]
StartTicksReader = Callable[[int], Optional[int]]
CookieReader = Callable[..., Iterable[object]]


class ProfileLeaseBusyError(RuntimeError):
    """Raised when another service process already owns a profile lease."""

    def __init__(self, profile_path: Path):
        self.profile_path = profile_path
        super().__init__(f"keepalive profile lease is busy: {profile_path}")


class ProfileBusyError(RuntimeError):
    """Raised when the profile belongs to a currently running Chrome process."""

    def __init__(self, profile_path: Path, pid: int):
        self.profile_path = profile_path
        self.pid = pid
        super().__init__(f"Chrome profile is busy: {profile_path} (pid={pid})")


class ProfileLockUncertainError(RuntimeError):
    """Raised when a singleton lock cannot safely be classified or cleaned."""

    def __init__(self, profile_path: Path, reason: str):
        self.profile_path = profile_path
        self.reason = reason
        super().__init__(
            f"Chrome profile lock ownership is uncertain: {profile_path} ({reason})"
        )


class SessionTokenNotFoundError(RuntimeError):
    """Raised when the Labs session cookie is absent."""


class SessionTokenTooShortError(RuntimeError):
    """Raised when all matching session cookies fail the minimum length guard."""


class SingletonLockState(str, Enum):
    """Safety classification for a Chrome SingletonLock."""

    ABSENT = "absent"
    BUSY = "busy"
    STALE = "stale"
    UNSAFE = "unsafe"


@dataclass(frozen=True)
class ProcessSnapshot:
    """PID identity captured from procfs without retaining sensitive data."""

    pid: int
    start_ticks: int
    cmdline: tuple[str, ...]


@dataclass(frozen=True)
class SingletonLockInspection:
    """Read-only result of inspecting a Chrome singleton lock."""

    state: SingletonLockState
    profile_path: Path
    reason: str
    pid: Optional[int] = None
    hostname: Optional[str] = None
    link_target: Optional[str] = None
    lock_device: Optional[int] = None
    lock_inode: Optional[int] = None

    @property
    def busy(self) -> bool:
        return self.state is SingletonLockState.BUSY


@dataclass(frozen=True)
class ProfileCleanupReport:
    """Artifacts removed after a lock was proven stale."""

    inspection: SingletonLockInspection
    removed: tuple[str, ...]


@dataclass
class ProfileLease:
    """An exclusive nonblocking service-owned flock for one profile."""

    profile_path: Path
    lock_path: Path
    _file_descriptor: Optional[int]

    @property
    def active(self) -> bool:
        return self._file_descriptor is not None

    def release(self) -> None:
        file_descriptor = self._file_descriptor
        if file_descriptor is None:
            return
        self._file_descriptor = None
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(file_descriptor)

    def __enter__(self) -> "ProfileLease":
        if not self.active:
            raise RuntimeError("profile lease has already been released")
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()


def _validated_token_id(token_id: object) -> str:
    if isinstance(token_id, bool):
        raise TypeError("token ID must be a positive integer")
    if isinstance(token_id, int):
        if token_id <= 0:
            raise ValueError("token ID must be a positive integer")
        return str(token_id)
    if isinstance(token_id, str):
        if not _TOKEN_ID_PATTERN.fullmatch(token_id):
            raise ValueError("token ID must use canonical positive decimal form")
        return token_id
    raise TypeError("token ID must be an integer or canonical decimal string")


def _canonical_base_path(base_dir: os.PathLike[str] | str) -> Path:
    if isinstance(base_dir, str) and not base_dir.strip():
        raise ValueError("profile base directory cannot be empty")
    return Path(base_dir).expanduser().resolve(strict=False)


def _require_within_base(candidate: Path, base_path: Path) -> None:
    try:
        candidate.relative_to(base_path)
    except ValueError as error:
        raise ValueError(
            f"profile path resolves outside configured base: {candidate}"
        ) from error


def validate_proxy_server(proxy: object) -> Optional[str]:
    """Return a credential-free Chrome proxy value or reject it safely."""

    value = str(proxy or "").strip()
    if not value:
        return None
    if any(character in value for character in ("\0", "\n", "\r")):
        raise ValueError("proxy URL contains invalid control characters")
    if "@" in value:
        raise ValueError("proxy URL must not include userinfo")
    parse_target = value if "://" in value else f"//{value}"
    parsed = urlsplit(parse_target)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("proxy URL must not include userinfo")
    return value


def canonical_profile_path(
    base_dir: os.PathLike[str] | str,
    token_id: object,
) -> Path:
    """Return a canonical per-token path that cannot escape ``base_dir``."""

    base_path = _canonical_base_path(base_dir)
    token_component = _validated_token_id(token_id)
    profile_path = (base_path / token_component).resolve(strict=False)
    _require_within_base(profile_path, base_path)
    return profile_path


def _validated_lease_key(lease_key: object) -> str:
    if not isinstance(lease_key, str) or not _LEASE_KEY_PATTERN.fullmatch(lease_key):
        raise ValueError("profile lease key must use safe canonical characters")
    return lease_key


def acquire_profile_path_lease(
    base_dir: os.PathLike[str] | str,
    profile_path: os.PathLike[str] | str,
    lease_key: str,
) -> ProfileLease:
    """Acquire a service-owned flock for one validated path below ``base_dir``."""

    base_path = _canonical_base_path(base_dir)
    base_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    canonical_profile = Path(profile_path).expanduser().resolve(strict=False)
    _require_within_base(canonical_profile, base_path)
    canonical_lease_key = _validated_lease_key(lease_key)

    lock_directory = base_path / _SERVICE_LOCK_DIRECTORY
    lock_directory.mkdir(mode=0o700, exist_ok=True)
    canonical_lock_directory = lock_directory.resolve(strict=True)
    _require_within_base(canonical_lock_directory, base_path)
    lock_path = canonical_lock_directory / f"{canonical_lease_key}.lock"

    open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(lock_path, open_flags, 0o600)
    try:
        os.fchmod(file_descriptor, 0o600)
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(file_descriptor)
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise ProfileLeaseBusyError(canonical_profile) from error
        raise
    return ProfileLease(
        profile_path=canonical_profile,
        lock_path=lock_path,
        _file_descriptor=file_descriptor,
    )


def acquire_profile_lease(
    base_dir: os.PathLike[str] | str,
    token_id: object,
) -> ProfileLease:
    """Acquire a service-owned, cross-process, nonblocking profile flock."""

    base_path = _canonical_base_path(base_dir)
    token_component = _validated_token_id(token_id)
    return acquire_profile_path_lease(
        base_path,
        canonical_profile_path(base_path, token_component),
        token_component,
    )


def _validated_pid(pid: object) -> int:
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise TypeError("PID must be a positive integer")
    if pid <= 0:
        raise ValueError("PID must be a positive integer")
    return pid


def read_proc_cmdline(
    pid: int,
    *,
    proc_root: os.PathLike[str] | str = "/proc",
) -> Optional[tuple[str, ...]]:
    """Read one process command line from procfs, preserving argument boundaries."""

    process_id = _validated_pid(pid)
    path = Path(proc_root) / str(process_id) / "cmdline"
    try:
        raw_cmdline = path.read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    if not raw_cmdline:
        return ()
    return tuple(
        os.fsdecode(argument)
        for argument in raw_cmdline.split(b"\0")
        if argument
    )


def read_proc_start_ticks(
    pid: int,
    *,
    proc_root: os.PathLike[str] | str = "/proc",
) -> Optional[int]:
    """Read Linux procfs field 22 (process start time in clock ticks)."""

    process_id = _validated_pid(pid)
    path = Path(proc_root) / str(process_id) / "stat"
    try:
        raw_stat = path.read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None

    command_end = raw_stat.rfind(b")")
    if command_end < 0 or command_end + 2 > len(raw_stat):
        raise ValueError(f"invalid proc stat format for pid {process_id}")
    fields_from_state = raw_stat[command_end + 2 :].split()
    start_ticks_index = 22 - 3
    if len(fields_from_state) <= start_ticks_index:
        raise ValueError(f"proc stat is missing start ticks for pid {process_id}")
    try:
        return int(fields_from_state[start_ticks_index])
    except ValueError as error:
        raise ValueError(f"invalid proc start ticks for pid {process_id}") from error


def _canonical_cmdline_profile(raw_path: object) -> Optional[Path]:
    value = str(raw_path or "")
    if not value or any(character in value for character in ("\0", "\n", "\r")):
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _cmdline_profile_ownership(
    cmdline: Sequence[str],
    profile_path: Path,
) -> Optional[bool]:
    """Return true/false ownership, or None when an argument is ambiguous."""

    canonical_profile = profile_path.resolve(strict=False)
    candidates: list[str] = []
    for index, argument in enumerate(cmdline):
        if argument.startswith("--user-data-dir="):
            candidates.append(argument.split("=", 1)[1])
        elif argument == "--user-data-dir":
            if index + 1 >= len(cmdline):
                return None
            candidates.append(cmdline[index + 1])
    if not candidates:
        return False

    uncertain = False
    for candidate in candidates:
        canonical_candidate = _canonical_cmdline_profile(candidate)
        if canonical_candidate is None:
            uncertain = True
        elif canonical_candidate == canonical_profile:
            return True
    return None if uncertain else False


def _cmdline_owns_profile(cmdline: Sequence[str], profile_path: Path) -> bool:
    return _cmdline_profile_ownership(cmdline, profile_path) is True


def verify_process_ownership(
    pid: int,
    profile_path: os.PathLike[str] | str,
    *,
    expected_start_ticks: Optional[int] = None,
    cmdline_reader: CmdlineReader = read_proc_cmdline,
    start_ticks_reader: StartTicksReader = read_proc_start_ticks,
) -> bool:
    """Verify an exact PID identity and canonical profile command-line argument."""

    process_id = _validated_pid(pid)
    canonical_profile = Path(profile_path).expanduser().resolve(strict=False)
    if expected_start_ticks is not None:
        if isinstance(expected_start_ticks, bool) or expected_start_ticks < 0:
            raise ValueError("expected start ticks must be a nonnegative integer")
        observed_start_ticks = start_ticks_reader(process_id)
        if observed_start_ticks != expected_start_ticks:
            return False
    cmdline = cmdline_reader(process_id)
    if cmdline is None:
        return False
    return _cmdline_owns_profile(cmdline, canonical_profile)


def read_process_snapshot(pid: int) -> Optional[ProcessSnapshot]:
    """Capture a PID snapshot while detecting reuse during the procfs reads."""

    process_id = _validated_pid(pid)
    initial_start_ticks = read_proc_start_ticks(process_id)
    if initial_start_ticks is None:
        return None
    cmdline = read_proc_cmdline(process_id)
    if cmdline is None:
        return None
    final_start_ticks = read_proc_start_ticks(process_id)
    if final_start_ticks is None or final_start_ticks != initial_start_ticks:
        return None
    return ProcessSnapshot(
        pid=process_id,
        start_ticks=initial_start_ticks,
        cmdline=tuple(cmdline),
    )


def _parse_singleton_target(target: str) -> Optional[tuple[str, int]]:
    hostname, separator, raw_pid = target.rpartition("-")
    if not separator or not hostname or not raw_pid.isascii() or not raw_pid.isdigit():
        return None
    pid = int(raw_pid)
    if pid <= 0:
        return None
    return hostname, pid


def _inspection_for_lock(
    state: SingletonLockState,
    profile_path: Path,
    reason: str,
    lock_stat: os.stat_result,
    *,
    link_target: Optional[str] = None,
    hostname: Optional[str] = None,
    pid: Optional[int] = None,
) -> SingletonLockInspection:
    return SingletonLockInspection(
        state=state,
        profile_path=profile_path,
        reason=reason,
        pid=pid,
        hostname=hostname,
        link_target=link_target,
        lock_device=lock_stat.st_dev,
        lock_inode=lock_stat.st_ino,
    )


def _classify_singleton_owner(
    profile_path: Path,
    lock_stat: os.stat_result,
    link_target: str,
    hostname: str,
    pid: int,
    process_reader: Callable[[int], Optional[ProcessSnapshot]],
    local_hostname: str,
) -> SingletonLockInspection:
    def inspection(
        state: SingletonLockState,
        reason: str,
    ) -> SingletonLockInspection:
        return _inspection_for_lock(
            state,
            profile_path,
            reason,
            lock_stat,
            link_target=link_target,
            hostname=hostname,
            pid=pid,
        )
    if hostname != local_hostname:
        return inspection(SingletonLockState.UNSAFE, "lock_belongs_to_foreign_host")
    try:
        process = process_reader(pid)
    except (OSError, ValueError):
        return inspection(SingletonLockState.UNSAFE, "process_inspection_failed")
    if process is None:
        return inspection(SingletonLockState.STALE, "pid_not_running")
    if process.pid != pid:
        return inspection(SingletonLockState.UNSAFE, "process_reader_pid_mismatch")
    ownership = _cmdline_profile_ownership(process.cmdline, profile_path)
    if ownership is True:
        return inspection(SingletonLockState.BUSY, "profile_owned_by_live_pid")
    if ownership is None:
        return inspection(SingletonLockState.UNSAFE, "profile_argument_is_ambiguous")
    return inspection(SingletonLockState.STALE, "pid_not_owned_by_profile")


def inspect_singleton_lock(
    profile_path: os.PathLike[str] | str,
    *,
    process_reader: Callable[[int], Optional[ProcessSnapshot]] = read_process_snapshot,
    local_hostname: Optional[str] = None,
) -> SingletonLockInspection:
    """Classify ``SingletonLock`` without treating a dangling symlink as stale."""
    canonical_profile = Path(profile_path).expanduser().resolve(strict=False)
    lock_path = canonical_profile / "SingletonLock"
    try:
        lock_stat = lock_path.lstat()
    except FileNotFoundError:
        return SingletonLockInspection(
            SingletonLockState.ABSENT, canonical_profile, "lock_absent"
        )
    if not lock_path.is_symlink():
        return _inspection_for_lock(
            SingletonLockState.UNSAFE,
            canonical_profile,
            "lock_is_not_symlink",
            lock_stat,
        )
    try:
        link_target = os.readlink(lock_path)
    except OSError:
        return _inspection_for_lock(
            SingletonLockState.UNSAFE,
            canonical_profile,
            "lock_target_unreadable",
            lock_stat,
        )
    parsed_target = _parse_singleton_target(link_target)
    if parsed_target is None:
        return _inspection_for_lock(
            SingletonLockState.UNSAFE,
            canonical_profile,
            "lock_target_malformed",
            lock_stat,
            link_target=link_target,
        )
    lock_hostname, pid = parsed_target
    return _classify_singleton_owner(
        canonical_profile,
        lock_stat,
        link_target,
        lock_hostname,
        pid,
        process_reader,
        local_hostname or socket.gethostname(),
    )


def _lock_matches_inspection(inspection: SingletonLockInspection) -> bool:
    lock_path = inspection.profile_path / "SingletonLock"
    try:
        current_stat = lock_path.lstat()
        current_target = os.readlink(lock_path)
    except (FileNotFoundError, OSError):
        return False
    return (
        current_stat.st_dev == inspection.lock_device
        and current_stat.st_ino == inspection.lock_inode
        and current_target == inspection.link_target
    )


def _remove_stale_artifacts(
    inspection: SingletonLockInspection,
) -> tuple[str, ...]:
    if inspection.state is not SingletonLockState.STALE:
        raise ValueError("singleton artifacts may be removed only for a stale lock")

    removed: list[str] = []
    for artifact_name in _SINGLETON_ARTIFACTS:
        if not _lock_matches_inspection(inspection):
            raise ProfileLockUncertainError(
                inspection.profile_path,
                "singleton lock changed during cleanup",
            )
        artifact_path = inspection.profile_path / artifact_name
        try:
            artifact_path.lstat()
        except FileNotFoundError:
            continue
        artifact_path.unlink()
        removed.append(artifact_name)
    return tuple(removed)


def prepare_profile(
    lease: ProfileLease,
    *,
    process_reader: Callable[[int], Optional[ProcessSnapshot]] = read_process_snapshot,
    local_hostname: Optional[str] = None,
) -> ProfileCleanupReport:
    """Report busy profiles and remove singleton artifacts only if proven stale."""

    if not isinstance(lease, ProfileLease) or not lease.active:
        raise ValueError("an active service-owned profile lease is required")
    inspection = inspect_singleton_lock(
        lease.profile_path,
        process_reader=process_reader,
        local_hostname=local_hostname,
    )
    if inspection.state is SingletonLockState.BUSY:
        if inspection.pid is None:
            raise ProfileLockUncertainError(
                lease.profile_path,
                "busy lock has no PID",
            )
        raise ProfileBusyError(lease.profile_path, inspection.pid)
    if inspection.state is SingletonLockState.UNSAFE:
        raise ProfileLockUncertainError(lease.profile_path, inspection.reason)
    if inspection.state is SingletonLockState.ABSENT:
        return ProfileCleanupReport(inspection=inspection, removed=())
    return ProfileCleanupReport(
        inspection=inspection,
        removed=_remove_stale_artifacts(inspection),
    )


def _default_cookie_reader(**kwargs) -> Iterable[object]:
    import browser_cookie3

    return browser_cookie3.chrome(**kwargs)


def _cookie_expiry(cookie: object) -> float:
    raw_expiry = getattr(cookie, "expires", None)
    try:
        return float(raw_expiry)
    except (TypeError, ValueError):
        return float("-inf")


def _cookie_selection_key(cookie: object) -> tuple[object, ...]:
    domain = str(getattr(cookie, "domain", "")).lower().lstrip(".")
    path = str(getattr(cookie, "path", "") or "")
    value = str(getattr(cookie, "value", ""))
    return (
        domain == SESSION_COOKIE_DOMAIN,
        len(path),
        _cookie_expiry(cookie),
        bool(getattr(cookie, "secure", False)),
        domain,
        path,
        value,
    )


def _chrome_cookie_files(profile_path: Path) -> tuple[Path, ...]:
    """Return Chrome cookie stores with the last-used profile first."""
    candidates = []
    local_state = profile_path / "Local State"
    try:
        payload = json.loads(local_state.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
    profile_state = payload.get("profile") if isinstance(payload, dict) else None
    last_used = profile_state.get("last_used") if isinstance(profile_state, dict) else None
    if isinstance(last_used, str) and last_used in {"Default"} | {
        entry.name
        for entry in profile_path.iterdir()
        if entry.is_dir() and entry.name.startswith("Profile ")
    }:
        candidates.append(profile_path / last_used / "Cookies")
    default_cookie_file = profile_path / "Default" / "Cookies"
    if default_cookie_file not in candidates:
        candidates.append(default_cookie_file)
    return tuple(candidates)


def read_session_token(
    profile_path: os.PathLike[str] | str,
    *,
    cookie_reader: Optional[CookieReader] = None,
    minimum_length: int = MIN_SESSION_TOKEN_LENGTH,
) -> str:
    """Read the deterministic valid Labs ST without logging credential contents."""

    if isinstance(minimum_length, bool) or not isinstance(minimum_length, int):
        raise TypeError("minimum token length must be an integer")
    if minimum_length <= 0:
        raise ValueError("minimum token length must be positive")

    canonical_profile = Path(profile_path).expanduser().resolve(strict=False)
    reader = cookie_reader or _default_cookie_reader
    matching_cookie_found = False
    for cookie_file in _chrome_cookie_files(canonical_profile):
        cookies = reader(
            cookie_file=str(cookie_file),
            domain_name=SESSION_COOKIE_DOMAIN,
        )
        matching_cookies = [
            cookie
            for cookie in cookies
            if getattr(cookie, "name", None) == SESSION_COOKIE_NAME
            and str(getattr(cookie, "domain", "")).lower().lstrip(".")
            == SESSION_COOKIE_DOMAIN
        ]
        if not matching_cookies:
            continue
        matching_cookie_found = True
        valid_cookies = []
        for cookie in matching_cookies:
            value = getattr(cookie, "value", None)
            if isinstance(value, str) and len(value.encode("utf-8")) >= minimum_length:
                valid_cookies.append(cookie)
        if valid_cookies:
            return max(valid_cookies, key=_cookie_selection_key).value

    if matching_cookie_found:
        raise SessionTokenTooShortError(
            f"Labs session token is shorter than {minimum_length} bytes"
        )
    raise SessionTokenNotFoundError("Labs session token cookie was not found")
