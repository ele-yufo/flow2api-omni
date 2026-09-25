"""FileCache 预签名媒体 URL 竞速下载测试。

背景（2026-09-25 生图慢事故）：成品下载固定走指纹/请求代理（住宅 IP），
代理节点间歇抖动时 3MB 图片下载 12s~235s，直连同一 URL 仅 ~2s。
预签名 CDN URL（flow-content.google/...?Signature=...）不校验出口 IP，
改为直连+代理并发竞速取先成功者；全失败时落回原有 curl_cffi→wget→curl 路径。
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
import tempfile

from src.shared.storage.file_cache import FileCache


def _make_response(status_code=200, content=b""):
    return type("Resp", (), {"status_code": status_code, "content": content})()


def _presigned_url(name: str = "abc") -> str:
    return (
        f"https://flow-content.google/image/{name}"
        "?Expires=1790366637&KeyName=labs-flow-prod-cdn&Signature=sig-abc123"
    )


class PresignedUrlDetectionTests(unittest.TestCase):
    def test_flow_content_with_signature_is_presigned(self):
        self.assertTrue(FileCache._is_presigned_media_url(_presigned_url()))

    def test_video_upsampled_url_is_presigned(self):
        url = "https://flow-content.google/video/x_upsampled?Expires=1&Signature=s"
        self.assertTrue(FileCache._is_presigned_media_url(url))

    def test_googleusercontent_with_signature_is_presigned(self):
        url = "https://lh3.googleusercontent.com/a.jpg?Signature=s&Expires=1"
        self.assertTrue(FileCache._is_presigned_media_url(url))

    def test_plain_google_url_without_signature_is_not(self):
        self.assertFalse(FileCache._is_presigned_media_url("https://flow.google.com/fx/api/trpc/media.get"))

    def test_non_google_host_with_signature_is_not(self):
        self.assertFalse(FileCache._is_presigned_media_url("https://example.com/x?Signature=s"))

    def test_garbage_url_is_not(self):
        self.assertFalse(FileCache._is_presigned_media_url("not a url"))


class RaceDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache = FileCache(cache_dir=self.tmpdir.name, default_timeout=3600)

    def tearDown(self):
        self.tmpdir.cleanup()

    async def test_race_prefers_fastest_route(self):
        """代理路慢、直连路快 → 取直连结果，且不等败者收尾。"""
        import time
        fetch_log = []

        class _FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def get(self, url, timeout=None, proxy=None, headers=None, impersonate=None, verify=False):
                if proxy is None:
                    await asyncio.sleep(0.05)
                    fetch_log.append("direct")
                    return _make_response(content=b"via-direct")
                await asyncio.sleep(2.0)  # 代理路慢
                fetch_log.append("proxy")
                return _make_response(content=b"via-proxy")

        started_at = time.monotonic()
        with patch("src.shared.storage.file_cache.AsyncSession", lambda: _FakeSession()):
            content = await self.cache._race_download_media(
                _presigned_url(), "http://127.0.0.1:7890", {}
            )
        elapsed = time.monotonic() - started_at

        self.assertEqual(content, b"via-direct")
        self.assertLess(elapsed, 1.0)  # 胜者返回不等败者
        self.assertEqual(fetch_log, ["direct"])

    async def test_race_falls_back_to_proxy_when_direct_fails(self):
        """直连不通（环境无直连出口）时代理路兜底。"""

        class _FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def get(self, url, timeout=None, proxy=None, headers=None, impersonate=None, verify=False):
                if proxy is None:
                    raise ConnectionError("direct blocked")
                await asyncio.sleep(0.05)
                return _make_response(content=b"via-proxy")

        with patch("src.shared.storage.file_cache.AsyncSession", lambda: _FakeSession()):
            content = await self.cache._race_download_media(
                _presigned_url(), "http://127.0.0.1:7890", {}
            )

        self.assertEqual(content, b"via-proxy")

    async def test_race_returns_none_when_all_routes_fail(self):
        """双路全失败 → None，调用方落回原有下载路径。"""

        class _FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def get(self, url, **kwargs):
                raise TimeoutError("both routes dead")

        with patch("src.shared.storage.file_cache.AsyncSession", lambda: _FakeSession()):
            content = await self.cache._race_download_media(
                _presigned_url(), "http://127.0.0.1:7890", {}
            )

        self.assertIsNone(content)

    async def test_race_single_route_when_no_proxy(self):
        """无代理配置时只跑直连一路（不空开第二路）。"""
        seen_proxies = []

        class _FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def get(self, url, timeout=None, proxy=None, headers=None, impersonate=None, verify=False):
                seen_proxies.append(proxy)
                return _make_response(content=b"solo")

        with patch("src.shared.storage.file_cache.AsyncSession", lambda: _FakeSession()):
            content = await self.cache._race_download_media(_presigned_url(), None, {})

        self.assertEqual(content, b"solo")
        self.assertEqual(seen_proxies, [None])

    async def test_download_and_cache_uses_race_for_presigned_url(self):
        """download_and_cache 对预签名 URL 走竞速并写缓存。"""
        cached_content = b"raced-bytes"

        class _FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def get(self, url, timeout=None, proxy=None, headers=None, impersonate=None, verify=False):
                if proxy is None:
                    return _make_response(content=cached_content)
                await asyncio.sleep(5)
                return _make_response(content=b"slow-proxy")

        with patch("src.shared.storage.file_cache.AsyncSession", lambda: _FakeSession()):
            filename = await self.cache.download_and_cache(_presigned_url("cached"), "image")

        path = Path(self.tmpdir.name) / filename
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), cached_content)

    async def test_download_and_cache_skips_race_for_video(self):
        """视频不竞速（大文件双路下载会翻倍代理流量），走原有单路。"""
        get_calls = []

        class _FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def get(self, url, timeout=None, proxy=None, headers=None, impersonate=None, verify=False):
                get_calls.append(proxy)
                return _make_response(content=b"video-bytes")

        self.cache._resolve_download_proxy = AsyncMock(return_value="http://127.0.0.1:7890")
        with patch("src.shared.storage.file_cache.AsyncSession", lambda: _FakeSession()):
            filename = await self.cache.download_and_cache(_presigned_url("vid"), "video")

        path = Path(self.tmpdir.name) / filename
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"video-bytes")
        # 原 curl_cffi 路径只发一次、且只走代理解析出的那一路
        self.assertEqual(get_calls, ["http://127.0.0.1:7890"])



if __name__ == "__main__":
    unittest.main()
