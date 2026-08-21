#!/usr/bin/env python3
"""保活巡检:汇总账号存活状态并投递到 Discord。

由本地 systemd 定时器每小时触发。**不主动打 Google**——只读取本地 DB
当前状态,零额外风控负担,只做"汇报"。

两层判定:
1. 业务层:tokens.is_active / ban_reason(主服务即时维护)。
2. 保活层:token_lifecycle 遥测新鲜度(复用 scripts/keepalive_patrol.py 的
   cadence 分类)——保活 daemon 静默僵死时 is_active 不会变,只有新鲜度
   能暴露(2026-08-21 事故:daemon 冻结 12h、2 个账号 AT 过期,旧巡检只
   看 is_active 仍报全部存活)。

每小时运行但只在出问题时投递 Discord(critical=死号或 UNHEALTHY,
warning=PROBE_ERROR 退避中);每天 00/12 UTC 各投递一次全绿心跳汇总,
证明巡检自身活着。--force-report 强制立即投递当前状态(运维验收用)。
"""
import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/opt/Projects/flow2api")

from scripts.keepalive_patrol import (  # noqa: E402
    build_cadence_policy,
    classify_telemetry,
    read_telemetry,
)
from src.services.alert_notifier import AlertNotifier  # noqa: E402
from src.core.config import config  # noqa: E402

DB = "/opt/Projects/flow2api/data/flow.db"
HEARTBEAT_HOURS_UTC = frozenset({0, 12})


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


def evaluate_lifecycle(now=None):
    """用 patrol 的 cadence 策略分类每个保活账号的新鲜度。"""

    policy = build_cadence_policy(
        config.keepalive_browser_interval_seconds,
        config.keepalive_browser_retired_interval_seconds,
    )
    results = []
    for record in read_telemetry(DB):
        classification, reason = classify_telemetry(record, policy=policy, now=now)
        results.append((record, classification, reason))
    return results


def decide_report(active, dead, total_credits, lifecycle_rows, *, now, force=False):
    """返回 (should_send, severity, title, description, fields) 或 (False, ...) 跳过。"""

    n_active = len(active)
    dead_list = [f"{e}（{ban or '禁用'}）" for _, e, _, ban, _ in dead]
    unhealthy = [(r, reason) for r, cls, reason in lifecycle_rows if cls == "UNHEALTHY"]
    probe_errors = [(r, reason) for r, cls, reason in lifecycle_rows if cls == "PROBE_ERROR"]
    current_time = now.astimezone(timezone.utc)

    issues = []
    if dead_list:
        issues.append(f"失效需重新登录注入：{'、'.join(dead_list)}")
    if unhealthy:
        overdue = "、".join(f"{r.email}（{reason}）" for r, reason in unhealthy)
        issues.append(f"保活超期未刷新：{overdue}")
    if probe_errors:
        backing_off = "、".join(f"{r.email}（{reason}）" for r, reason in probe_errors)
        issues.append(f"保活退避中（暂可自愈，持续则升级）：{backing_off}")

    heartbeat_due = current_time.hour in HEARTBEAT_HOURS_UTC

    if dead or unhealthy:
        title = f"🩺 保活巡检：{n_active} 活 / {len(dead) + len(unhealthy)} 需介入"
        severity = "critical"
    elif probe_errors:
        title = f"🩺 保活巡检：{len(probe_errors)} 个号保活退避中"
        severity = "warning"
    elif heartbeat_due or force:
        title = f"🩺 保活巡检：{n_active} 个号全部存活"
        severity = "warning"
        issues.append("✅ 全部存活，无需人工介入。")
    else:
        return (False, None, None, None, None)

    description = (
        f"活跃账号 {n_active} 个，总额度 {total_credits}。\n"
        + "\n".join(issues)
        + f"\n巡检时刻 {current_time.strftime('%Y-%m-%d %H:%M UTC')}"
    )
    fields = [
        ("活跃", str(n_active), True),
        ("需维护", str(len(dead)), True),
        ("保活超期", str(len(unhealthy)), True),
        ("退避中", str(len(probe_errors)), True),
        ("总额度", str(total_credits), True),
    ]
    return (True, severity, title, description, fields)


async def main(argv=None) -> int:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument(
        "--force-report",
        action="store_true",
        help="跳过静默规则，立即投递当前状态（运维验收用）",
    )
    parsed = args.parse_args(argv)
    now = datetime.now(timezone.utc)

    n_active = -1
    try:
        active, dead, total_credits = read_state()
        lifecycle_rows = evaluate_lifecycle(now=now)
        n_active = len(active)
    except Exception as error:
        # 巡检自身失败必须可见——这是发现"DB/文件层 wedge"的最后一道。
        should_send, severity, title, description, fields = (
            True,
            "critical",
            "🩺 保活巡检：巡检自身失败",
            f"读取本地状态失败（{type(error).__name__}），保活状态未知。",
            None,
        )
    else:
        should_send, severity, title, description, fields = decide_report(
            active,
            dead,
            total_credits,
            lifecycle_rows,
            now=now,
            force=parsed.force_report,
        )

    if not should_send:
        print(
            f"[healthcheck] {now.isoformat()} all-healthy, silent "
            f"(heartbeat at {sorted(HEARTBEAT_HOURS_UTC)} UTC)"
        )
        return 0

    notifier = AlertNotifier(config.alert_webhook_url)
    ok = await notifier.send_alert(title, description, fields=fields, severity=severity)
    print(
        f"[healthcheck] {now.isoformat()} active={n_active} "
        f"severity={severity} delivered={ok}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
