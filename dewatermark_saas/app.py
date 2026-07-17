"""去水印 SaaS Demo —— FastAPI 组合根。

- 后台单 worker 串行消费 job 队列（与 ProPainter GPU 串行匹配）。
- Basic Auth 网关（凭证从环境变量取；/healthz 豁免供上游探活）。
- 托管 designer 产出的单页前端（FileResponse，不用需要 aiofiles 的 StaticFiles）。

启动（从 repo 根，让 src.shared 可导入）：
    /opt/Projects/flow2api/.venv/bin/python -m uvicorn dewatermark_saas.app:app \
        --host 127.0.0.1 --port 18300
"""
import asyncio
import logging
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import settings
from .jobs import JobStore
from .pipeline import process
from .share_link import extract_share_uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dewm_saas.app")

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_security = HTTPBasic(auto_error=False)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_security)) -> None:
    """Basic Auth 网关。未配置凭证时放行（仅本地测试用；部署必须设 DEWM_BASIC_USER/PASS）。"""
    if not settings.auth_enabled:
        return
    unauthorized = HTTPException(
        status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"}
    )
    if credentials is None:
        raise unauthorized
    ok_user = secrets.compare_digest(credentials.username, settings.basic_user)
    ok_pass = secrets.compare_digest(credentials.password, settings.basic_pass)
    if not (ok_user and ok_pass):
        raise unauthorized


async def _worker(store: JobStore) -> None:
    """串行消费 job 队列。process() 内部已兜底不抛，这里再加一层保险。"""
    while True:
        job_id = await store.queue.get()
        try:
            job = store.get(job_id)
            if job is not None:
                await process(job)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("worker crashed on job %s", job_id)
        finally:
            store.queue.task_done()


async def _reaper(store: JobStore) -> None:
    """周期回收过期终态 job：清内存条目 + 删工作目录（防磁盘/内存无界增长）。"""
    work_root = os.path.abspath(settings.work_dir)
    while True:
        try:
            await asyncio.sleep(settings.reap_interval_seconds)
            cutoff = time.monotonic() - settings.job_ttl_seconds
            for work_dir in store.reap(cutoff):
                # 安全兜底：只删 work_dir 根下的目录，绝不越界 rmtree。
                if work_dir and os.path.abspath(work_dir).startswith(work_root + os.sep):
                    shutil.rmtree(work_dir, ignore_errors=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("reaper error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.work_dir, exist_ok=True)
    store = JobStore()
    app.state.store = store
    worker = asyncio.create_task(_worker(store))
    reaper = asyncio.create_task(_reaper(store))
    logger.info("dewm_saas 启动 port=%s work_dir=%s auth_enabled=%s",
                settings.port, settings.work_dir, settings.auth_enabled)
    if not settings.auth_enabled:
        logger.warning("Basic Auth 未启用（未设 DEWM_BASIC_USER/PASS）—— 切勿公开暴露！")
    try:
        yield
    finally:
        for task in (worker, reaper):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Veo 去水印 Demo", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/", dependencies=[Depends(require_auth)])
async def index() -> FileResponse:
    idx = os.path.join(_STATIC_DIR, "index.html")
    if not os.path.exists(idx):
        raise HTTPException(status_code=404, detail="前端尚未就绪")
    return FileResponse(idx, media_type="text/html")


async def _save_upload(upload: UploadFile, dest: str, max_bytes: int) -> None:
    """流式落地上传文件，超限即删并抛 413。"""
    total = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                f.close()
                try:
                    os.remove(dest)
                except OSError:
                    pass
                raise HTTPException(status_code=413, detail="文件过大（上限 200MB）")
            f.write(chunk)


@app.post("/api/jobs", dependencies=[Depends(require_auth)])
async def create_job(
    request: Request,
    link: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
) -> JSONResponse:
    store: JobStore = request.app.state.store
    if not link and file is None:
        raise HTTPException(status_code=400, detail="请提供 Flow 分享链接或上传视频文件")

    if file is not None:
        job = store.create("upload", work_base=settings.work_dir)
        os.makedirs(job.work_dir, exist_ok=True)
        in_path = os.path.join(job.work_dir, "in.mp4")
        await _save_upload(file, in_path, settings.max_input_bytes)
        job.input_path = in_path
    else:
        if extract_share_uuid(link) is None:
            raise HTTPException(status_code=400, detail="链接无效，请贴 Flow 分享链接")
        job = store.create("link", work_base=settings.work_dir, link=link)

    await store.enqueue(job.id)
    return JSONResponse({"job_id": job.id})


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def job_status(job_id: str, request: Request) -> JSONResponse:
    store: JobStore = request.app.state.store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job 不存在")
    return JSONResponse(job.public_view())


@app.get("/api/jobs/{job_id}/download", dependencies=[Depends(require_auth)])
async def download(job_id: str, request: Request) -> FileResponse:
    store: JobStore = request.app.state.store
    job = store.get(job_id)
    if job is None or job.status != "done" or not job.output_path or not os.path.exists(job.output_path):
        raise HTTPException(status_code=404, detail="结果不存在或尚未完成")
    return FileResponse(
        job.output_path,
        media_type="video/mp4",
        filename=f"dewatermarked_{job_id[:8]}.mp4",
    )
