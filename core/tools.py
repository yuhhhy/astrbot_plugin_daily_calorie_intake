"""LLM 工具的业务逻辑。

``main.py`` 中 ``@filter.llm_tool`` 装饰的 handler 只保留工具的
docstring（注册时读取）并做一行委托；具体逻辑在本模块实现。
所有函数依赖显式注入的 ``store`` 与 ``now``，不依赖插件实例。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .constants import (
    BACKFILL_MAX_DAYS,
    CONFIDENCE_LEVELS,
    MAX_CALORIES,
    MAX_DESCRIPTION_LENGTH,
    MIN_CALORIES,
)
from .llm_parsing import coerce_int, normalize_macro_grams
from .state_store import resolve_date
from .stats import weekly_stats

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .state_store import UserStateStore

# “配置时区当前时间”可调用对象的类型别名
CallableNow = Callable[[], datetime]


async def record_calorie(
    event: AstrMessageEvent,
    *,
    store: UserStateStore,
    now: CallableNow,
    description: str,
    calories: Any,
    protein: Any,
    carbs: Any,
    fat: Any,
    lower_bound: Any,
    upper_bound: Any,
    confidence: str,
    date: str,
) -> str:
    """记录一笔饮食热量与宏量营养素（record_daily_calorie_intake 工具）。"""
    clean_description = (description or "").strip()[
        :MAX_DESCRIPTION_LENGTH
    ] or "饮食记录"
    # 容忍模型传来的浮点/字符串数字；非法时直接拒绝本次记录。
    try:
        calories = coerce_int(calories)
    except ValueError:
        return "calories 必须是 1～10000 之间的整数，本次未记录。"
    if not MIN_CALORIES <= calories <= MAX_CALORIES:
        return "calories 必须是 1～10000 之间的整数，本次未记录。"
    # 区间缺省时收敛为点估计；非法区间钳制为 [calories, calories]。
    try:
        lower = coerce_int(lower_bound) if lower_bound else calories
        upper = coerce_int(upper_bound) if upper_bound else calories
    except ValueError:
        lower = upper = calories
    lower = min(max(0, lower), calories)
    upper = max(calories, min(MAX_CALORIES, upper))
    conf = (confidence or "").strip().lower()
    if conf not in CONFIDENCE_LEVELS:
        conf = "low"
    # 营养素是补充信息：解析失败按 0 处理，不让记录失败。
    protein_g = normalize_macro_grams(protein)
    carbs_g = normalize_macro_grams(carbs)
    fat_g = normalize_macro_grams(fat)

    current = now()
    # 解析记录日期（支持补记）；未来日期与超出回溯窗口的日期一律拒绝。
    try:
        entry_date = resolve_date(date, current)
    except ValueError:
        return "date 无法识别。请使用 today、yesterday、前天或 YYYY-MM-DD。"
    days_ago = (current.date() - datetime.fromisoformat(entry_date).date()).days
    if days_ago < 0:
        return "不能记录未来日期的饮食，date 请改为今天或更早。"
    if days_ago > BACKFILL_MAX_DAYS:
        return f"仅支持补记最近 {BACKFILL_MAX_DAYS} 天内的记录，date 请改为更近的日期。"

    entry = {
        "id": uuid.uuid4().hex[:8],
        "date": entry_date,
        "created_at": current.isoformat(),
        "description": clean_description,
        "calories": calories,
        "protein": protein_g,
        "carbs": carbs_g,
        "fat": fat_g,
        "lower_bound": lower,
        "upper_bound": upper,
        "confidence": conf,
        "source": "text",
    }

    def mutate(state: dict[str, Any]) -> None:
        state["entries"].append(entry)

    state, _ = await store.update(event, mutate)

    date_, total, remaining = store.date_summary(state, entry_date)
    remaining_text = (
        f"还可摄入约 {remaining} kcal"
        if remaining >= 0
        else f"已超过目标约 {-remaining} kcal"
    )
    # 有营养素估算时附带返回，供模型向用户播报。
    macro_text = (
        f"，蛋白质 {protein_g:g}g／碳水 {carbs_g:g}g／脂肪 {fat_g:g}g"
        if (protein_g, carbs_g, fat_g) != (0, 0, 0)
        else ""
    )
    # 返回给模型的文本会进入下一轮 prompt，由模型组织成对用户的回复。
    return (
        f"已记录：{clean_description}，约 {calories} kcal{macro_text}"
        f"（区间 {lower}～{upper}，可信度 {conf}），记录 ID {entry['id']}。"
        f"{date_} 当日累计 {total} kcal，目标 {state['profile']['target']} kcal，"
        f"{remaining_text}。"
    )


async def list_records(
    event: AstrMessageEvent,
    *,
    store: UserStateStore,
    now: CallableNow,
    date: str,
) -> str:
    """列出指定日期的热量记录（list_daily_calorie_records 工具）。"""
    state = await store.load(event)
    if not state["profile"]:
        return "用户尚未建立热量档案。"
    # 日期说法统一解析：today/今天、yesterday/昨天、前天 或 YYYY-MM-DD。
    try:
        date = resolve_date(date, now())
    except ValueError:
        return "日期无法识别。请使用 today、yesterday、前天或 YYYY-MM-DD。"
    entries = [entry for entry in state["entries"] if entry.get("date") == date]
    if not entries:
        return f"{date} 没有热量记录。"
    return "\n".join(
        f"{number}. id={entry['id']}；{entry.get('description', '饮食记录')}；"
        f"{entry.get('calories', 0)} kcal"
        for number, entry in enumerate(entries, start=1)
    )


async def weekly_stats_summary(
    event: AstrMessageEvent,
    *,
    store: UserStateStore,
    now: CallableNow,
    days: Any,
) -> str:
    """汇总最近若干天的摄入统计（get_weekly_stats 工具）。"""
    state = await store.load(event)
    if not state["profile"]:
        return "用户尚未建立热量档案。"
    # 天数容错：无法解析时回退 7 天，函数内再钳制到 1～30。
    try:
        days = coerce_int(days)
    except ValueError:
        days = 7

    stats = weekly_stats(state, now().date().isoformat(), days)
    if stats["recorded_days"] == 0:
        return (
            f"最近 {stats['days']} 天（{stats['start_date']} ～ "
            f"{stats['end_date']}）没有热量记录。"
        )
    trend_text = {
        "up": "近几天比之前吃得更多",
        "down": "近几天比之前吃得更少",
        "flat": "摄入量基本持平",
    }[stats["trend"]]
    best_text = ""
    if stats["best_day"]:
        best_text = (
            f"，最高的一天是 {stats['best_day']['date']}"
            f"（{stats['best_day']['total']} kcal）"
        )
    return (
        f"最近 {stats['days']} 天（{stats['start_date']} ～ {stats['end_date']}）"
        f"累计 {stats['total']} kcal，日均 {stats['daily_avg']} kcal，"
        f"目标 {stats['target']} kcal；记录 {stats['recorded_days']}/{stats['days']} 天，"
        f"达标 {stats['on_target_days']} 天（当日摄入不超过目标）{best_text}；"
        f"趋势：{trend_text}"
        f"（近 3 天日均 {stats['recent_avg']} kcal，此前日均 {stats['earlier_avg']} kcal）。"
    )


async def undo_record(
    event: AstrMessageEvent,
    *,
    store: UserStateStore,
    now: CallableNow,
    selector: str,
    date: str,
) -> str:
    """撤销一笔明确指定的热量记录（undo_daily_calorie_record 工具）。"""
    selector = (selector or "").strip()
    if not selector:
        return "未指定要撤销的记录。请先调用 list_daily_calorie_records。"
    # 日期说法统一解析：today/今天、yesterday/昨天、前天 或 YYYY-MM-DD。
    try:
        date = resolve_date(date, now())
    except ValueError:
        return "日期无法识别。请使用 today、yesterday、前天或 YYYY-MM-DD。"

    removed_holder: dict[str, Any] = {}

    def mutate(state: dict[str, Any]) -> None:
        normalized = selector.strip().lstrip("#")
        # 与命令版撤销相同：纯描述命中多条时不盲删，要求先用 ID 定位。
        is_id = any(
            entry.get("id") == normalized and entry.get("date") == date
            for entry in state["entries"]
        )
        if not normalized.isdigit() and not is_id:
            matches = [
                entry
                for entry in state["entries"]
                if entry.get("date") == date
                and normalized in str(entry.get("description", ""))
            ]
            if len(matches) > 1:
                removed_holder["error"] = (
                    "描述匹配到多条记录，不能确定要撤销哪一条。"
                    "请先调用 list_daily_calorie_records，再使用记录 ID。"
                )
                return
        index = store.find_entry_index(state, selector, date)
        if index is None:
            removed_holder["error"] = (
                "没有找到对应记录。请先调用 list_daily_calorie_records 定位。"
            )
            return
        removed_holder["removed"] = state["entries"].pop(index)

    _, result = await store.update(event, mutate)
    if "error" in removed_holder:
        return removed_holder["error"]
    removed = removed_holder["removed"]
    return (
        f"已撤销记录 {removed.get('id')}："
        f"{removed.get('description', '饮食记录')}，"
        f"{removed.get('calories', 0)} kcal。"
    )
