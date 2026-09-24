"""摄入统计聚合：周报等跨日计算。

纯函数、不依赖 AstrBot，可独立单元测试。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def weekly_stats(state: dict[str, Any], today: str, days: int = 7) -> dict[str, Any]:
    """统计最近 ``days`` 天（含今天）的摄入数据。

    Args:
        state: 完整用户状态。
        today: 今天的 ISO 日期字符串（统计窗口的最后一天）。
        days: 统计窗口天数，函数内钳制到 1～30。

    Returns:
        dict 字段：
        - days：实际使用的窗口天数（钳制后）
        - start_date / end_date：窗口首尾 ISO 日期
        - per_day：[{date, total, recorded}]，从旧到新
        - total：窗口总摄入
        - daily_avg：自然日日均（未记录按 0 计）
        - recorded_days：有记录的天数
        - on_target_days：摄入 ≤ 目标的已记录天数
        - best_day：{date, total}，窗口内最高的一天；无记录时为 None
        - trend：up / down / flat（近 3 天日均 vs 此前均值，±10% 内视为持平）
        - recent_avg / earlier_avg：趋势两段的自然日均值
    """
    days = max(1, min(30, int(days)))
    end = datetime.fromisoformat(today).date()
    start = end - timedelta(days=days - 1)
    profile = state.get("profile") or {}
    try:
        target = int(profile.get("target", 0))
    except (TypeError, ValueError):
        target = 0

    # 按日累加窗口内的记录热量。
    totals: dict[str, int] = {}
    cursor = start
    while cursor <= end:
        totals[cursor.isoformat()] = 0
        cursor += timedelta(days=1)
    for entry in state.get("entries", []):
        entry_date = entry.get("date")
        if entry_date in totals:
            totals[entry_date] += int(entry.get("calories", 0))

    per_day = [
        {"date": d, "total": totals[d], "recorded": totals[d] > 0}
        for d in sorted(totals)
    ]
    recorded = [d for d in per_day if d["recorded"]]
    grand_total = sum(d["total"] for d in per_day)
    best_day = max(recorded, key=lambda d: d["total"]) if recorded else None

    # 趋势：近 3 天日均 vs 此前均值；窗口不足 4 天视为持平。
    recent_part = per_day[-3:]
    earlier_part = per_day[:-3]
    recent_avg = sum(d["total"] for d in recent_part) / len(recent_part)
    if earlier_part:
        earlier_avg = sum(d["total"] for d in earlier_part) / len(earlier_part)
        if recent_avg > earlier_avg * 1.1:
            trend = "up"
        elif recent_avg < earlier_avg * 0.9:
            trend = "down"
        else:
            trend = "flat"
    else:
        earlier_avg = recent_avg
        trend = "flat"

    return {
        "days": days,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "per_day": per_day,
        "total": grand_total,
        "daily_avg": round(grand_total / days),
        "target": target,
        "recorded_days": len(recorded),
        "on_target_days": sum(1 for d in recorded if d["total"] <= target),
        "best_day": {"date": best_day["date"], "total": best_day["total"]}
        if best_day
        else None,
        "trend": trend,
        "recent_avg": round(recent_avg),
        "earlier_avg": round(earlier_avg),
    }
