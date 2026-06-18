"""运维告警投递：把告警格式化为 Discord webhook embed 并尽力投递。

只负责"怎么发"；"何时/为何发"由调用方（token_manager）决定。
告警失败绝不影响主流程——所有异常都被吞掉并记日志。
"""
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple, Union

from curl_cffi.requests import AsyncSession

from ..core.logger import debug_logger

_SEVERITY_COLOR = {
    "critical": 15158332,  # 红
    "warning": 15105570,   # 橙
}
_DEFAULT_COLOR = 15105570

FieldsType = Optional[Sequence[Union[Tuple[str, str, bool], dict]]]


def _normalize_fields(fields: FieldsType) -> List[dict]:
    out: List[dict] = []
    for f in fields or []:
        if isinstance(f, dict):
            out.append({"name": str(f.get("name", "")), "value": str(f.get("value", "")),
                        "inline": bool(f.get("inline", False))})
        else:
            name, value, inline = f
            out.append({"name": str(name), "value": str(value), "inline": bool(inline)})
    return out


def build_discord_payload(title: str, description: str, fields: FieldsType = None,
                          severity: str = "warning") -> dict:
    """构造 Discord webhook body（单 embed）。纯函数，便于单测。"""
    embed = {
        "title": title,
        "description": description,
        "color": _SEVERITY_COLOR.get(severity, _DEFAULT_COLOR),
        "fields": _normalize_fields(fields),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return {"username": "Flow2API 哨兵", "embeds": [embed]}


class AlertNotifier:
    """把告警投递到 Discord webhook（尽力而为）。"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url or ""

    async def send_alert(self, title: str, description: str, fields: FieldsType = None,
                         severity: str = "warning") -> bool:
        if not self.webhook_url:
            debug_logger.log_warning(f"[ALERT] (无 webhook，仅记录) {title}: {description}")
            return False
        payload = build_discord_payload(title, description, fields, severity)
        try:
            async with AsyncSession() as session:
                resp = await session.post(self.webhook_url, json=payload, timeout=10)
            status = getattr(resp, "status_code", 0)
            if status >= 400:
                body = getattr(resp, "text", "")
                debug_logger.log_warning(f"[ALERT] 投递被拒 ({title}): HTTP {status} {str(body)[:200]}")
                return False
            debug_logger.log_info(f"[ALERT] 已投递: {title} (HTTP {status})")
            return True
        except Exception as e:
            debug_logger.log_warning(f"[ALERT] 投递失败 ({title}): {e}")
            return False
