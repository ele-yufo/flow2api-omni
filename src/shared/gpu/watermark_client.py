"""Client for the resident Pro-video de-watermark service (dewatermark/server.py).

Downloads the finished upstream video to a local file, asks the local ProPainter
service to remove the sparkle watermark, and returns a local URL for the result.
Returns None on any failure so the caller can fall back to the original URL.
"""
import os
from typing import Optional

import httpx

from ..config import config
from ..telemetry import debug_logger


async def dewatermark_video(video_url: str, file_cache, base_url: str) -> Optional[str]:
    """De-watermark a finished Pro video.

    Args:
        video_url: upstream (or already-local) video URL of the generated clip.
        file_cache: FileCache instance (provides download_and_cache + cache_dir).
        base_url: base URL the client uses to reach this server's /tmp/ mount.

    Returns:
        Local URL of the de-watermarked video, or None on failure (caller falls back).
    """
    try:
        # 1) ensure a local copy of the source video (idempotent: cached by URL hash).
        # Use absolute paths: the de-watermark service runs with a different cwd.
        in_name = await file_cache.download_and_cache(video_url, "video")
        in_path = os.path.abspath(os.path.join(str(file_cache.cache_dir), in_name))
        if not os.path.exists(in_path):
            debug_logger.log_error(f"[WATERMARK] downloaded file missing: {in_path}")
            return None

        out_name = f"dewm_{in_name}"
        out_path = os.path.abspath(os.path.join(str(file_cache.cache_dir), out_name))

        # 2) call the resident de-watermark service (local file paths, same machine)
        url = f"{config.watermark_service_url}/dewatermark"
        async with httpx.AsyncClient(timeout=config.watermark_timeout_seconds) as client:
            resp = await client.post(url, json={"input": in_path, "output": out_path})
            resp.raise_for_status()
            data = resp.json()

        if not data.get("ok") or not os.path.exists(out_path):
            debug_logger.log_error(f"[WATERMARK] service returned no usable output: {data}")
            return None

        debug_logger.log_info(f"[WATERMARK] removed watermark: {out_name} timings={data.get('timings')}")
        return f"{base_url}/tmp/{out_name}"
    except Exception as e:
        debug_logger.log_error(f"[WATERMARK] de-watermark failed ({type(e).__name__}): {e}")
        return None
