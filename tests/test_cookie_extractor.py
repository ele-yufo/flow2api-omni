import unittest

from src.core.cookie_extractor import extract_session_token

KEY = "__Secure-next-auth.session-token"
VALID = "eyJ" + "A" * 1100  # 模拟一个够长的 JWE


class CookieExtractorTests(unittest.TestCase):
    def test_netscape_cookies_txt_fulltext(self):
        raw = (
            "# Netscape HTTP Cookie File\n"
            "# comment line\n"
            ".labs.google\tTRUE\t/\tFALSE\t1816193751\t_ga\tGA1.1.x\n"
            f"labs.google\tFALSE\t/\tTRUE\t1784225752\t{KEY}\t{VALID}\n"
            "labs.google\tFALSE\t/\tFALSE\t0\temail\truby%40gmail.com\n"
        )
        self.assertEqual(extract_session_token(raw), VALID)

    def test_cookie_header(self):
        raw = f"_ga=GA1.1.x; {KEY}={VALID}; email=ruby%40gmail.com"
        self.assertEqual(extract_session_token(raw), VALID)

    def test_json_array(self):
        raw = f'[{{"name":"_ga","value":"x"}},{{"name":"{KEY}","value":"{VALID}"}}]'
        self.assertEqual(extract_session_token(raw), VALID)

    def test_bare_token(self):
        self.assertEqual(extract_session_token(f"  {VALID}  "), VALID)

    def test_missing_raises(self):
        with self.assertRaises(ValueError):
            extract_session_token("_ga=GA1.1.x; email=ruby%40gmail.com")

    def test_too_short_raises(self):
        raw = f"labs.google\tFALSE\t/\tTRUE\t0\t{KEY}\tundefined\n"
        with self.assertRaises(ValueError):
            extract_session_token(raw)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            extract_session_token("   ")

    def test_picks_last_when_duplicated(self):
        raw = (
            f"labs.google\tFALSE\t/\tTRUE\t0\t{KEY}\t{'eyJ' + 'B' * 1100}\n"
            f"labs.google\tFALSE\t/\tTRUE\t0\t{KEY}\t{VALID}\n"
        )
        self.assertEqual(extract_session_token(raw), VALID)

    def test_json_picks_last_when_duplicated(self):
        raw = (
            f'[{{"name":"{KEY}","value":"{"eyJ" + "B" * 1100}"}},'
            f'{{"name":"{KEY}","value":"{VALID}"}}]'
        )
        self.assertEqual(extract_session_token(raw), VALID)


class ResolveStFromRequestTests(unittest.TestCase):
    def test_resolve_prefers_raw(self):
        from src.api.admin import resolve_st_from_request
        raw = f"x=1; {KEY}={VALID}"
        self.assertEqual(resolve_st_from_request(st=None, raw=raw), VALID)

    def test_resolve_uses_st_when_no_raw(self):
        from src.api.admin import resolve_st_from_request
        self.assertEqual(resolve_st_from_request(st=VALID, raw=None), VALID)

    def test_resolve_none_raises(self):
        from src.api.admin import resolve_st_from_request
        with self.assertRaises(ValueError):
            resolve_st_from_request(st=None, raw=None)


class AddTokenRouteErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_raw_returns_400_not_500(self):
        # 回归测试：抽取失败必须是干净的 400，不能被外层 except Exception 吞成 500
        from fastapi import HTTPException
        from src.api import admin
        req = admin.AddTokenRequest(raw="cookie1=abc; cookie2=def")  # 不含 ST
        with self.assertRaises(HTTPException) as ctx:
            await admin.add_token(req, token="dummy")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_missing_both_returns_400(self):
        from fastapi import HTTPException
        from src.api import admin
        req = admin.AddTokenRequest()  # st 与 raw 都为空
        with self.assertRaises(HTTPException) as ctx:
            await admin.add_token(req, token="dummy")
        self.assertEqual(ctx.exception.status_code, 400)
