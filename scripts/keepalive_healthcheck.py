#!/usr/bin/env python3
"""保活巡检:汇总账号存活状态并投递到 Discord。

由本地 systemd 定时器周期调用(每 12h)。**不主动打 Google**——只读取本地 DB
当前状态(活跃账号的存活由后台保活任务每日主动 st_to_at+get_credits 维护、
失效会即时标记 ST_REVOKED 并禁用),因此本巡检零额外风控负担,只做"汇报"。

输出一条 Discord 消息:N 活 / M 死、总额度、需维护的死号清单。
"""
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/opt/Projects/flow2api")

from src.services.alert_notifier import AlertNotifier  # noqa: E402
from src.core.config import config  # noqa: E402

DB = "/opt/Projects/flow2api/data/flow.db"


def read_state():
    conn = sqlite3.connect(DB)
    try:
        rows = conn.execute(
            "SELECT id, email, is_active, COALESCE(ban_reason, ''), credits "
            "FROM tokens ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    active = [r for r in rows if r[2]]
    dead = [r for r in rows if not r[2]]
    total_credits = sum((r[4] or 0) for r in active)
    return active, dead, total_credits


async def main() -> int:
    active, dead, total_credits = read_state()
    n_active, n_dead = len(active), len(dead)

    if dead:
        title = f"🩺 保活巡检：{n_active} 活 / {n_dead} 需维护"
        severity = "critical"
        dead_str = "、".join(f"{e}（{ban or '禁用'}）" for _, e, _, ban, _ in dead)
        action = f"以下号已失效、需重新登录注入：{dead_str}"
    else:
        title = f"🩺 保活巡检：{n_active} 个号全部存活"
        severity = "warning"
        action = "✅ 全部存活，无需人工介入。"

    description = (
        f"活跃账号 {n_active} 个，总额度 {total_credits}。\n{action}\n"
        f"巡检时刻 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    fields = [
        ("活跃", str(n_active), True),
        ("需维护", str(n_dead), True),
        ("总额度", str(total_credits), True),
    ]

    notifier = AlertNotifier(config.alert_webhook_url)
    ok = await notifier.send_alert(title, description, fields=fields, severity=severity)
    print(
        f"[healthcheck] {datetime.now(timezone.utc).isoformat()} "
        f"active={n_active} dead={n_dead} credits={total_credits} delivered={ok}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
