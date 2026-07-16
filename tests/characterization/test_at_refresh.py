"""Characterization: lock AT-refresh timing decision (1h threshold)."""
from datetime import datetime, timedelta, timezone
from tests.conftest import assert_golden


def test_should_refresh_at_golden():
    from src.services.tokens.at_refresh import should_refresh_at
    from src.core.models import Token

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def tok(**kw):
        return Token(st="S" * 60, email="e@e.com", **kw)

    out = {
        "no_at": should_refresh_at(tok(at=None), now),
        "no_expires": should_refresh_at(tok(at="AT", at_expires=None), now),
        "expires_30min": should_refresh_at(tok(at="AT", at_expires=now + timedelta(minutes=30)), now),
        "expires_2h": should_refresh_at(tok(at="AT", at_expires=now + timedelta(hours=2)), now),
        "expires_59min": should_refresh_at(tok(at="AT", at_expires=now + timedelta(minutes=59)), now),
        "expires_61min": should_refresh_at(tok(at="AT", at_expires=now + timedelta(minutes=61)), now),
        "already_expired": should_refresh_at(tok(at="AT", at_expires=now - timedelta(hours=1)), now),
    }
    assert out == {"no_at": True, "no_expires": True, "expires_30min": True,
                   "expires_2h": False, "expires_59min": True, "expires_61min": False,
                   "already_expired": True}
    assert_golden("at_refresh", out)
