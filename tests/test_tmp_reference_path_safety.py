"""客户端提供的 /tmp/ 引用不得读到缓存目录以外的文件。

公网入口上线后（2026-09-06），`image_url` 里的 `/tmp/...` 由任意持 key 的远端客户端
提供；保留目录成分会让 `/tmp/../../etc/passwd` 变成一次任意文件读取。
"""

import asyncio
from pathlib import Path

import pytest

from src.api import routes


class _FakeCache:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir


class _FakeHandler:
    def __init__(self, cache_dir: Path):
        self.file_cache = _FakeCache(cache_dir)


class _DeadNetwork:
    """网络回退等价于"取不到"——测试只关心有没有读到本地机密文件。

    生产代码把下载异常吞掉并返回 None,所以这里不能靠抛异常断言,
    只能断言返回值里没有目标文件的内容。
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        raise ConnectionError("no network in tests")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "abc123.jpg").write_bytes(b"legit-bytes")
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"SECRET")

    monkeypatch.setattr(routes, "generation_handler", _FakeHandler(cache_dir))
    monkeypatch.setattr(routes, "AsyncSession", lambda *a, **k: _DeadNetwork())
    return cache_dir


def test_cached_file_still_readable(wired):
    data = asyncio.run(routes.retrieve_image_data("http://host:18282/tmp/abc123.jpg"))
    assert data == b"legit-bytes"


@pytest.mark.parametrize(
    "url",
    [
        "http://host:18282/tmp/../secret.txt",
        "https://flow.example.com/tmp/../../etc/passwd",
        "http://host:18282/tmp/sub/../../secret.txt",
    ],
)
def test_traversal_never_reads_outside_cache(wired, url):
    assert asyncio.run(routes.retrieve_image_data(url)) is None
