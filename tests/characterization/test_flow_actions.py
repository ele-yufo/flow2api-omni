"""Characterization: lock core auth/project flow_client actions (mocked _make_request)."""
import asyncio
from unittest.mock import AsyncMock

from src.core.cookie_extractor import SESSION_TOKEN_KEY, MIN_ST_LEN


def _fc():
    from src.services.flow_client import FlowClient
    return FlowClient(None)


def test_st_to_at_captures_rotated_st():
    fc = _fc()

    async def fake_make_request(**kw):
        # simulate labs.google returning a rotated ST via Set-Cookie capture
        if kw.get("capture_set_cookie") is not None:
            kw["capture_set_cookie"].append(f"{SESSION_TOKEN_KEY}={'r' * (MIN_ST_LEN + 5)}; Path=/")
        return {"access_token": "AT_NEW", "expires": "2026-01-01T00:00:00Z"}

    fc._make_request = AsyncMock(side_effect=fake_make_request)
    result = asyncio.run(fc.st_to_at("ST_OLD"))
    assert result["access_token"] == "AT_NEW"
    assert result["rotated_st"] == "r" * (MIN_ST_LEN + 5)  # rotated ST captured


def test_st_to_at_no_rotation_when_same():
    fc = _fc()
    fc._make_request = AsyncMock(return_value={"access_token": "AT"})
    result = asyncio.run(fc.st_to_at("ST"))
    assert "rotated_st" not in result  # no rotated cookie -> key absent


def test_create_project_extracts_project_id():
    fc = _fc()
    fc._make_request = AsyncMock(return_value={
        "result": {"data": {"json": {"result": {"projectId": "proj-uuid-1"}}}}})
    pid = asyncio.run(fc.create_project("ST", "My Title"))
    assert pid == "proj-uuid-1"


def test_create_project_missing_id_raises():
    fc = _fc()
    fc._make_request = AsyncMock(return_value={"result": {"data": {"json": {"result": {}}}}})
    try:
        asyncio.run(fc.create_project("ST", "T"))
        assert False, "should raise on missing projectId"
    except Exception as e:
        assert "projectId" in str(e)


def test_get_credits_passthrough():
    fc = _fc()
    fc._make_request = AsyncMock(return_value={"credits": 920, "userPaygateTier": "PAYGATE_TIER_ONE"})
    result = asyncio.run(fc.get_credits("AT"))
    assert result == {"credits": 920, "userPaygateTier": "PAYGATE_TIER_ONE"}
