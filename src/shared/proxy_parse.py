"""Parse user-entered proxy lines into a standard URL (shared, pure).

Extracted from ProxyManager. Handles protocol-prefixed, st5, and bare host:port[:user:pass]
forms. Locked by tests/characterization/test_proxy_parse.py.
"""
import re
from typing import Optional


def parse_proxy_line(line: str) -> Optional[str]:
    """将用户输入代理转换为标准 URL 格式。

    支持格式:
    - http/https/socks5/socks5h://user:pass@host:port
    - socks5://host:port:user:pass
    - st5 host:port:user:pass
    - host:port
    - host:port:user:pass
    """
    if not line:
        return None

    line = line.strip()
    if not line:
        return None

    # st5 host:port:user:pass
    st5_match = re.match(r"^st5\s+(.+)$", line, re.IGNORECASE)
    if st5_match:
        rest = st5_match.group(1).strip()
        if "@" in rest:
            return f"socks5://{rest}"
        parts = rest.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            host = parts[0]
            port = parts[1]
            username = parts[2]
            password = ":".join(parts[3:])
            return f"socks5://{username}:{password}@{host}:{port}"
        return None

    # 协议前缀格式
    if line.startswith(("http://", "https://", "socks5://", "socks5h://")):
        if "@" in line:
            return line
        try:
            protocol_end = line.index("://") + 3
            protocol = line[:protocol_end]
            rest = line[protocol_end:]
            parts = rest.split(":")
            if len(parts) >= 4 and parts[1].isdigit():
                host = parts[0]
                port = parts[1]
                username = parts[2]
                password = ":".join(parts[3:])
                return f"{protocol}{username}:{password}@{host}:{port}"
            if len(parts) == 2 and parts[1].isdigit():
                return line
        except Exception:
            return None
        return None

    # 无协议，带 @：默认按 http 处理
    if "@" in line:
        return f"http://{line}"

    # 无协议，按冒号数量判断
    parts = line.split(":")
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{parts[0]}:{parts[1]}"

    if len(parts) >= 4 and parts[1].isdigit():
        host = parts[0]
        port = parts[1]
        username = parts[2]
        password = ":".join(parts[3:])
        return f"http://{username}:{password}@{host}:{port}"

    return None
