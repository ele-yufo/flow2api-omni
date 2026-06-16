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
