"""Token lifecycle, keepalive configuration, and telemetry persistence."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

import aiosqlite

from ..models import KeepaliveToken, TokenLifecycle
from ..token_states import (
    TOKEN_REASON_GRANT_EXPIRED,
    TOKEN_REASON_MANUAL_DISABLED,
    TOKEN_REASON_MEMBERSHIP_EXPIRED,
    TOKEN_REASON_ONBOARDING_PENDING,
    TOKEN_REASON_ST_REVOKED,
    AccountLifecycleState,
    AccountLifecycleStatus,
    TierClassification,
)
from ..account_identity import (
    VerifiedAccountSnapshot,
    VerifiedSnapshotResult,
    normalize_account_email,
)
from ..account_lifecycle import apply_account_tier_observation


_UNSET = object()
_AUTH_RECOVERY_REASONS = {TOKEN_REASON_GRANT_EXPIRED, TOKEN_REASON_ST_REVOKED}


@dataclass(frozen=True)
class PublishOutcome:
    """Observable outcome of a successful ``publish_verified_account`` call.

    Contains no credentials (no ST/AT) — safe to log or surface to callers.
    """

    token_id: int
    membership_status: str
    pool_transition: Optional[str]
    business_active: bool
    ban_reason: Optional[str]
    keepalive_enabled: bool
    runtime_mode: str
    profile_state: str


class PublishError(Exception):
    """Raised when ``publish_verified_account`` rejects its inputs or loses state.

    ``code`` is one of:
    - ``"warm_rejected"``: ``runtime_mode`` was not ``"persistent"``.
    - ``"internal"``: the token vanished between the two transactions.
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _membership_state_from_row(row) -> AccountLifecycleState:
    return AccountLifecycleState(
        confirmed_status=AccountLifecycleStatus(row["membership_confirmed_status"]),
        candidate=TierClassification(row["membership_candidate"]),
        candidate_count=int(row["membership_candidate_count"]),
    )


def _resolve_pool_state(
    row,
    previous_state,
    next_state,
    observed_at,
    allow_auth_reactivate: bool,
):
    is_active = bool(row["is_active"])
    ban_reason = row["ban_reason"]
    banned_at = row["banned_at"]
    pool_transition = None

    if allow_auth_reactivate and ban_reason in _AUTH_RECOVERY_REASONS:
        is_active = True
        ban_reason = None
        banned_at = None

    if (
        previous_state.confirmed_status is AccountLifecycleStatus.ACTIVE
        and next_state.confirmed_status is AccountLifecycleStatus.RETIRED
        and is_active
        and ban_reason is None
    ):
        is_active = False
        ban_reason = TOKEN_REASON_MEMBERSHIP_EXPIRED
        banned_at = observed_at
        pool_transition = "retired"
    elif (
        previous_state.confirmed_status is AccountLifecycleStatus.RETIRED
        and next_state.confirmed_status is AccountLifecycleStatus.ACTIVE
        and not is_active
        and ban_reason == TOKEN_REASON_MEMBERSHIP_EXPIRED
    ):
        is_active = True
        ban_reason = None
        banned_at = None
        pool_transition = "restored"

    return is_active, ban_reason, banned_at, pool_transition


class TokenLifecycleRepository:
    """Persistence for the one-to-one ``token_lifecycle`` token extension."""

    def __init__(self, engine):
        self._engine = engine

    @staticmethod
    def normalize_legacy_token_ids(raw_ids) -> List[int]:
        """Normalize TOML string/list values while preserving configured order."""
        if raw_ids is None:
            return []
        values: Iterable = raw_ids if isinstance(raw_ids, (list, tuple, set)) else str(raw_ids).split(",")
        normalized = []
        seen = set()
        for value in values:
            try:
                token_id = int(str(value).strip())
            except (TypeError, ValueError):
                continue
            if token_id <= 0 or token_id in seen:
                continue
            seen.add(token_id)
            normalized.append(token_id)
        return normalized

    async def create_for_token(self, token_id: int, *, db=None) -> None:
        """Create the default disabled/warm lifecycle row for a new token."""
        params = (
            token_id,
            AccountLifecycleStatus.ACTIVE.value,
            TierClassification.UNKNOWN.value,
        )
        sql = """
            INSERT INTO token_lifecycle (
                token_id, membership_confirmed_status, membership_candidate,
                membership_candidate_count, keepalive_enabled, runtime_mode, profile_state
            ) VALUES (?, ?, ?, 0, 0, 'warm', 'unprovisioned')
        """
        if db is not None:
            await db.execute(sql, params)
            return
        async with self._engine.transaction() as connection:
            await connection.execute(sql, params)

    async def delete_for_token(self, token_id: int, *, db=None) -> None:
        """Delete one token's lifecycle row, optionally inside a caller transaction."""
        sql = "DELETE FROM token_lifecycle WHERE token_id = ?"
        if db is not None:
            await db.execute(sql, (token_id,))
            return
        async with self._engine.transaction() as connection:
            await connection.execute(sql, (token_id,))

    async def backfill_legacy(self, db, raw_legacy_ids) -> None:
        """Backfill existing tokens once without overwriting lifecycle decisions.

        Configured browser keepalive IDs retain persistent/ready behavior. Every
        lifecycle insert uses ``INSERT OR IGNORE`` so repeated migrations cannot
        overwrite runtime changes. Legacy inactive rows without a reason are marked
        explicitly in the business token row as manually disabled.
        """
        legacy_ids = self.normalize_legacy_token_ids(raw_legacy_ids)
        for token_id in legacy_ids:
            await db.execute(
                """
                INSERT OR IGNORE INTO token_lifecycle (
                    token_id, membership_confirmed_status, membership_candidate,
                    membership_candidate_count, keepalive_enabled, runtime_mode, profile_state
                )
                SELECT id, ?, ?, 0, 1, 'persistent', 'ready'
                FROM tokens
                WHERE id = ?
                """,
                (
                    AccountLifecycleStatus.ACTIVE.value,
                    TierClassification.UNKNOWN.value,
                    token_id,
                ),
            )

        await db.execute(
            """
            INSERT OR IGNORE INTO token_lifecycle (
                token_id, membership_confirmed_status, membership_candidate,
                membership_candidate_count, keepalive_enabled, runtime_mode, profile_state
            )
            SELECT id, ?, ?, 0, 0, 'warm', 'unprovisioned'
            FROM tokens
            """,
            (
                AccountLifecycleStatus.ACTIVE.value,
                TierClassification.UNKNOWN.value,
            ),
        )
        await db.execute(
            """
            UPDATE tokens
            SET ban_reason = ?
            WHERE is_active = 0 AND ban_reason IS NULL
            """,
            (TOKEN_REASON_MANUAL_DISABLED,),
        )

    async def apply_verified_snapshot(
        self,
        token_id: int,
        snapshot: VerifiedAccountSnapshot,
        *,
        observed_at: Optional[datetime] = None,
        next_due_at: Optional[datetime] = None,
        allow_auth_reactivate: bool = True,
    ) -> VerifiedSnapshotResult:
        """Atomically persist a verified account snapshot and lifecycle transitions."""
        if not isinstance(snapshot, VerifiedAccountSnapshot):
            raise TypeError("snapshot must be a VerifiedAccountSnapshot")
        timestamp = observed_at or datetime.now(timezone.utc)

        async with self._engine.transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT t.id, t.email, t.is_active, t.ban_reason, t.banned_at,
                       l.membership_confirmed_status, l.membership_candidate,
                       l.membership_candidate_count, l.verified_email
                FROM tokens AS t
                JOIN token_lifecycle AS l ON l.token_id = t.id
                WHERE t.id = ?
                """,
                (token_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise KeyError(f"token lifecycle not found: {token_id}")

            expected_email = normalize_account_email(row["email"])
            verified_email = normalize_account_email(row["verified_email"])
            if snapshot.normalized_email != expected_email:
                raise ValueError("account identity mismatch")
            if verified_email and snapshot.normalized_email != verified_email:
                raise ValueError("profile identity mismatch")

            collision_cursor = await db.execute(
                "SELECT id FROM tokens WHERE st = ? AND id <> ? LIMIT 1",
                (snapshot.st, token_id),
            )
            if await collision_cursor.fetchone() is not None:
                raise ValueError("session token is already assigned to another account")

            previous_state = _membership_state_from_row(row)
            next_state = apply_account_tier_observation(
                previous_state, snapshot.user_paygate_tier
            )
            is_active, ban_reason, banned_at, pool_transition = _resolve_pool_state(
                row,
                previous_state,
                next_state,
                timestamp,
                allow_auth_reactivate,
            )

            await db.execute(
                """
                UPDATE tokens
                SET st = ?, at = ?, at_expires = COALESCE(?, at_expires),
                    name = CASE WHEN ? = '' THEN name ELSE ? END,
                    credits = ?,
                    user_paygate_tier = COALESCE(?, user_paygate_tier),
                    is_active = ?, ban_reason = ?, banned_at = ?
                WHERE id = ?
                """,
                (
                    snapshot.st,
                    snapshot.at,
                    snapshot.at_expires,
                    snapshot.name,
                    snapshot.name,
                    snapshot.credits,
                    snapshot.user_paygate_tier,
                    is_active,
                    ban_reason,
                    banned_at,
                    token_id,
                ),
            )
            await db.execute(
                """
                UPDATE token_lifecycle
                SET membership_confirmed_status = ?, membership_candidate = ?,
                    membership_candidate_count = ?, profile_state = 'ready',
                    verified_email = ?, last_keepalive_at = ?,
                    last_keepalive_success_at = ?, last_keepalive_status = 'success',
                    last_keepalive_error = NULL, keepalive_failure_count = 0,
                    last_observed_tier = ?, last_observed_at = ?,
                    next_due_at = COALESCE(?, next_due_at),
                    retired_at = CASE WHEN ? = 'retired' THEN ? ELSE retired_at END,
                    restored_at = CASE WHEN ? = 'restored' THEN ? ELSE restored_at END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE token_id = ?
                """,
                (
                    next_state.confirmed_status.value,
                    next_state.candidate.value,
                    next_state.candidate_count,
                    snapshot.email,
                    timestamp,
                    timestamp,
                    snapshot.user_paygate_tier,
                    timestamp,
                    next_due_at,
                    pool_transition,
                    timestamp,
                    pool_transition,
                    timestamp,
                    token_id,
                ),
            )

        return VerifiedSnapshotResult(
            token_id=token_id,
            membership_status=next_state.confirmed_status.value,
            pool_transition=pool_transition,
        )

    async def publish_verified_account(
        self,
        *,
        token_id: int,
        snapshot: VerifiedAccountSnapshot,
        runtime_mode: str,
        keepalive_enabled: bool,
        business_enabled: bool,
        observed_at: datetime,
    ) -> PublishOutcome:
        """Publish a verified account by reusing ``apply_verified_snapshot``.

        Preconditions: the caller has already inserted the ``tokens`` row and
        the ``token_lifecycle`` skeleton (via ``create_for_token``).

        This method only writes to ``tokens`` and ``token_lifecycle``; it never
        touches the network. It composes two transactions:

        1. ``apply_verified_snapshot`` (single atomic transaction): identity
           check, ST collision, membership hysteresis, ``_resolve_pool_state``
           (auth recovery + retired/restored), and the two-table UPDATE for
           credentials + lifecycle observation fields.
        2. A small desired-state transaction that clears ``onboarding_pending``,
           applies ``business_enabled`` (only when no other ban owns the row),
           and persists ``keepalive_enabled`` / ``runtime_mode`` / ``profile_state``.

        See spec §8 for the rationale and consistency analysis.
        """
        if runtime_mode != "persistent":
            raise PublishError("warm_rejected")
        if not isinstance(keepalive_enabled, bool):
            raise TypeError("keepalive_enabled must be a bool")
        if not isinstance(business_enabled, bool):
            raise TypeError("business_enabled must be a bool")

        snapshot_result = await self.apply_verified_snapshot(
            token_id,
            snapshot,
            observed_at=observed_at,
            allow_auth_reactivate=True,
            next_due_at=None,
        )

        async with self._engine.transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT is_active, ban_reason FROM tokens WHERE id = ?",
                (token_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise PublishError(
                    "internal",
                    "token vanished after apply_verified_snapshot",
                )
            is_active = bool(row["is_active"])
            ban_reason = row["ban_reason"]

            # 2a. Clear the onboarding placeholder ban (not handled by _resolve_pool_state).
            if ban_reason == TOKEN_REASON_ONBOARDING_PENDING:
                ban_reason = None
            # 2b. business_enabled toggles is_active/manual_disabled only when no
            #     protected ban owns the row (manual_disabled/429/consecutive_errors
            #     are preserved because ban_reason is not None here).
            if not business_enabled and ban_reason is None:
                is_active, ban_reason = False, TOKEN_REASON_MANUAL_DISABLED
            elif business_enabled and ban_reason is None:
                is_active = True

            await db.execute(
                "UPDATE tokens SET is_active = ?, ban_reason = ?, "
                "banned_at = CASE WHEN ? IS NULL THEN NULL ELSE banned_at END "
                "WHERE id = ?",
                (is_active, ban_reason, ban_reason, token_id),
            )
            await db.execute(
                "UPDATE token_lifecycle SET keepalive_enabled = ?, "
                "runtime_mode = ?, profile_state = 'ready', "
                "updated_at = CURRENT_TIMESTAMP WHERE token_id = ?",
                (1 if keepalive_enabled else 0, runtime_mode, token_id),
            )

        return PublishOutcome(
            token_id=token_id,
            membership_status=snapshot_result.membership_status,
            pool_transition=snapshot_result.pool_transition,
            business_active=is_active,
            ban_reason=ban_reason,
            keepalive_enabled=keepalive_enabled,
            runtime_mode=runtime_mode,
            profile_state="ready",
        )

    async def get(self, token_id: int) -> Optional[TokenLifecycle]:
        """Return one token's lifecycle row."""
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM token_lifecycle WHERE token_id = ?", (token_id,)
            )
            row = await cursor.fetchone()
            return TokenLifecycle(**dict(row)) if row else None

    async def list_enabled(self) -> List[TokenLifecycle]:
        """List keepalive-enabled lifecycle rows without business-token filtering."""
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM token_lifecycle
                WHERE keepalive_enabled = 1
                ORDER BY token_id
                """
            )
            return [TokenLifecycle(**dict(row)) for row in await cursor.fetchall()]

    async def list_enabled_tokens(self) -> List[KeepaliveToken]:
        """Join enabled lifecycle rows to full tokens, including disabled/banned ones."""
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    t.*,
                    l.membership_confirmed_status,
                    l.membership_candidate,
                    l.membership_candidate_count,
                    l.keepalive_enabled,
                    l.runtime_mode,
                    l.profile_state,
                    l.verified_email,
                    l.last_keepalive_at,
                    l.last_keepalive_success_at,
                    l.last_keepalive_status,
                    l.last_keepalive_error,
                    l.keepalive_failure_count,
                    l.next_due_at,
                    l.last_failure_at,
                    l.last_failure_code,
                    l.last_failure_detail,
                    l.last_observed_tier,
                    l.last_observed_at,
                    l.retired_at,
                    l.restored_at,
                    l.last_alert_code,
                    l.last_alert_at,
                    l.alert_episode,
                    l.alerted
                FROM tokens AS t
                JOIN token_lifecycle AS l ON l.token_id = t.id
                WHERE l.keepalive_enabled = 1
                ORDER BY t.id
                """
            )
            return [KeepaliveToken(**dict(row)) for row in await cursor.fetchall()]

    async def set_desired_state(
        self,
        token_id: int,
        *,
        keepalive_enabled: Optional[bool] = None,
        runtime_mode: Optional[str] = None,
        profile_state: Optional[str] = None,
    ) -> None:
        """Atomically update selected keepalive fields without changing business state."""
        if keepalive_enabled is None and runtime_mode is None and profile_state is None:
            raise ValueError("at least one desired-state field is required")
        if keepalive_enabled is not None and not isinstance(keepalive_enabled, bool):
            raise TypeError("keepalive_enabled must be a bool")
        if runtime_mode is not None and runtime_mode not in ("persistent", "warm"):
            raise ValueError("runtime_mode must be 'persistent' or 'warm'")
        async with self._engine.transaction() as db:
            cursor = await db.execute(
                """
                UPDATE token_lifecycle
                SET keepalive_enabled = COALESCE(?, keepalive_enabled),
                    runtime_mode = COALESCE(?, runtime_mode),
                    profile_state = COALESCE(?, profile_state),
                    updated_at = CURRENT_TIMESTAMP
                WHERE token_id = ?
                """,
                (keepalive_enabled, runtime_mode, profile_state, token_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"token lifecycle not found: {token_id}")

    async def finalize_onboarding_state(
        self,
        token_id: int,
        *,
        keepalive_enabled: bool,
        runtime_mode: str,
        enable_business_if_pending: bool,
        completed_at: Optional[datetime] = None,
    ) -> None:
        """Atomically publish a validated profile and resolve onboarding-owned bans."""
        if not isinstance(keepalive_enabled, bool):
            raise TypeError("keepalive_enabled must be a bool")
        if runtime_mode not in ("persistent", "warm"):
            raise ValueError("runtime_mode must be 'persistent' or 'warm'")
        if not isinstance(enable_business_if_pending, bool):
            raise TypeError("enable_business_if_pending must be a bool")
        timestamp = completed_at or datetime.now(timezone.utc)

        async with self._engine.transaction() as db:
            cursor = await db.execute(
                """
                SELECT t.ban_reason
                FROM tokens AS t
                JOIN token_lifecycle AS l ON l.token_id = t.id
                WHERE t.id = ?
                """,
                (token_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise KeyError(f"token lifecycle not found: {token_id}")

            if row[0] == TOKEN_REASON_ONBOARDING_PENDING:
                if enable_business_if_pending:
                    await db.execute(
                        """
                        UPDATE tokens
                        SET is_active = 1, ban_reason = NULL, banned_at = NULL
                        WHERE id = ? AND ban_reason = ?
                        """,
                        (token_id, TOKEN_REASON_ONBOARDING_PENDING),
                    )
                else:
                    await db.execute(
                        """
                        UPDATE tokens
                        SET is_active = 0, ban_reason = ?, banned_at = ?
                        WHERE id = ? AND ban_reason = ?
                        """,
                        (
                            TOKEN_REASON_MANUAL_DISABLED,
                            timestamp,
                            token_id,
                            TOKEN_REASON_ONBOARDING_PENDING,
                        ),
                    )

            lifecycle_cursor = await db.execute(
                """
                UPDATE token_lifecycle
                SET keepalive_enabled = ?, runtime_mode = ?, profile_state = 'ready',
                    updated_at = CURRENT_TIMESTAMP
                WHERE token_id = ?
                """,
                (keepalive_enabled, runtime_mode, token_id),
            )
            if lifecycle_cursor.rowcount != 1:
                raise KeyError(f"token lifecycle not found: {token_id}")

    async def set_verified_email(self, token_id: int, verified_email: Optional[str]) -> None:
        """Set or explicitly clear the independently verified profile email."""
        async with self._engine.transaction() as db:
            cursor = await db.execute(
                """
                UPDATE token_lifecycle
                SET verified_email = ?, updated_at = CURRENT_TIMESTAMP
                WHERE token_id = ?
                """,
                (verified_email, token_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"token lifecycle not found: {token_id}")

    async def update_membership_state(
        self,
        token_id: int,
        state: AccountLifecycleState,
        *,
        observed_tier: Optional[str] = None,
        observed_at: Optional[datetime] = None,
    ) -> None:
        """Persist canonical membership state plus observation/transition telemetry."""
        if not isinstance(state, AccountLifecycleState):
            raise TypeError("state must be an AccountLifecycleState")
        async with self._engine.transaction() as db:
            current_cursor = await db.execute(
                "SELECT membership_confirmed_status FROM token_lifecycle WHERE token_id = ?",
                (token_id,),
            )
            current = await current_cursor.fetchone()
            if current is None:
                raise KeyError(f"token lifecycle not found: {token_id}")
            previous_status = current[0]
            retired = (
                previous_status == AccountLifecycleStatus.ACTIVE.value
                and state.confirmed_status is AccountLifecycleStatus.RETIRED
            )
            restored = (
                previous_status == AccountLifecycleStatus.RETIRED.value
                and state.confirmed_status is AccountLifecycleStatus.ACTIVE
            )
            await db.execute(
                """
                UPDATE token_lifecycle
                SET membership_confirmed_status = ?,
                    membership_candidate = ?,
                    membership_candidate_count = ?,
                    last_observed_tier = COALESCE(?, last_observed_tier),
                    last_observed_at = CASE
                        WHEN ? IS NULL THEN last_observed_at
                        ELSE COALESCE(?, CURRENT_TIMESTAMP)
                    END,
                    retired_at = CASE
                        WHEN ? THEN COALESCE(?, CURRENT_TIMESTAMP)
                        ELSE retired_at
                    END,
                    restored_at = CASE
                        WHEN ? THEN COALESCE(?, CURRENT_TIMESTAMP)
                        ELSE restored_at
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE token_id = ?
                """,
                (
                    state.confirmed_status.value,
                    state.candidate.value,
                    state.candidate_count,
                    observed_tier,
                    observed_tier,
                    observed_at,
                    retired,
                    observed_at,
                    restored,
                    observed_at,
                    token_id,
                ),
            )

    async def update_alert(
        self,
        token_id: int,
        *,
        alert_code: Optional[str],
        alerted_at: Optional[datetime] = None,
    ) -> None:
        """Set or explicitly clear the most recent lifecycle alert marker."""
        async with self._engine.transaction() as db:
            if alert_code is None:
                cursor = await db.execute(
                    """
                    UPDATE token_lifecycle
                    SET last_alert_code = NULL,
                        last_alert_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                    """,
                    (token_id,),
                )
            else:
                cursor = await db.execute(
                    """
                    UPDATE token_lifecycle
                    SET last_alert_code = ?,
                        last_alert_at = COALESCE(?, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                    """,
                    (alert_code, alerted_at, token_id),
                )
            if cursor.rowcount != 1:
                raise KeyError(f"token lifecycle not found: {token_id}")

    async def update_alert_state(
        self,
        token_id: int,
        *,
        alert_code: Optional[str],
        episode: int,
        alerted: bool,
        alerted_at: Optional[datetime] = None,
    ) -> None:
        """Persist the complete alert episode state for restart-safe deduplication."""
        if isinstance(episode, bool) or not isinstance(episode, int) or episode < 0:
            raise ValueError("episode must be a non-negative integer")
        if not isinstance(alerted, bool):
            raise TypeError("alerted must be a bool")
        if alert_code is None and alerted:
            raise ValueError("alerted requires an active alert code")

        async with self._engine.transaction() as db:
            cursor = await db.execute(
                """
                UPDATE token_lifecycle
                SET last_alert_code = ?,
                    last_alert_at = CASE
                        WHEN ? IS NULL THEN NULL
                        ELSE COALESCE(?, CURRENT_TIMESTAMP)
                    END,
                    alert_episode = ?, alerted = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE token_id = ?
                """,
                (
                    alert_code,
                    alert_code,
                    alerted_at,
                    episode,
                    alerted,
                    token_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"token lifecycle not found: {token_id}")

    async def update_keepalive_telemetry(
        self,
        token_id: int,
        *,
        status: str,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
        attempted_at: Optional[datetime] = None,
        next_due_at=_UNSET,
    ) -> None:
        """Record a keepalive outcome and scheduling/failure telemetry."""
        normalized_status = str(status).strip().lower()
        if not normalized_status:
            raise ValueError("status must not be empty")
        due_assignment = "next_due_at = next_due_at"
        due_params = []
        if next_due_at is not _UNSET:
            due_assignment = "next_due_at = ?"
            due_params = [next_due_at]

        async with self._engine.transaction() as db:
            if normalized_status in ("success", "ok", "alive"):
                params = [attempted_at, attempted_at, status, *due_params, token_id]
                cursor = await db.execute(
                    f"""
                    UPDATE token_lifecycle
                    SET last_keepalive_at = COALESCE(?, CURRENT_TIMESTAMP),
                        last_keepalive_success_at = COALESCE(?, CURRENT_TIMESTAMP),
                        last_keepalive_status = ?,
                        last_keepalive_error = NULL,
                        keepalive_failure_count = 0,
                        {due_assignment},
                        updated_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                    """,
                    params,
                )
            else:
                params = [
                    attempted_at,
                    status,
                    error,
                    attempted_at,
                    error_code,
                    error,
                    *due_params,
                    token_id,
                ]
                cursor = await db.execute(
                    f"""
                    UPDATE token_lifecycle
                    SET last_keepalive_at = COALESCE(?, CURRENT_TIMESTAMP),
                        last_keepalive_status = ?,
                        last_keepalive_error = ?,
                        keepalive_failure_count = keepalive_failure_count + 1,
                        last_failure_at = COALESCE(?, CURRENT_TIMESTAMP),
                        last_failure_code = ?,
                        last_failure_detail = ?,
                        {due_assignment},
                        updated_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                    """,
                    params,
                )
            if cursor.rowcount != 1:
                raise KeyError(f"token lifecycle not found: {token_id}")

    async def clear_keepalive_error(self, token_id: int) -> None:
        """Explicitly clear telemetry error text without changing other fields."""
        async with self._engine.transaction() as db:
            cursor = await db.execute(
                """
                UPDATE token_lifecycle
                SET last_keepalive_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE token_id = ?
                """,
                (token_id,),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"token lifecycle not found: {token_id}")
