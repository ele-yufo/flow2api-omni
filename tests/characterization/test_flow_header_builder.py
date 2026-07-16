"""Characterization: lock HeaderBuilder output (extracted from FlowClient, P5).

UA is deterministic per account_id (md5-seeded). FlowClient's thin delegating methods
must produce identical output to the HeaderBuilder they now wrap.
"""
from tests.conftest import assert_golden

_FP = {
    "user_agent": "Mozilla/5.0 FP UA",
    "accept_language": "en-US",
    "sec_ch_ua": '"X";v="1"',
    "sec_ch_ua_mobile": "?0",
    "sec_ch_ua_platform": '"Linux"',
}


def _matrix(hb_build, hb_gen, hb_fam):
    return {
        "st_only": hb_build(st_token="ST_FIXED_ACCOUNT_0001_abcdefgh", use_st=True),
        "at_only": hb_build(at_token="AT_FIXED_ACCOUNT_0001_abcdefgh", use_at=True),
        "with_fingerprint": hb_build(
            st_token="ST_FIXED_ACCOUNT_0001_abcdefgh", use_st=True, fingerprint=_FP
        ),
        "ua_deterministic": hb_gen("fixed_account_seed"),
        "ua_family_chrome": hb_fam("Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36"),
        "ua_family_edge": hb_fam("Mozilla/5.0 Edg/124.0"),
        "ua_family_unknown": hb_fam("curl/8.0"),
    }


def test_header_builder_golden():
    from src.services.flow.http_headers import HeaderBuilder

    hb = HeaderBuilder()
    out = _matrix(hb.build_request_headers, hb.generate_user_agent, hb.get_user_agent_family)
    assert out["st_only"]["Cookie"].startswith("__Secure-next-auth.session-token=")
    assert_golden("flow_header_builder", out)


def test_flowclient_delegation_matches_builder():
    """FlowClient 薄委托方法 == HeaderBuilder(抽取后行为不变)。"""
    from src.services.flow_client import FlowClient
    from src.services.flow.http_headers import HeaderBuilder

    fc = FlowClient(None)  # proxy_manager=None; header 方法不依赖它
    hb = HeaderBuilder()
    via_fc = _matrix(fc._build_request_headers, fc._generate_user_agent, fc._get_user_agent_family)
    via_hb = _matrix(hb.build_request_headers, hb.generate_user_agent, hb.get_user_agent_family)
    assert via_fc == via_hb
