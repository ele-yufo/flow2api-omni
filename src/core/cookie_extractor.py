"""从多种粘贴格式中抽取 __Secure-next-auth.session-token (ST)。

支持（按优先级）：
1. Netscape cookies.txt 全文（制表符分隔，7 列，第 6 列为 name、第 7 列为 value）
2. Cookie 请求头 / `a=b; key=value` 分号串
3. JSON 数组（DevTools "Copy all as JSON" / EditThisCookie 导出）
4. 裸 ST 值（以 eyJ 开头的 JWE）

抽不到或长度 < MIN_ST_LEN 时抛 ValueError。
"""
import json
from typing import Optional

SESSION_TOKEN_KEY = "__Secure-next-auth.session-token"
MIN_ST_LEN = 200  # 实测 ST ~1064，护栏防止把 undefined/截断值写入


def _from_json(text: str) -> Optional[str]:
    stripped = text.lstrip()
    if not (stripped.startswith("[") or stripped.startswith("{")):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    cookies = data if isinstance(data, list) else data.get("cookies", [])
    if not isinstance(cookies, list):
        return None
    found: Optional[str] = None
    for item in cookies:
        if isinstance(item, dict) and item.get("name") == SESSION_TOKEN_KEY:
            value = item.get("value")
            if isinstance(value, str):
                found = value.strip()  # 命中多条时取最后一条（与 cookies.txt 一致）
    return found


def _from_netscape(text: str) -> Optional[str]:
    found: Optional[str] = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # Netscape 标准是 TAB 分隔，但很多粘贴/复制会把 TAB 变成空格。
        # 先按 TAB 切；不足 7 段则按任意空白再切（cookie 各字段本身不含空格，安全）。
        parts = line.split("\t")
        if len(parts) < 7:
            parts = line.split()
        if len(parts) >= 7 and parts[5].strip() == SESSION_TOKEN_KEY:
            found = parts[6].strip()  # 命中多条时保留最后一条（最新）
    return found


def _from_cookie_header(text: str) -> Optional[str]:
    marker = SESSION_TOKEN_KEY + "="
    if marker not in text:
        return None
    seg = text.split(marker, 1)[1]
    # 值止于 ; 或空白/换行；ST 本身无空格
    value = seg.split(";", 1)[0].strip()
    if value:
        value = value.split()[0]
    return value or None


def _from_bare(text: str) -> Optional[str]:
    tokens = text.split()
    if tokens and tokens[0].startswith("eyJ"):
        return tokens[0]
    return None


def extract_session_token(raw: str) -> str:
    """从任意支持的格式中抽取 ST；失败抛 ValueError。"""
    if not raw or not raw.strip():
        raise ValueError("空输入：未提供任何内容")
    text = raw.strip()

    candidate = (
        _from_json(text)
        or _from_netscape(text)
        or _from_cookie_header(text)
        or _from_bare(text)
    )

    if not candidate:
        raise ValueError(
            f"未能从输入中找到 {SESSION_TOKEN_KEY}。"
            f"请粘贴 cookies.txt 全文、Cookie 头、JSON 导出或裸 ST 值。"
        )
    candidate = candidate.strip()
    if len(candidate) < MIN_ST_LEN:
        raise ValueError(
            f"提取到的 ST 过短 (len={len(candidate)} < {MIN_ST_LEN})，疑似无效或已损坏。"
        )
    return candidate
