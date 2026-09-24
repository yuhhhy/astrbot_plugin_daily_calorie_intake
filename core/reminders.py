"""订阅提醒：每日总结与每周体重提示。

本模块只做纯逻辑（时间解析、到期判断、文案生成与投递标记计算），
不依赖 AstrBot；真正的投递由 ``main.py`` 的定时任务与到点补发处理器完成。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .constants import (
    DEFAULT_SUMMARY_TIME,
    DEFAULT_WEIGHT_CHECK_THRESHOLD,
    DEFAULT_WEIGHT_CHECK_WEEKDAY,
)

# 1 公斤脂肪约等于 7700 kcal，用于把累计缺口/超标换算成体重参考值
KCAL_PER_KG_FAT = 7700


def parse_summary_time(spec: str, default: str = DEFAULT_SUMMARY_TIME) -> str:
    """解析 ``HH:MM`` 形式的时间。

    Args:
        spec: 用户输入的时间，留空时使用默认值。
        default: 默认时间。

    Returns:
        规范化的 ``HH:MM`` 字符串（补零）。

    Raises:
        ValueError: 格式不合法或超出 00:00～23:59。
    """
    text = (spec or "").strip() or default
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError("时间格式应为 HH:MM，例如 21:00")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("时间需在 00:00～23:59 之间")
    return f"{hour:02d}:{minute:02d}"


def cron_expression(time_str: str) -> str:
    """把 ``HH:MM`` 转成每天执行的 cron 表达式（分 时 日 月 周）。"""
    hour, minute = time_str.split(":")
    return f"{int(minute)} {int(hour)} * * *"


def cron_timezone_name(offset: int) -> str | None:
    """把时区偏移小时数换算成 cron 可用的 IANA 名称。

    注意 ``Etc/GMT`` 系列的符号与常识相反：``Etc/GMT-8`` 表示 UTC+8。
    超出 -12～+14 时返回 ``None``（交给调度器使用默认时区）。
    """
    if 0 <= offset <= 14:
        return f"Etc/GMT-{offset}"
    if -12 <= offset < 0:
        return f"Etc/GMT+{-offset}"
    return None


def _time_tuple(time_str: str | None, default: str) -> tuple[int, int]:
    """把 ``HH:MM`` 解析为 (时, 分)；损坏时回退默认值。"""
    for candidate in (time_str, default):
        try:
            hour, minute = (int(part) for part in str(candidate).split(":"))
            return hour, minute
        except (AttributeError, TypeError, ValueError):
            continue
    return 21, 0


def is_summary_due(
    subscription: dict[str, Any], current: datetime, default_time: str
) -> bool:
    """判断今天的每日总结是否尚未投递且已到时间。"""
    if not subscription.get("enabled"):
        return False
    today = current.date().isoformat()
    if subscription.get("last_daily_date") == today:
        return False
    hour, minute = _time_tuple(subscription.get("time"), default_time)
    return (current.hour, current.minute) >= (hour, minute)


def is_weight_check_due(
    subscription: dict[str, Any],
    current: datetime,
    weekday: int,
    default_time: str = DEFAULT_SUMMARY_TIME,
) -> bool:
    """判断今天是否是该用户的每周体重检查日且今天还没提示过。

    ``weekday`` 采用 1=周一 … 7=周日的编号（与 WebUI 配置一致）。
    与每日总结共用同一时间窗（到点后才提示），避免清晨打扰。
    """
    if not subscription.get("enabled"):
        return False
    if current.isoweekday() != weekday:
        return False
    if subscription.get("last_weight_check") == current.date().isoformat():
        return False
    hour, minute = _time_tuple(subscription.get("time"), default_time)
    return (current.hour, current.minute) >= (hour, minute)


def _today_total(state: dict[str, Any], today: str) -> tuple[int, list[dict[str, Any]]]:
    """返回今天的总摄入与今天的记录列表。"""
    entries = [entry for entry in state["entries"] if entry.get("date") == today]
    return sum(int(entry.get("calories", 0)) for entry in entries), entries


def build_daily_summary(state: dict[str, Any], today: str) -> str:
    """生成每日总结文案（纯文本，适合推送）。"""
    target = int(state["profile"]["target"])
    total, entries = _today_total(state, today)
    header = f"🌙 今日饮食总结（{today[5:]}）"
    if not entries:
        return f"{header}\n今天还没有饮食记录，别忘了记录哦～"
    remaining = target - total
    status = (
        f"还可摄入约 {remaining} kcal"
        if remaining >= 0
        else f"已超过目标约 {-remaining} kcal"
    )
    lines = [
        header,
        f"已记录 {len(entries)} 笔，累计 {total} kcal（目标 {target} kcal），{status}。",
    ]
    protein = sum(float(e.get("protein") or 0) for e in entries)
    carbs = sum(float(e.get("carbs") or 0) for e in entries)
    fat = sum(float(e.get("fat") or 0) for e in entries)
    if (protein, carbs, fat) != (0, 0, 0):
        lines.append(f"蛋白质 {protein:g}g／碳水 {carbs:g}g／脂肪 {fat:g}g。")
    # 单条明细：描述 + 热量，最多 8 条，过多时省略。
    detail = "｜".join(
        f"{str(entry.get('description', '饮食记录'))[:12]} {entry.get('calories', 0)} kcal"
        for entry in entries[:8]
    )
    if len(entries) > 8:
        detail += f"｜…等 {len(entries)} 笔"
    lines.append(detail)
    return "\n".join(lines)


def build_weight_nudge(state: dict[str, Any], today: str, threshold: int) -> str | None:
    """生成每周体重更新提示；累计缺口/超标未达阈值时返回 None。

    统计口径：近 7 天总摄入 − 目标 × 7，负值为缺口、正值为超标。
    """
    from .stats import weekly_stats

    stats = weekly_stats(state, today, 7)
    if stats["recorded_days"] == 0:
        return None
    net = stats["total"] - stats["target"] * stats["days"]
    if abs(net) < threshold:
        return None
    kg = abs(net) / KCAL_PER_KG_FAT
    if net < 0:
        head = f"⚖️ 体重提醒：近 7 天累计缺口约 {-net} kcal（约合 {kg:.1f} kg 脂肪）"
    else:
        head = f"⚖️ 体重提醒：近 7 天累计超标约 {net} kcal（约合 {kg:.1f} kg 脂肪）"
    return (
        f"{head}，体重可能已有变化。\n"
        "想更新体重的话，发送 /热量 配置 并回复「体重」即可（会自动重算每日目标）。"
    )


def compose_due_message(
    state: dict[str, Any],
    current: datetime,
    *,
    default_time: str = DEFAULT_SUMMARY_TIME,
    weekday: int = DEFAULT_WEIGHT_CHECK_WEEKDAY,
    threshold: int = DEFAULT_WEIGHT_CHECK_THRESHOLD,
) -> tuple[str | None, bool, bool]:
    """组装本次到期需要投递的内容。

    Returns:
        （待发送文本或 None, 是否已投递每日总结, 是否已做每周体重检查）。
        文本为 None 表示本次无需投递。
    """
    subscription = state.get("subscription") or {}
    summary_due = is_summary_due(subscription, current, default_time)
    weight_due = is_weight_check_due(subscription, current, weekday, default_time)
    if not summary_due and not weight_due:
        return None, False, False
    if not state.get("profile"):
        # 没有档案无法生成任何内容；标记为已检查避免反复触发。
        return None, summary_due, weight_due

    today = current.date().isoformat()
    parts: list[str] = []
    if summary_due:
        parts.append(build_daily_summary(state, today))
    if weight_due:
        nudge = build_weight_nudge(state, today, threshold)
        if nudge:
            parts.append(nudge)
    text = "\n\n".join(parts) if parts else None
    return text, summary_due, weight_due
