"""Pure media-cache helpers (extension guess / download headers / error normalize).

Extracted from FileCache (0 self). Locked by tests/characterization/test_cache_helpers.py.
"""
import mimetypes
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse


def guess_extension(url: str, media_type: str) -> str:
    """尽量保留原始扩展名，未知时回退到默认值。"""
    path = urlparse(url).path or ""
    guessed, _ = mimetypes.guess_type(path)
    suffix = Path(path).suffix.lower()

    if media_type == "video":
        if suffix in {".mp4", ".mov", ".webm", ".mkv", ".m4v"}:
            return suffix
        if guessed == "video/webm":
            return ".webm"
        if guessed == "video/quicktime":
            return ".mov"
        return ".mp4"

    if media_type == "image":
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp"}:
            return suffix
        if guessed == "image/png":
            return ".png"
        if guessed == "image/webp":
            return ".webp"
        if guessed == "image/gif":
            return ".gif"
        if guessed == "image/avif":
            return ".avif"
        if guessed == "image/bmp":
            return ".bmp"
        return ".jpg"

    return suffix


def build_download_headers(
    media_type: str,
    fingerprint: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """构建媒体下载请求头，优先复用当前打码浏览器指纹。"""
    headers = {
        "Accept": (
            "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
            if media_type == "image"
            else "*/*"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://labs.google/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
    }

    if media_type == "image":
        headers["Sec-Fetch-Dest"] = "image"
    else:
        headers["Sec-Fetch-Dest"] = "video"

    if isinstance(fingerprint, dict):
        if fingerprint.get("user_agent"):
            headers["User-Agent"] = str(fingerprint["user_agent"])
        if fingerprint.get("accept_language"):
            headers["Accept-Language"] = str(fingerprint["accept_language"])
        if fingerprint.get("sec_ch_ua"):
            headers["sec-ch-ua"] = str(fingerprint["sec_ch_ua"])
        if fingerprint.get("sec_ch_ua_mobile"):
            headers["sec-ch-ua-mobile"] = str(fingerprint["sec_ch_ua_mobile"])
        if fingerprint.get("sec_ch_ua_platform"):
            headers["sec-ch-ua-platform"] = str(fingerprint["sec_ch_ua_platform"])

    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return headers


def normalize_cache_error(error: Exception) -> str:
    """整理缓存错误，避免将底层命令异常直接暴露给用户。"""
    if isinstance(error, FileNotFoundError):
        missing_name = Path(getattr(error, "filename", "") or "curl").name or "curl"
        return f"本机未安装 {missing_name}"

    message = str(error or "").strip()
    if not message:
        return "未知错误"

    if message.startswith("Failed to cache file:"):
        message = message.split(":", 1)[1].strip() or "未知错误"

    return message
