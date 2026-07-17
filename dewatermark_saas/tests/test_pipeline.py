"""管线四条错误语义 + 成功路径（mock 掉真实 I/O，全离线）。"""
import asyncio
import os

import dewatermark_saas.pipeline as pl
from dewatermark_saas.jobs import Job

SHARE = "https://labs.google/fx/tools/flow/shared/video/acf396ba-f17a-40dc-a9b8-7ddfad28be07"


def _write(path: str, data: bytes = b"x") -> None:
    with open(path, "wb") as f:
        f.write(data)


def _run(job: Job) -> None:
    asyncio.run(pl.process(job))


def test_link_success(tmp_path, monkeypatch):
    async def fake_dl(url, dest):
        _write(dest)

    async def fake_pp(inp, out):
        _write(out, b"clean")
        return {"status_code": 200, "json": {"ok": True, "timings": {"total": 8.0}}}

    monkeypatch.setattr(pl, "_download_to", fake_dl)
    monkeypatch.setattr(pl, "_call_propainter", fake_pp)

    job = Job(id="j1", source_kind="link", work_dir=str(tmp_path), link=SHARE)
    _run(job)
    assert job.status == "done"
    assert job.output_path and os.path.exists(job.output_path)
    assert job.timings == {"total": 8.0}
    assert job.error_message is None


def test_unsupported_resolution(tmp_path, monkeypatch):
    async def fake_dl(url, dest):
        _write(dest)

    async def fake_pp(inp, out):
        return {"status_code": 200, "json": {"ok": False, "reason": "resolution 1920x1080 not calibrated"}}

    monkeypatch.setattr(pl, "_download_to", fake_dl)
    monkeypatch.setattr(pl, "_call_propainter", fake_pp)

    job = Job(id="j2", source_kind="link", work_dir=str(tmp_path), link=SHARE)
    _run(job)
    assert job.status == "error"
    assert "720p" in job.error_message


def test_propainter_500(tmp_path, monkeypatch):
    async def fake_dl(url, dest):
        _write(dest)

    async def fake_pp(inp, out):
        return {"status_code": 500, "json": {}}

    monkeypatch.setattr(pl, "_download_to", fake_dl)
    monkeypatch.setattr(pl, "_call_propainter", fake_pp)

    job = Job(id="j3", source_kind="link", work_dir=str(tmp_path), link=SHARE)
    _run(job)
    assert job.status == "error"
    assert job.error_message == "去水印处理失败，请重试"


def test_download_failure(tmp_path, monkeypatch):
    async def fake_dl(url, dest):
        raise RuntimeError("boom")

    monkeypatch.setattr(pl, "_download_to", fake_dl)

    job = Job(id="j4", source_kind="link", work_dir=str(tmp_path), link=SHARE)
    _run(job)
    assert job.status == "error"
    assert job.error_message == "视频获取失败，请检查链接"


def test_invalid_link(tmp_path):
    job = Job(id="j5", source_kind="link", work_dir=str(tmp_path), link="not-a-link")
    _run(job)
    assert job.status == "error"
    assert job.error_message == "链接无效，请贴 Flow 分享链接"


def test_upload_success(tmp_path, monkeypatch):
    async def fake_pp(inp, out):
        _write(out, b"clean")
        return {"status_code": 200, "json": {"ok": True, "timings": None}}

    monkeypatch.setattr(pl, "_call_propainter", fake_pp)

    in_file = str(tmp_path / "uploaded.mp4")
    _write(in_file)
    job = Job(id="j6", source_kind="upload", work_dir=str(tmp_path), input_path=in_file)
    _run(job)
    assert job.status == "done"
    assert job.output_path and os.path.exists(job.output_path)


def test_upload_missing_file(tmp_path):
    job = Job(id="j7", source_kind="upload", work_dir=str(tmp_path), input_path=str(tmp_path / "gone.mp4"))
    _run(job)
    assert job.status == "error"
    assert "上传文件丢失" in job.error_message
