"""处理管线：解析链接 → 下载源视频 → 调 ProPainter 去水印 → 结果。

错误语义与 `src/shared/gpu/watermark_client.dewatermark_video` 相反：那套是"失败即回退原片"
（给 flow2api 尽力而为用）；本 SaaS **必须把失败暴露给用户**，绝不把带水印原片当成功返回。
"""
import logging
import os
from typing import Optional

import httpx

# 复用一期抽出的共享配置层（config.watermark_service_url / watermark_timeout_seconds）。
# 从 repo 根启动时 src 是 PEP420 namespace 包，可直接 import。
from src.shared.config import config

from .config import settings
from .jobs import Job
from .share_link import extract_share_uuid, og_video_url

logger = logging.getLogger("dewm_saas.pipeline")


def _client_kwargs() -> dict:
    # trust_env=False：只用显式传入的 proxy，不被 shell 的 *_PROXY 环境变量串走。
    kw = {"follow_redirects": True, "timeout": settings.download_timeout,
          "trust_env": False, "headers": {"User-Agent": settings.http_ua}}
    if settings.http_proxy:
        kw["proxy"] = settings.http_proxy
    return kw


async def _download_to(url: str, dest: str) -> None:
    """流式下载视频到 dest，带大小上限与内容类型校验。失败抛异常。"""
    async with httpx.AsyncClient(**_client_kwargs()) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"源视频请求返回 {resp.status_code}")
            ctype = resp.headers.get("content-type", "").lower()
            if ctype.startswith("text/") or "html" in ctype:
                raise RuntimeError("链接未指向视频（可能分享已失效）")
            total = 0
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    total += len(chunk)
                    if total > settings.max_input_bytes:
                        raise ValueError("input too large")
                    f.write(chunk)
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise RuntimeError("下载得到空文件")


async def _call_propainter(in_path: str, out_path: str) -> dict:
    """调本机常驻 ProPainter 服务。返回其 JSON；网络层异常向上抛。"""
    url = f"{config.watermark_service_url}/dewatermark"
    # 本地 127.0.0.1，绝不走代理：trust_env=False 挡掉环境里的 *_PROXY。
    async with httpx.AsyncClient(timeout=config.watermark_timeout_seconds, trust_env=False) as client:
        resp = await client.post(url, json={"input": in_path, "output": out_path})
    return {"status_code": resp.status_code, "json": _safe_json(resp)}


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


async def process(job: Job) -> None:
    """跑完一个 job，全程更新 job.status / progress / error_message。绝不抛出。"""
    try:
        os.makedirs(job.work_dir, exist_ok=True)
        in_path = os.path.abspath(os.path.join(job.work_dir, "in.mp4"))
        out_path = os.path.abspath(os.path.join(job.work_dir, "out.mp4"))

        # 1) 拿到源视频 -----------------------------------------------------
        if job.source_kind == "link":
            job.set_status("resolving")
            uuid = extract_share_uuid(job.link or "")
            if not uuid:
                job.error_message = "链接无效，请贴 Flow 分享链接"
                job.set_status("error")
                return
            job.set_status("downloading")
            try:
                await _download_to(og_video_url(uuid), in_path)
            except ValueError:
                job.error_message = "文件过大（上限 200MB）"
                job.set_status("error")
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("download failed job=%s: %s", job.id, e)
                job.error_message = "视频获取失败，请检查链接"
                job.set_status("error")
                return
        else:  # upload：文件已在 POST 阶段落地到 in_path
            if not job.input_path or not os.path.exists(job.input_path):
                job.error_message = "上传文件丢失，请重试"
                job.set_status("error")
                return
            in_path = os.path.abspath(job.input_path)
            job.set_status("downloading", progress=45)

        # 2) 去水印 ---------------------------------------------------------
        job.set_status("dewatermarking")
        try:
            result = await _call_propainter(in_path, out_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("propainter call failed job=%s: %s", job.id, e)
            job.error_message = "去水印服务不可用，请稍后重试"
            job.set_status("error")
            return

        code, data = result["status_code"], result["json"]
        if code == 200 and data.get("ok"):
            if not os.path.exists(out_path):
                job.error_message = "去水印处理失败，请重试"
                job.set_status("error")
                return
            job.output_path = out_path
            job.timings = data.get("timings")
            job.set_status("done")
            logger.info("done job=%s timings=%s", job.id, job.timings)
            return
        if code == 200 and data.get("ok") is False:
            # 分辨率未标定：ProPainter 只覆盖 720p sparkle。
            job.error_message = "仅支持 720p Veo 视频（1280×720 / 720×1280）"
            job.set_status("error")
            return
        # 其余（500 等）：真错误
        job.error_message = "去水印处理失败，请重试"
        job.set_status("error")
    except Exception as e:  # noqa: BLE001  最后兜底，绝不让 worker 崩
        logger.exception("unexpected pipeline error job=%s: %s", job.id, e)
        job.error_message = "内部错误，请重试"
        job.set_status("error")
