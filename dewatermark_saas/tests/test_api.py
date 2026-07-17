"""FastAPI 端点 + Basic Auth（TestClient，全离线；worker 用 mock 的 process）。"""
import os
import time

from fastapi.testclient import TestClient

import dewatermark_saas.app as appmod
from dewatermark_saas.config import Settings

SHARE = "https://labs.google/fx/tools/flow/shared/video/acf396ba-f17a-40dc-a9b8-7ddfad28be07"


def _settings(tmp_path, **kw) -> Settings:
    base = dict(
        host="127.0.0.1", port=18300, work_dir=str(tmp_path),
        max_input_bytes=1024 * 1024, download_timeout=5.0, http_ua="ua",
        http_proxy="", basic_user="", basic_pass="",
        job_ttl_seconds=9999.0, reap_interval_seconds=9999.0,
    )
    base.update(kw)
    return Settings(**base)


def test_missing_body_returns_400(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "settings", _settings(tmp_path))
    with TestClient(appmod.app) as c:
        assert c.post("/api/jobs").status_code == 400


def test_invalid_link_returns_400(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "settings", _settings(tmp_path))
    with TestClient(appmod.app) as c:
        assert c.post("/api/jobs", data={"link": "nope"}).status_code == 400


def test_basic_auth_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "settings", _settings(tmp_path, basic_user="u", basic_pass="p"))
    with TestClient(appmod.app) as c:
        # /healthz 永远开放
        assert c.get("/healthz").status_code == 200
        # 无凭证 -> 401
        assert c.post("/api/jobs").status_code == 401
        # 错凭证 -> 401
        assert c.post("/api/jobs", auth=("u", "wrong")).status_code == 401
        # 对凭证 -> 过鉴权，落到 400（缺 body）
        assert c.post("/api/jobs", auth=("u", "p")).status_code == 400


def test_job_lifecycle_and_download(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "settings", _settings(tmp_path))

    async def fake_process(job):
        os.makedirs(job.work_dir, exist_ok=True)
        out = os.path.join(job.work_dir, "out.mp4")
        with open(out, "wb") as f:
            f.write(b"clean-video")
        job.output_path = out
        job.set_status("done")

    monkeypatch.setattr(appmod, "process", fake_process)

    with TestClient(appmod.app) as c:
        r = c.post("/api/jobs", data={"link": SHARE})
        assert r.status_code == 200
        jid = r.json()["job_id"]

        status = {}
        for _ in range(60):
            status = c.get(f"/api/jobs/{jid}").json()
            if status["status"] == "done":
                break
            time.sleep(0.05)
        assert status["status"] == "done"
        assert status["download_url"] == f"/api/jobs/{jid}/download"

        d = c.get(f"/api/jobs/{jid}/download")
        assert d.status_code == 200
        assert d.content == b"clean-video"


def test_status_404_for_unknown_job(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "settings", _settings(tmp_path))
    with TestClient(appmod.app) as c:
        assert c.get("/api/jobs/deadbeef").status_code == 404
