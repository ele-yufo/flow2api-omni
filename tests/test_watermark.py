"""Tests for Pro-video de-watermark integration (config + watermark_client)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.config import config
# watermark_client moved to src.shared.gpu (P3); patch its real module (httpx lives there).
from src.shared.gpu import watermark_client
from src.shared.gpu.watermark_client import dewatermark_video


class WatermarkConfigTests(unittest.TestCase):
    def test_defaults(self):
        # Defaults present and sane regardless of whether [watermark] is in toml.
        self.assertIsInstance(config.watermark_enabled, bool)
        self.assertTrue(config.watermark_service_url.startswith("http"))
        self.assertFalse(config.watermark_service_url.endswith("/"))  # trailing slash stripped
        self.assertGreaterEqual(config.watermark_timeout_seconds, 10)
        self.assertLessEqual(config.watermark_timeout_seconds, 600)

    def test_timeout_clamped(self):
        raw = config.get_raw_config()
        saved = raw.get("watermark")
        try:
            raw["watermark"] = {"timeout_seconds": 99999}
            self.assertEqual(config.watermark_timeout_seconds, 600)   # clamped high
            raw["watermark"] = {"timeout_seconds": 1}
            self.assertEqual(config.watermark_timeout_seconds, 10)    # clamped low
            raw["watermark"] = {"timeout_seconds": "bad"}
            self.assertEqual(config.watermark_timeout_seconds, 120)   # fallback
        finally:
            if saved is None:
                raw.pop("watermark", None)
            else:
                raw["watermark"] = saved


class FakeCache:
    """Minimal FileCache stand-in: creates the 'downloaded' input file."""
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)

    async def download_and_cache(self, url, media_type):
        name = "deadbeef.mp4"
        (self.cache_dir / name).write_bytes(b"fake-video")
        return name


def _fake_client(*, status=200, ok=True, create_output=True, raise_on_post=False):
    """Build a fake httpx.AsyncClient class with configurable behaviour."""
    class FakeResp:
        def raise_for_status(self):
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")

        def json(self):
            return {"ok": ok, "output": "x", "timings": {}, "total": 1.0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            if raise_on_post:
                raise RuntimeError("timeout")
            if create_output and ok and status < 400:
                Path(json["output"]).write_bytes(b"dewm-video")
            return FakeResp()

    return FakeClient


class DewatermarkVideoTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, **client_kw):
        with tempfile.TemporaryDirectory() as d:
            cache = FakeCache(d)
            with patch.object(watermark_client.httpx, "AsyncClient", _fake_client(**client_kw)):
                return await dewatermark_video("https://upstream/video.mp4", cache, "http://h:18282")

    async def test_success_returns_local_url(self):
        url = await self._run(status=200, ok=True, create_output=True)
        self.assertEqual(url, "http://h:18282/tmp/dewm_deadbeef.mp4")

    async def test_service_not_ok_returns_none(self):
        self.assertIsNone(await self._run(status=200, ok=False, create_output=False))

    async def test_http_error_returns_none(self):
        self.assertIsNone(await self._run(status=500))

    async def test_post_exception_returns_none(self):
        self.assertIsNone(await self._run(raise_on_post=True))

    async def test_output_missing_returns_none(self):
        # service says ok but never wrote the file -> fallback
        self.assertIsNone(await self._run(status=200, ok=True, create_output=False))


if __name__ == "__main__":
    unittest.main()
