"""Browser-backed keepalive refresh with identity-safe atomic persistence."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import quote

from ..flow.errors import is_retryable_network_error, is_timeout_error
from ..tokens.account_identity import VerifiedAccountSnapshot, normalize_account_email
from .models import FailureCode, RefreshOutcome
from .profile import (
    MIN_SESSION_TOKEN_LENGTH,
    ProfileBusyError,
    ProfileLeaseBusyError,
    ProfileLockUncertainError,
    SessionTokenNotFoundError,
    SessionTokenTooShortError,
    read_session_token,
    validate_proxy_server,
)

if TYPE_CHECKING:
    from ...core.database import Database
    from ...core.models import KeepaliveToken
    from ..flow_client import FlowClient


FLOW_TOOL_BASE = "https://labs.google/fx/tools/flow"
SESSION_URL = "https://labs.google/fx/api/auth/session"
DEFAULT_READY_TIMEOUT_SECONDS = 45.0
DEFAULT_READY_POLL_SECONDS = 1.0
DEFAULT_SESSION_SETTLE_SECONDS = 3.0
DEFAULT_LAUNCH_TIMEOUT_SECONDS = 30.0
DEFAULT_STOP_TIMEOUT_SECONDS = 10.0

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]
ErrorClassifier = Callable[[BaseException], bool]
CookieReader = Callable[[Path], str]


def _load_nodriver():
    """Import nodriver only when a keepalive browser is actually launched."""

    return importlib.import_module("nodriver")


def _load_runtime_patcher():
    from ..captcha.nodriver_patches import _patch_nodriver_runtime

    return _patch_nodriver_runtime


def _positive_timeout(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def _nonnegative_delay(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return result


def _browser_arguments(proxy: Optional[str]) -> list[str]:
    arguments = [
        "--disable-quic",
        "--disable-features=UseDnsHttpsSvcb",
        "--disable-dev-shm-usage",
        "--disable-setuid-sandbox",
        "--disable-gpu",
        "--disable-infobars",
        "--window-size=1280,720",
        "--window-position=3000,3000",
        "--profile-directory=Default",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--disable-default-apps",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    proxy_value = validate_proxy_server(proxy)
    if proxy_value is not None:
        arguments.append(f"--proxy-server={proxy_value}")
    return arguments


async def launch_keepalive_browser(
    profile: Path | str,
    proxy: Optional[str],
    display: Optional[str],
    executable: Path | str,
    headless: bool = False,
    *,
    launch_timeout_seconds: float = DEFAULT_LAUNCH_TIMEOUT_SECONDS,
):
    """Launch nodriver with its loopback, dynamically allocated control endpoint."""

    profile_path = Path(profile).expanduser().resolve(strict=False)
    executable_path = Path(executable).expanduser().resolve(strict=False)
    if not profile_path.is_dir():
        raise FileNotFoundError("keepalive browser profile is missing")
    if not executable_path.is_file():
        raise FileNotFoundError("keepalive browser executable is missing")
    if not isinstance(headless, bool):
        raise TypeError("headless must be a bool")
    timeout = _positive_timeout(launch_timeout_seconds, "launch timeout")
    if display:
        os.environ["DISPLAY"] = str(display)

    browser_args = _browser_arguments(proxy)
    nodriver = _load_nodriver()
    launch_kwargs = {
        "headless": headless,
        "user_data_dir": str(profile_path),
        "browser_executable_path": str(executable_path),
        "browser_args": browser_args,
        "sandbox": False,
    }
    # Do not pass host/port or remote-debugging arguments. nodriver then launches a
    # new browser and assigns 127.0.0.1 plus a free ephemeral control port itself.
    try:
        browser = await asyncio.wait_for(
            nodriver.start(**launch_kwargs), timeout=timeout
        )
    except Exception as error:
        error_text = str(error).casefold()
        if "no_sandbox" not in error_text and "root" not in error_text:
            raise
        fallback_args = list(browser_args)
        if "--no-sandbox" not in fallback_args:
            fallback_args.append("--no-sandbox")
        fallback_kwargs = dict(launch_kwargs)
        fallback_kwargs["browser_args"] = fallback_args
        fallback_kwargs["sandbox"] = True
        browser = await asyncio.wait_for(
            nodriver.start(**fallback_kwargs), timeout=timeout
        )

    try:
        _load_runtime_patcher()(browser)
    except Exception:
        pass
    return browser


async def safe_stop_browser(
    browser: object | None,
    *,
    timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS,
) -> bool:
    """Stop a browser regardless of whether its ``stop`` method is sync or async."""

    if browser is None:
        return True
    try:
        result = browser.stop()
        if inspect.isawaitable(result):
            await asyncio.wait_for(
                result,
                timeout=_positive_timeout(timeout_seconds, "stop timeout"),
            )
        return True
    except Exception:
        return False


def _default_network_error_classifier(error: BaseException) -> bool:
    message = str(error)
    if is_timeout_error(error) or is_retryable_network_error(message):
        return True
    normalized = message.casefold()
    return any(
        marker in normalized
        for marker in (
            "proxy error",
            "proxy connect",
            "could not resolve",
            "name or service not known",
            "temporary failure in name resolution",
            "connection unreachable",
        )
    )


def _default_browser_error_classifier(_error: BaseException) -> bool:
    return True


def _is_unauthenticated(error: BaseException) -> bool:
    message = str(error).casefold()
    return any(
        marker in message
        for marker in ("401", "unauthenticated", "invalid_token", "invalid token")
    )


def _failure_detail(operation: str, error: BaseException) -> str:
    return f"{operation} failed ({type(error).__name__})"


def classify_browser_launch_failure(
    error: BaseException,
    *,
    network_error_classifier: ErrorClassifier = _default_network_error_classifier,
) -> RefreshOutcome:
    """Convert a launcher exception to a credential-safe typed outcome."""

    if network_error_classifier(error):
        return RefreshOutcome.failure(
            FailureCode.NETWORK,
            detail=_failure_detail("browser launch network transport", error),
        )
    return RefreshOutcome.failure(
        FailureCode.BROWSER_LAUNCH,
        detail=_failure_detail("browser launch", error),
        restart_browser=True,
    )


def _parse_expiry(raw_expiry: object) -> Optional[datetime]:
    if not raw_expiry:
        return None
    try:
        value = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock must return a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_profile_busy_error(error: BaseException) -> bool:
    if isinstance(
        error,
        (ProfileBusyError, ProfileLeaseBusyError, ProfileLockUncertainError),
    ):
        return True
    message = str(error).casefold()
    return "database is locked" in message or "database is busy" in message


def _session_payload(raw_body: object) -> Optional[Mapping[str, Any]]:
    if not isinstance(raw_body, str) or not raw_body.strip():
        return None
    try:
        payload = json.loads(raw_body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _session_has_rejection(payload: Mapping[str, Any]) -> bool:
    error = payload.get("error")
    if error:
        return True
    status = payload.get("status")
    return status in (401, 403, "401", "403")


class KeepaliveRefresher:
    """Refresh one already-launched browser and atomically persist verified state."""

    def __init__(
        self,
        db: "Database",
        flow_client: "FlowClient",
        *,
        cookie_reader: CookieReader = read_session_token,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = lambda: datetime.now(timezone.utc),
        network_error_classifier: ErrorClassifier = _default_network_error_classifier,
        browser_error_classifier: ErrorClassifier = _default_browser_error_classifier,
        unauthenticated_error_classifier: ErrorClassifier = _is_unauthenticated,
        ready_timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS,
        ready_poll_seconds: float = DEFAULT_READY_POLL_SECONDS,
        session_settle_seconds: float = DEFAULT_SESSION_SETTLE_SECONDS,
    ) -> None:
        dependencies = (
            cookie_reader,
            sleep,
            clock,
            network_error_classifier,
            browser_error_classifier,
            unauthenticated_error_classifier,
        )
        if not all(callable(dependency) for dependency in dependencies):
            raise TypeError("refresher dependencies must be callable")
        self._db = db
        self._flow_client = flow_client
        self._cookie_reader = cookie_reader
        self._sleep = sleep
        self._clock = clock
        self._network_error_classifier = network_error_classifier
        self._browser_error_classifier = browser_error_classifier
        self._unauthenticated_error_classifier = unauthenticated_error_classifier
        self._ready_timeout_seconds = _positive_timeout(
            ready_timeout_seconds, "ready timeout"
        )
        self._ready_poll_seconds = _positive_timeout(
            ready_poll_seconds, "ready poll interval"
        )
        self._session_settle_seconds = _nonnegative_delay(
            session_settle_seconds, "session settle delay"
        )

    def _browser_failure(
        self,
        code: FailureCode,
        operation: str,
        error: BaseException,
    ) -> RefreshOutcome:
        if self._network_error_classifier(error):
            return RefreshOutcome.failure(
                FailureCode.NETWORK,
                detail=_failure_detail(f"{operation} network transport", error),
            )
        return RefreshOutcome.failure(
            code,
            detail=_failure_detail(operation, error),
            restart_browser=self._browser_error_classifier(error),
        )

    async def _wait_until_ready(self, tab: object) -> Optional[BaseException]:
        async def poll() -> Optional[BaseException]:
            checks = max(
                1,
                math.ceil(
                    self._ready_timeout_seconds / self._ready_poll_seconds
                ),
            )
            last_error: Optional[BaseException] = None
            for check in range(checks):
                try:
                    ready_state = await tab.evaluate(
                        "document.readyState", return_by_value=True
                    )
                    last_error = None
                    if ready_state == "complete":
                        return None
                except Exception as error:
                    last_error = error
                if check + 1 < checks:
                    await self._sleep(self._ready_poll_seconds)
            return last_error

        try:
            return await asyncio.wait_for(
                poll(), timeout=self._ready_timeout_seconds
            )
        except TimeoutError:
            return None

    async def _navigate_flow(
        self,
        browser: object,
        target: "KeepaliveToken",
        settle_seconds: float,
    ) -> Optional[RefreshOutcome]:
        project_id = str(target.current_project_id or "").strip()
        flow_url = FLOW_TOOL_BASE
        if project_id:
            flow_url = f"{FLOW_TOOL_BASE}/project/{quote(project_id, safe='')}"
        try:
            tab = await browser.get(flow_url)
        except Exception as error:
            return self._browser_failure(
                FailureCode.NAVIGATION, "Flow navigation", error
            )
        ready_error = await self._wait_until_ready(tab)
        if ready_error is not None:
            return self._browser_failure(
                FailureCode.NAVIGATION, "Flow page readiness", ready_error
            )
        await self._sleep(settle_seconds)
        return None

    async def _read_browser_session(
        self,
        browser: object,
    ) -> tuple[Optional[Mapping[str, Any]], Optional[RefreshOutcome]]:
        try:
            tab = await browser.get(SESSION_URL)
        except Exception as error:
            return None, self._browser_failure(
                FailureCode.NAVIGATION, "session navigation", error
            )
        await self._sleep(self._session_settle_seconds)
        try:
            raw_body = await tab.evaluate(
                "document.body.innerText", return_by_value=True
            )
        except Exception as error:
            return None, self._browser_failure(
                FailureCode.SESSION_BODY, "session body evaluation", error
            )
        payload = _session_payload(raw_body)
        if payload is None:
            return None, RefreshOutcome.failure(
                FailureCode.SESSION_BODY,
                detail="session endpoint returned no valid JSON object",
            )
        if _session_has_rejection(payload):
            return None, RefreshOutcome.failure(
                FailureCode.SESSION_REJECTED,
                detail="browser session was rejected",
                human_action=True,
            )
        return payload, None

    @staticmethod
    def _verified_session_identity(
        payload: Mapping[str, Any],
        target: "KeepaliveToken",
    ) -> tuple[Optional[str], Optional[str], Optional[RefreshOutcome]]:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            return None, None, RefreshOutcome.failure(
                FailureCode.SESSION_BODY,
                detail="session endpoint returned no access token",
            )
        user = payload.get("user")
        user = user if isinstance(user, dict) else {}
        email = str(user.get("email") or "").strip()
        normalized_email = normalize_account_email(email)
        if not normalized_email:
            return None, None, RefreshOutcome.failure(
                FailureCode.SESSION_REJECTED,
                detail="browser session returned no account identity",
                human_action=True,
            )
        expected_email = normalize_account_email(target.email)
        lifecycle_email = normalize_account_email(target.verified_email or "")
        if normalized_email != expected_email or (
            lifecycle_email and normalized_email != lifecycle_email
        ):
            return None, None, RefreshOutcome.failure(
                FailureCode.IDENTITY_MISMATCH,
                detail="browser account identity does not match the assigned account",
                human_action=True,
            )
        return access_token.strip(), email, None

    def _read_profile_session_token(
        self,
        profile_path: Path,
    ) -> tuple[Optional[str], Optional[RefreshOutcome]]:
        try:
            session_token = self._cookie_reader(profile_path)
        except (SessionTokenNotFoundError, SessionTokenTooShortError) as error:
            return None, RefreshOutcome.failure(
                FailureCode.COOKIE_MISSING,
                detail=_failure_detail("profile session cookie read", error),
                human_action=True,
            )
        except Exception as error:
            if _is_profile_busy_error(error):
                return None, RefreshOutcome.failure(
                    FailureCode.PROFILE_BUSY,
                    detail=_failure_detail("profile cookie store access", error),
                )
            return None, RefreshOutcome.failure(
                FailureCode.COOKIE_MISSING,
                detail=_failure_detail("profile session cookie read", error),
                human_action=True,
            )
        if (
            not isinstance(session_token, str)
            or len(session_token.encode("utf-8")) < MIN_SESSION_TOKEN_LENGTH
        ):
            return None, RefreshOutcome.failure(
                FailureCode.COOKIE_MISSING,
                detail="profile session cookie is missing or invalid",
                human_action=True,
            )
        return session_token, None

    async def _read_credits(
        self,
        access_token: str,
    ) -> tuple[Optional[Mapping[str, Any]], Optional[RefreshOutcome]]:
        try:
            credits_result = await self._flow_client.get_credits(access_token)
        except Exception as error:
            if self._unauthenticated_error_classifier(error):
                return None, RefreshOutcome.failure(
                    FailureCode.GRANT_EXPIRED,
                    detail=_failure_detail("credits authorization", error),
                    human_action=True,
                )
            if self._network_error_classifier(error):
                return None, RefreshOutcome.failure(
                    FailureCode.NETWORK,
                    detail=_failure_detail("credits network transport", error),
                )
            return None, RefreshOutcome.failure(
                FailureCode.CREDITS,
                detail=_failure_detail("credits validation", error),
            )
        if not isinstance(credits_result, dict):
            return None, RefreshOutcome.failure(
                FailureCode.CREDITS,
                detail="credits endpoint returned no valid JSON object",
            )
        credits = credits_result.get("credits")
        if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
            return None, RefreshOutcome.failure(
                FailureCode.CREDITS,
                detail="credits endpoint returned an invalid integer value",
            )
        return credits_result, None

    async def refresh(
        self,
        browser: object,
        target: "KeepaliveToken",
        profile: Path,
        settle_seconds: float,
    ) -> RefreshOutcome:
        """Run the proven browser-session/SQLite-cookie/credits refresh sequence."""

        try:
            settle_delay = _nonnegative_delay(settle_seconds, "settle delay")
            profile_path = Path(profile).expanduser().resolve(strict=False)
        except (TypeError, ValueError, OSError) as error:
            return RefreshOutcome.failure(
                FailureCode.INTERNAL,
                detail=_failure_detail("refresh input validation", error),
            )
        if not profile_path.is_dir():
            return RefreshOutcome.failure(
                FailureCode.PROFILE_MISSING,
                detail="browser profile is missing",
                human_action=True,
            )

        navigation_failure = await self._navigate_flow(
            browser, target, settle_delay
        )
        if navigation_failure is not None:
            return navigation_failure
        payload, session_failure = await self._read_browser_session(browser)
        if session_failure is not None or payload is None:
            return session_failure or RefreshOutcome.failure(FailureCode.INTERNAL)

        access_token, session_email, identity_failure = self._verified_session_identity(
            payload, target
        )
        if identity_failure is not None or access_token is None or session_email is None:
            return identity_failure or RefreshOutcome.failure(FailureCode.INTERNAL)

        session_token, cookie_failure = self._read_profile_session_token(profile_path)
        if cookie_failure is not None or session_token is None:
            return cookie_failure or RefreshOutcome.failure(FailureCode.INTERNAL)
        credits_result, credits_failure = await self._read_credits(access_token)
        if credits_failure is not None or credits_result is None:
            return credits_failure or RefreshOutcome.failure(FailureCode.INTERNAL)

        user = payload.get("user")
        user = user if isinstance(user, dict) else {}
        name = str(user.get("name") or "").strip() or session_email.split("@", 1)[0]
        raw_tier = credits_result.get("userPaygateTier")
        user_paygate_tier = (
            raw_tier.strip()
            if isinstance(raw_tier, str) and raw_tier.strip()
            else None
        )
        expiry = _parse_expiry(payload.get("expires"))
        credits = credits_result["credits"]
        snapshot = VerifiedAccountSnapshot(
            email=session_email,
            normalized_email=normalize_account_email(session_email),
            name=name,
            st=session_token,
            at=access_token,
            at_expires=expiry,
            credits=credits,
            user_paygate_tier=user_paygate_tier,
        )
        token_id = getattr(target, "id", None)
        if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id <= 0:
            return RefreshOutcome.failure(
                FailureCode.INTERNAL,
                detail="keepalive target has no valid token ID",
            )
        try:
            observed_at = _as_utc(self._clock())
            await self._db.apply_verified_account_snapshot(
                token_id,
                snapshot,
                observed_at=observed_at,
            )
        except Exception as error:
            return RefreshOutcome.failure(
                FailureCode.INTERNAL,
                detail=_failure_detail("verified snapshot persistence", error),
            )
        return RefreshOutcome.success(expiry=expiry, credits=credits)
