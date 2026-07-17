"""Flow 分享链接解析（纯函数，无 I/O）。

Flow 分享页 `https://labs.google/fx/tools/flow/shared/video/<uuid>` 内嵌一个公开的
og:video 端点 `https://labs.google/fx/api/og-video/shared/<uuid>`，直接返回 mp4，
无需登录/代理/授权（已实测）。所以只要从用户输入里抽出 uuid，即可拼出可下载的视频地址。
"""
import re
from typing import Optional

# 标准 uuid v4 形态；不锁版本位，宽松匹配 Google 的 id。
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# 固定的 Google 端点前缀 —— 只按 uuid 拼这一个 host，杜绝任意 URL 抓取（无 SSRF 面）。
OG_VIDEO_BASE = "https://labs.google/fx/api/og-video/shared"


def extract_share_uuid(text: str) -> Optional[str]:
    """从完整分享 URL、带 query/fragment 的链接、或裸 uuid 中抽出 uuid（小写）。

    返回 None 表示输入里没有合法 uuid（调用方据此报"链接无效"）。
    """
    if not text:
        return None
    m = _UUID_RE.search(text)
    return m.group(0).lower() if m else None


def og_video_url(uuid: str) -> str:
    """由 uuid 构造公开 og-video mp4 地址。"""
    return f"{OG_VIDEO_BASE}/{uuid}"
