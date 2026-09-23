from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

ACTIVITY_LEVELS = {
    "1": ("久坐（很少运动）", 1.2),
    "2": ("轻量活动（每周运动 1～3 天）", 1.375),
    "3": ("中等活动（每周运动 3～5 天）", 1.55),
    "4": ("高强度活动（每周运动 6～7 天）", 1.725),
    "5": ("非常高强度活动或体力工作", 1.9),
}

GOALS = {"1": "减重", "2": "维持", "3": "增重"}


def calculate_targets(
    age: int,
    height_cm: float,
    weight_kg: float,
    sex: str,
    activity_factor: float,
    goal: str,
) -> tuple[int, int]:
    """Calculate estimated daily energy expenditure and calorie target.

    Args:
        age: Age in complete years.
        height_cm: Height in centimetres.
        weight_kg: Weight in kilograms.
        sex: Biological sex used by the Mifflin-St Jeor equation.
        activity_factor: Activity multiplier applied to basal metabolism.
        goal: One of ``减重``, ``维持``, or ``增重``.

    Returns:
        Rounded TDEE and rounded daily calorie target.

    Raises:
        ValueError: If ``sex`` or ``goal`` is unsupported.
    """
    if sex == "男":
        sex_constant = 5
    elif sex == "女":
        sex_constant = -161
    elif sex == "不提供":
        sex_constant = -78
    else:
        raise ValueError("Unsupported biological sex")

    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + sex_constant
    tdee = round(bmr * activity_factor)
    if goal == "减重":
        target = max(round(bmr), tdee - 400)
    elif goal == "增重":
        target = tdee + 250
    elif goal == "维持":
        target = tdee
    else:
        raise ValueError("Unsupported goal")
    return tdee, target


def parse_food_analysis(text: str) -> dict[str, Any]:
    """Parse and validate the model's food analysis JSON.

    Args:
        text: Raw model response.

    Returns:
        Normalized food analysis.

    Raises:
        ValueError: If the response is not valid food-analysis JSON.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("The model did not return a JSON object")
    payload = json.loads(match.group(0))
    if not isinstance(payload.get("is_food"), bool):
        raise ValueError("Missing is_food")
    if not payload["is_food"]:
        return {"is_food": False}

    calories = int(payload["calories"])
    lower = int(payload.get("lower_bound", calories))
    upper = int(payload.get("upper_bound", calories))
    if not 1 <= calories <= 10000:
        raise ValueError("Calories are outside the supported range")
    if lower > calories or upper < calories:
        raise ValueError("Invalid calorie range")
    confidence = str(payload.get("confidence", "low")).lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return {
        "is_food": True,
        "description": str(payload.get("description", "食物图片"))[:200],
        "calories": calories,
        "lower_bound": max(0, lower),
        "upper_bound": min(10000, upper),
        "confidence": confidence,
        "notes": str(payload.get("notes", ""))[:300],
    }


@register(
    "astrbot_plugin_daily_calorie_intake",
    "yuhhhy",
    "通过多模态模型估算并记录每日热量摄入",
    "1.1.0",
)
class DailyCalorieIntakePlugin(Star):
    """Track per-user calorie targets and food-image estimates."""

    def __init__(self, context: Context):
        super().__init__(context)
        self._locks: dict[str, asyncio.Lock] = {}

    def _state_key(self, event: AstrMessageEvent) -> str:
        """Build a stable, privacy-preserving per-user storage key.

        Args:
            event: Current message event.

        Returns:
            Plugin KV key for the sender.
        """
        sender = event.get_sender_id() or event.unified_msg_origin
        identity = f"{event.get_platform_id()}:{sender}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
        return f"user:{digest}"

    async def _load_state(self, event: AstrMessageEvent) -> dict[str, Any]:
        """Load normalized state for the current sender.

        Args:
            event: Current message event.

        Returns:
            Mutable user state dictionary.
        """
        state = await self.get_kv_data(self._state_key(event), {})
        if not isinstance(state, dict):
            state = {}
        state.setdefault("profile", None)
        if "recording_enabled" not in state:
            state["recording_enabled"] = True
        state.pop("auto_record", None)
        state.setdefault("entries", [])
        state.pop("auto_mode", None)
        state.pop("pending", None)
        for entry in state["entries"]:
            if not entry.get("id"):
                seed = (
                    f"{entry.get('created_at', '')}:{entry.get('description', '')}:"
                    f"{entry.get('calories', 0)}"
                )
                entry["id"] = hashlib.sha256(seed.encode()).hexdigest()[:8]
        return state

    async def _save_state(self, event: AstrMessageEvent, state: dict[str, Any]) -> None:
        """Persist state for the current sender.

        Args:
            event: Current message event.
            state: Complete user state.
        """
        await self.put_kv_data(self._state_key(event), state)

    def _today_summary(self, state: dict[str, Any]) -> tuple[str, int, int]:
        """Create today's summary from stored entries.

        Args:
            state: Complete user state.

        Returns:
            Local date, total intake, and remaining target.
        """
        date = datetime.now().astimezone().date().isoformat()
        total = sum(
            int(entry.get("calories", 0))
            for entry in state["entries"]
            if entry.get("date") == date
        )
        target = int(state["profile"]["target"])
        return date, total, target - total

    def _find_entry_index(
        self, state: dict[str, Any], selector: str, date: str | None = None
    ) -> int | None:
        """Resolve a record selector to its index in the full entry list.

        Args:
            state: Complete user state.
            selector: Record ID, displayed number, description text, or ``最近``.
            date: Optional ISO date limiting number and description matching.

        Returns:
            Index in ``state['entries']``, or ``None`` when no record matches.
        """
        candidates = [
            (index, entry)
            for index, entry in enumerate(state["entries"])
            if date is None or entry.get("date") == date
        ]
        if not candidates:
            return None
        selector = selector.strip().lstrip("#")
        if selector in {"", "最近", "最近一次", "最后", "上一条"}:
            return candidates[-1][0]
        for index, entry in reversed(candidates):
            if entry.get("id") == selector:
                return index
        if selector.isdigit():
            displayed_index = int(selector) - 1
            if 0 <= displayed_index < len(candidates):
                return candidates[displayed_index][0]
        for index, entry in reversed(candidates):
            if selector in str(entry.get("description", "")):
                return index
        return None

    @filter.command_group("热量")
    def calorie():
        """管理每日热量目标和饮食记录。"""

    @calorie.command("开始")
    async def start_profile(self, event: AstrMessageEvent):
        """通过多轮问答创建或重建个人热量档案。"""
        prompts = [
            "请输入年龄（仅支持 18～100 岁的成年人）：",
            "请输入身高，单位厘米，例如 175：",
            "请输入体重，单位公斤，例如 68.5：",
            "请选择用于代谢公式的生理性别：男 / 女 / 不提供：",
            (
                "请选择日常活动水平：\n"
                "1. 久坐\n2. 轻量活动\n3. 中等活动\n"
                "4. 高强度活动\n5. 非常高强度活动或体力工作"
            ),
            "请选择目标：1. 减重  2. 维持  3. 增重",
        ]
        answers: dict[str, Any] = {}
        step = 0
        yield event.plain_result(
            "我们先建立个人档案。随时发送“退出”可以取消。\n\n" + prompts[0]
        )

        @session_waiter(timeout=180, record_history_chains=False)
        async def profile_waiter(
            controller: SessionController, next_event: AstrMessageEvent
        ) -> None:
            nonlocal step
            value = next_event.message_str.strip()
            if value == "退出":
                await next_event.send(next_event.plain_result("已取消建立档案。"))
                controller.stop()
                return

            try:
                if step == 0:
                    parsed = int(value)
                    if not 18 <= parsed <= 100:
                        raise ValueError
                    answers["age"] = parsed
                elif step == 1:
                    parsed = float(value)
                    if not 120 <= parsed <= 230:
                        raise ValueError
                    answers["height_cm"] = parsed
                elif step == 2:
                    parsed = float(value)
                    if not 30 <= parsed <= 300:
                        raise ValueError
                    answers["weight_kg"] = parsed
                elif step == 3:
                    aliases = {"男性": "男", "女性": "女", "跳过": "不提供"}
                    parsed = aliases.get(value, value)
                    if parsed not in {"男", "女", "不提供"}:
                        raise ValueError
                    answers["sex"] = parsed
                elif step == 4:
                    if value not in ACTIVITY_LEVELS:
                        raise ValueError
                    answers["activity_level"] = ACTIVITY_LEVELS[value][0]
                    answers["activity_factor"] = ACTIVITY_LEVELS[value][1]
                elif step == 5:
                    if value not in GOALS:
                        raise ValueError
                    answers["goal"] = GOALS[value]
            except (TypeError, ValueError):
                await next_event.send(
                    next_event.plain_result(
                        "输入格式不正确，请重新输入。\n" + prompts[step]
                    )
                )
                controller.keep(timeout=180, reset_timeout=True)
                return

            step += 1
            if step < len(prompts):
                await next_event.send(next_event.plain_result(prompts[step]))
                controller.keep(timeout=180, reset_timeout=True)
                return

            tdee, target = calculate_targets(
                answers["age"],
                answers["height_cm"],
                answers["weight_kg"],
                answers["sex"],
                answers["activity_factor"],
                answers["goal"],
            )
            answers["tdee"] = tdee
            answers["target"] = target
            answers["updated_at"] = datetime.now().astimezone().isoformat()
            state = await self._load_state(next_event)
            state["profile"] = answers
            await self._save_state(next_event, state)
            sex_note = (
                "\n你选择了不提供生理性别，因此结果采用两种公式的中间估值。"
                if answers["sex"] == "不提供"
                else ""
            )
            await next_event.send(
                next_event.plain_result(
                    f"档案已保存。估算每日总消耗约 {tdee} kcal，"
                    f"{answers['goal']}目标为 {target} kcal/天。{sex_note}\n"
                    "热量记录已默认开启，发送食物图片会自动分析并入账。\n"
                    "这是日常管理估算，不替代医生或营养师建议。"
                )
            )
            controller.stop()

        try:
            await profile_waiter(event)
        except TimeoutError:
            yield event.plain_result("建立档案已超时，请发送 /热量 开始 重新填写。")
        finally:
            event.stop_event()

    @calorie.command("配置")
    async def configure(self, event: AstrMessageEvent):
        """通过对话查看和局部修改档案，修改后自动重算目标。"""
        state = await self._load_state(event)
        profile = state["profile"]
        if not profile:
            yield event.plain_result("尚未建立档案，请先发送 /热量 开始。")
            return
        yield event.plain_result(
            "当前档案：\n"
            f"年龄：{profile['age']} 岁\n"
            f"身高：{profile['height_cm']} cm\n"
            f"体重：{profile['weight_kg']} kg\n"
            f"生理性别：{profile['sex']}\n"
            f"活动水平：{profile['activity_level']}\n"
            f"目标：{profile['goal']}\n"
            f"估算 TDEE：{profile['tdee']} kcal\n"
            f"每日目标：{profile['target']} kcal\n\n"
            "你想修改哪一项？可以回复：年龄、身高、体重、性别、活动或目标。\n"
            "修改完成后回复“完成”。"
        )
        selected_field: str | None = None
        value_prompts = {
            "年龄": "请输入新年龄（18～100）：",
            "身高": "请输入新身高，单位厘米（120～230）：",
            "体重": "请输入新体重，单位公斤（30～300）：",
            "性别": "请输入用于代谢公式的生理性别：男 / 女 / 不提供：",
            "活动": (
                "请选择新活动水平：\n"
                "1. 久坐\n2. 轻量活动\n3. 中等活动\n"
                "4. 高强度活动\n5. 非常高强度活动或体力工作"
            ),
            "目标": "请选择新目标：1. 减重  2. 维持  3. 增重",
        }

        @session_waiter(timeout=180, record_history_chains=False)
        async def config_waiter(
            controller: SessionController, next_event: AstrMessageEvent
        ) -> None:
            nonlocal selected_field
            value = next_event.message_str.strip()
            if value in {"完成", "退出", "取消"}:
                await next_event.send(next_event.plain_result("配置已结束。"))
                controller.stop()
                return

            if selected_field is None:
                aliases = {
                    "age": "年龄",
                    "height": "身高",
                    "weight": "体重",
                    "sex": "性别",
                    "activity": "活动",
                    "goal": "目标",
                }
                normalized = aliases.get(value.lower(), value)
                if normalized not in value_prompts:
                    matches = [field for field in value_prompts if field in value]
                    normalized = matches[0] if len(matches) == 1 else ""
                if normalized not in value_prompts:
                    await next_event.send(
                        next_event.plain_result(
                            "我没识别出要修改的项目。请回复："
                            "年龄、身高、体重、性别、活动或目标。"
                        )
                    )
                    controller.keep(timeout=180, reset_timeout=True)
                    return
                selected_field = normalized
                await next_event.send(
                    next_event.plain_result(value_prompts[selected_field])
                )
                controller.keep(timeout=180, reset_timeout=True)
                return

            try:
                if selected_field == "年龄":
                    parsed = int(value)
                    if not 18 <= parsed <= 100:
                        raise ValueError
                    profile["age"] = parsed
                elif selected_field == "身高":
                    parsed = float(value)
                    if not 120 <= parsed <= 230:
                        raise ValueError
                    profile["height_cm"] = parsed
                elif selected_field == "体重":
                    parsed = float(value)
                    if not 30 <= parsed <= 300:
                        raise ValueError
                    profile["weight_kg"] = parsed
                elif selected_field == "性别":
                    sex_aliases = {"男性": "男", "女性": "女", "跳过": "不提供"}
                    parsed = sex_aliases.get(value, value)
                    if parsed not in {"男", "女", "不提供"}:
                        raise ValueError
                    profile["sex"] = parsed
                elif selected_field == "活动":
                    if value not in ACTIVITY_LEVELS:
                        raise ValueError
                    profile["activity_level"] = ACTIVITY_LEVELS[value][0]
                    profile["activity_factor"] = ACTIVITY_LEVELS[value][1]
                elif selected_field == "目标":
                    parsed = GOALS.get(value, value)
                    if parsed not in GOALS.values():
                        raise ValueError
                    profile["goal"] = parsed
            except (TypeError, ValueError):
                await next_event.send(
                    next_event.plain_result(
                        "输入值无效，请重新输入。\n" + value_prompts[selected_field]
                    )
                )
                controller.keep(timeout=180, reset_timeout=True)
                return

            changed_field = selected_field
            tdee, target = calculate_targets(
                profile["age"],
                profile["height_cm"],
                profile["weight_kg"],
                profile["sex"],
                profile["activity_factor"],
                profile["goal"],
            )
            profile["tdee"] = tdee
            profile["target"] = target
            profile["updated_at"] = datetime.now().astimezone().isoformat()
            state["profile"] = profile
            await self._save_state(next_event, state)
            selected_field = None
            await next_event.send(
                next_event.plain_result(
                    f"已更新{changed_field}并自动重算：TDEE 约 {tdee} kcal，"
                    f"每日{profile['goal']}目标 {target} kcal。\n\n"
                    "还想修改哪一项？回复年龄、身高、体重、性别、活动或目标；"
                    "回复“完成”结束。"
                )
            )
            controller.keep(timeout=180, reset_timeout=True)

        try:
            await config_waiter(event)
        except TimeoutError:
            yield event.plain_result("配置对话已超时；已完成的修改均已保存。")
        finally:
            event.stop_event()

    @calorie.command("今天")
    async def today(self, event: AstrMessageEvent):
        """查看今日摄入和剩余热量。"""
        state = await self._load_state(event)
        if not state["profile"]:
            yield event.plain_result("请先发送 /热量 开始 建立个人档案。")
            return
        date, total, remaining = self._today_summary(state)
        status = (
            f"还可摄入约 {remaining} kcal"
            if remaining >= 0
            else f"已超过目标约 {-remaining} kcal"
        )
        entries = [entry for entry in state["entries"] if entry.get("date") == date]
        details = "\n".join(
            f"{number}. #{entry['id']} {entry.get('description', '饮食记录')} "
            f"{entry.get('calories', 0)} kcal"
            for number, entry in enumerate(entries, start=1)
        )
        yield event.plain_result(
            f"{date} 已记录 {total} kcal，目标 {state['profile']['target']} kcal，"
            f"{status}。"
            + (f"\n\n今日明细：\n{details}" if details else "\n今天还没有饮食记录。")
        )

    @calorie.command("记录")
    async def record(self, event: AstrMessageEvent, calories: int):
        """手动记录一笔热量。"""
        if not 1 <= calories <= 10000:
            yield event.plain_result("单次热量请输入 1～10000 之间的整数。")
            return
        key = self._state_key(event)
        async with self._locks.setdefault(key, asyncio.Lock()):
            state = await self._load_state(event)
            if not state["profile"]:
                yield event.plain_result("请先发送 /热量 开始 建立个人档案。")
                return
            if not state["recording_enabled"]:
                yield event.plain_result(
                    "热量记录当前已关闭。发送 /自动记录 开启 后再记录。"
                )
                return
            now = datetime.now().astimezone()
            state["entries"].append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "date": now.date().isoformat(),
                    "created_at": now.isoformat(),
                    "description": "手动记录",
                    "calories": calories,
                    "source": "manual",
                }
            )
            state["entries"] = state["entries"][-1000:]
            await self._save_state(event, state)
            _, total, remaining = self._today_summary(state)
        yield event.plain_result(
            f"已记录 {calories} kcal。今日累计 {total} kcal，"
            + (
                f"还可摄入约 {remaining} kcal。"
                if remaining >= 0
                else f"已超过目标约 {-remaining} kcal。"
            )
        )

    @calorie.command("撤销")
    async def undo(self, event: AstrMessageEvent):
        """列出今天的记录，并通过后续对话选择要撤销的一笔。"""
        state = await self._load_state(event)
        today = datetime.now().astimezone().date().isoformat()
        entries = [entry for entry in state["entries"] if entry.get("date") == today]
        if not entries:
            yield event.plain_result("今天还没有可撤销的热量记录。")
            return
        yield event.plain_result(
            "请选择要撤销的记录：\n"
            + "\n".join(
                f"{number}. #{entry['id']} "
                f"{entry.get('description', '饮食记录')} "
                f"{entry.get('calories', 0)} kcal"
                for number, entry in enumerate(entries, start=1)
            )
            + "\n\n请回复编号、记录 ID 或明确描述；回复“取消”退出。"
        )

        @session_waiter(timeout=120, record_history_chains=False)
        async def undo_waiter(
            controller: SessionController, next_event: AstrMessageEvent
        ) -> None:
            selector = next_event.message_str.strip()
            if selector in {"取消", "退出"}:
                await next_event.send(next_event.plain_result("已取消撤销。"))
                controller.stop()
                return

            key = self._state_key(next_event)
            async with self._locks.setdefault(key, asyncio.Lock()):
                current_state = await self._load_state(next_event)
                normalized = selector.lstrip("#")
                is_id = any(
                    entry.get("id") == normalized and entry.get("date") == today
                    for entry in current_state["entries"]
                )
                if not normalized.isdigit() and not is_id:
                    matches = [
                        entry
                        for entry in current_state["entries"]
                        if entry.get("date") == today
                        and normalized in str(entry.get("description", ""))
                    ]
                    if len(matches) > 1:
                        await next_event.send(
                            next_event.plain_result(
                                "这个描述匹配到多条记录，请回复对应编号或记录 ID。"
                            )
                        )
                        controller.keep(timeout=120, reset_timeout=True)
                        return
                index = self._find_entry_index(current_state, selector, today)
                if index is None:
                    await next_event.send(
                        next_event.plain_result(
                            "没有找到对应记录，请回复列表中的编号、记录 ID 或描述。"
                        )
                    )
                    controller.keep(timeout=120, reset_timeout=True)
                    return
                removed = current_state["entries"].pop(index)
                await self._save_state(next_event, current_state)

            await next_event.send(
                next_event.plain_result(
                    f"已撤销：{removed.get('description', '饮食记录')}，"
                    f"{removed.get('calories', 0)} kcal。"
                )
            )
            controller.stop()

        try:
            await undo_waiter(event)
        except TimeoutError:
            yield event.plain_result("撤销选择已超时，没有删除任何记录。")
        finally:
            event.stop_event()

    @filter.command("自动记录")
    async def auto_record(self, event: AstrMessageEvent, action: str = "状态"):
        """开启或关闭整个插件的新增热量记录功能。"""
        state = await self._load_state(event)
        if action in {"开启", "开"}:
            if not state["profile"]:
                yield event.plain_result("请先发送 /热量 开始 建立个人档案。")
                return
            state["recording_enabled"] = True
            await self._save_state(event, state)
            yield event.plain_result(
                "热量记录已开启。手动记录和食物图片分析均可使用；"
                "食物图片识别成功后会自动入账。"
            )
        elif action in {"关闭", "关"}:
            state["recording_enabled"] = False
            await self._save_state(event, state)
            yield event.plain_result(
                "热量记录已关闭。不会新增手动记录，也不会分析或记录食物图片；"
                "已有记录仍可查询和撤销。"
            )
        elif action == "状态":
            enabled = "已开启" if state["recording_enabled"] else "已关闭"
            yield event.plain_result(
                f"热量记录{enabled}；开启时食物图片会自动分析并直接入账。"
            )
        else:
            yield event.plain_result(
                "用法：/自动记录 开启、/自动记录 关闭、/自动记录 状态"
            )

    @filter.llm_tool(name="list_daily_calorie_records")
    async def list_records_tool(self, event: AstrMessageEvent, date: str) -> str:
        """列出指定日期的热量记录，供查询或撤销前定位记录。

        Args:
            date(string): 日期，使用 today 表示今天，或使用 YYYY-MM-DD。
        """
        state = await self._load_state(event)
        if not state["profile"]:
            return "用户尚未建立热量档案。"
        if date.lower() in {"today", "今天"}:
            date = datetime.now().astimezone().date().isoformat()
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return "日期格式无效，请使用 today 或 YYYY-MM-DD。"
        entries = [entry for entry in state["entries"] if entry.get("date") == date]
        if not entries:
            return f"{date} 没有热量记录。"
        return "\n".join(
            f"{number}. id={entry['id']}；{entry.get('description', '饮食记录')}；"
            f"{entry.get('calories', 0)} kcal"
            for number, entry in enumerate(entries, start=1)
        )

    @filter.llm_tool(name="undo_daily_calorie_record")
    async def undo_record_tool(
        self, event: AstrMessageEvent, selector: str, date: str
    ) -> str:
        """撤销用户明确指定的一条热量记录，含糊时应先列出记录。

        Args:
            selector(string): 记录 ID、当天显示编号或足以唯一定位的描述。
            date(string): 记录日期，使用 today 表示今天，或使用 YYYY-MM-DD。
        """
        if date.lower() in {"today", "今天"}:
            date = datetime.now().astimezone().date().isoformat()
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return "日期格式无效，请使用 today 或 YYYY-MM-DD。"
        key = self._state_key(event)
        async with self._locks.setdefault(key, asyncio.Lock()):
            state = await self._load_state(event)
            normalized = selector.strip().lstrip("#")
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
                    return (
                        "描述匹配到多条记录，不能确定要撤销哪一条。"
                        "请先调用 list_daily_calorie_records，再使用记录 ID。"
                    )
            index = self._find_entry_index(state, selector, date)
            if index is None:
                return "没有找到对应记录。请先调用 list_daily_calorie_records 定位。"
            removed = state["entries"].pop(index)
            await self._save_state(event, state)
        return (
            f"已撤销记录 {removed.get('id')}："
            f"{removed.get('description', '饮食记录')}，"
            f"{removed.get('calories', 0)} kcal。"
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def listen_for_food_images(self, event: AstrMessageEvent):
        """Analyze and record food images while calorie recording is enabled."""
        state = await self._load_state(event)
        if not state["recording_enabled"] or not state["profile"]:
            return

        images = [
            component
            for component in event.get_messages()
            if isinstance(component, Comp.Image)
        ]
        if not images:
            return

        source_message_id = str(getattr(event.message_obj, "message_id", ""))
        if source_message_id and (
            any(
                entry.get("source_message_id") == source_message_id
                for entry in state["entries"]
            )
        ):
            event.stop_event()
            return

        provider = await self.context.get_using_provider_async(event.unified_msg_origin)
        if not provider:
            event.stop_event()
            yield event.plain_result("没有可用的聊天模型，无法分析食物图片。")
            return
        modalities = provider.provider_config.get("modalities")
        if isinstance(modalities, list) and modalities and "image" not in modalities:
            event.stop_event()
            yield event.plain_result(
                "当前聊天模型未声明图片输入能力。请切换到支持多模态图片的模型后再试。"
            )
            return

        try:
            image_paths = [await image.convert_to_file_path() for image in images]
            response = await self.context.llm_generate(
                chat_provider_id=provider.meta().id,
                image_urls=image_paths,
                prompt=(
                    "分析这些图片是否展示了发送者已经食用或准备食用的一餐。"
                    "菜单、包装营养表、食谱、别人的食物和无法判断的图片不要视为可记录饮食。"
                    "若图片包含食物，请综合可见分量、烹饪方式、用油和酱汁，估算整餐热量。"
                    "只输出一个 JSON 对象，不要 Markdown，不要解释。格式："
                    '{"is_food":true,"description":"菜品和分量简述",'
                    '"calories":650,"lower_bound":520,"upper_bound":800,'
                    '"confidence":"high|medium|low","notes":"主要不确定因素"}。'
                    "若不应记录，输出："
                    '{"is_food":false}。'
                    f"用户附带文字：{(event.message_str or '无')[:500]}"
                ),
            )
            analysis = parse_food_analysis(response.completion_text)
        except Exception as exc:
            logger.exception("Food image analysis failed: %s", exc)
            event.stop_event()
            yield event.plain_result(
                "图片分析失败。请确认当前模型支持图片输入，或稍后重试。"
            )
            return

        if not analysis["is_food"]:
            return

        now = datetime.now().astimezone()
        entry = {
            "id": uuid.uuid4().hex[:8],
            "date": now.date().isoformat(),
            "created_at": now.isoformat(),
            "description": analysis["description"],
            "calories": analysis["calories"],
            "lower_bound": analysis["lower_bound"],
            "upper_bound": analysis["upper_bound"],
            "confidence": analysis["confidence"],
            "notes": analysis["notes"],
            "source": "image",
            "source_message_id": source_message_id,
        }
        key = self._state_key(event)
        async with self._locks.setdefault(key, asyncio.Lock()):
            state = await self._load_state(event)
            if not state["recording_enabled"]:
                return
            state["entries"].append(entry)
            state["entries"] = state["entries"][-1000:]
            await self._save_state(event, state)

        event.stop_event()
        confidence_text = {"high": "较高", "medium": "中等", "low": "较低"}[
            analysis["confidence"]
        ]
        estimate = (
            f"识别为：{analysis['description']}\n"
            f"估算 {analysis['calories']} kcal，合理范围 "
            f"{analysis['lower_bound']}～{analysis['upper_bound']} kcal，"
            f"置信度{confidence_text}。"
        )
        if analysis["notes"]:
            estimate += f"\n主要不确定因素：{analysis['notes']}"
        _, total, remaining = self._today_summary(state)
        yield event.plain_result(
            estimate
            + f"\n已自动记录。今日累计 {total} kcal，"
            + (
                f"还可摄入约 {remaining} kcal。"
                if remaining >= 0
                else f"已超过目标约 {-remaining} kcal。"
            )
        )

    async def terminate(self) -> None:
        """Release in-memory synchronization primitives on plugin shutdown."""
        self._locks.clear()
