"""每日热量助手插件（入口）。

通过多模态模型识别食物图片、由对话模型调用 LLM 工具记录文字描述的饮食，
为每个用户维护热量档案（基于 Mifflin-St Jeor 公式估算 TDEE 与每日目标），
自动累计当日摄入并计算剩余额度。

数据结构：每个用户在插件 KV 中保存一份状态（user:<哈希>），包含：
- profile：个人档案（年龄、身高、体重、性别、活动水平、目标、TDEE、每日目标）
- recording_enabled：该用户是否开启热量记录
- entries：饮食记录列表（每条含 id、日期、描述、热量、区间、来源等）

模块划分（业务逻辑在 core/ 子包内）：
- core/constants.py：全局常量与默认值
- core/nutrition.py：TDEE 与每日目标计算
- core/llm_parsing.py：模型输出的容错解析
- core/state_store.py：每用户状态的存取与汇总（UserStateStore）
- core/reply_ui.py：回复消息渲染
- core/session_filter.py：交互式会话隔离
main.py 只保留 AstrBot 要求的插件类与全部 @装饰器 handler，
具体逻辑委托给 core/ 内的模块。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import (
    SessionController,
    session_waiter,
)

from .core.constants import (
    ACTIVITY_LEVELS,
    AGE_MAX,
    AGE_MIN,
    CONFIDENCE_LEVELS,
    GOALS,
    HEIGHT_MAX,
    HEIGHT_MIN,
    MAX_CALORIES,
    MAX_DESCRIPTION_LENGTH,
    MAX_USER_MESSAGE_LENGTH,
    MIN_CALORIES,
    SESSION_TIMEOUT,
    UNDO_SESSION_TIMEOUT,
    WEIGHT_MAX,
    WEIGHT_MIN,
)
from .core.llm_parsing import coerce_int, parse_food_analysis
from .core.nutrition import calculate_targets
from .core.reply_ui import plain_result, table_cell
from .core.session_filter import CalorieUserSessionFilter
from .core.state_store import UserStateStore

# “今天”的日期边界默认使用 UTC+8（Asia/Shanghai，中国无夏令时），避免服务器时区
# （常见为 UTC）导致记录被划到错误的日期；可在 WebUI 插件配置中调整时区偏移。


@register(
    "astrbot_plugin_daily_calorie_intake",
    "yuhhhy",
    "通过多模态模型与对话描述估算并记录每日热量摄入",
    "1.3.2",
)
class DailyCalorieIntakePlugin(Star):
    """按用户维护热量档案，自动识别食物图片与文字描述并记录每日摄入。

    注意：AstrBot 在类定义时注册所有 @filter 装饰的 handler，
    因此本类不可拆分；业务逻辑请委托给模块级辅助类/函数。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # 插件目录缺少 _conf_schema.json 时 AstrBot 不会注入 config，回退为空字典。
        self.config = config if isinstance(config, dict) else {}
        # 每个用户一把读写锁：同一用户的 KV 读-改-写必须串行，避免并发丢更新。
        self._locks: dict[str, asyncio.Lock] = {}
        # 状态存取委托给 UserStateStore，注入 KV 读写、配置读取与当前时间。
        self.store = UserStateStore(
            get_kv_data=self.get_kv_data,
            put_kv_data=self.put_kv_data,
            config_value=self._config_value,
            now=self._now,
        )

    def _config_value(self, key: str, default: Any) -> Any:
        """安全读取插件配置项，缺失或为 None 时回退到默认值。

        每次调用实时读取配置，WebUI 中修改并保存后无需重载插件即可生效。
        """
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _timezone(self) -> timezone:
        """根据配置构建“今天”的日期边界时区，偏移钳制在 -12～+14 小时。"""
        try:
            offset = int(self._config_value("timezone_offset", 8))
        except (TypeError, ValueError):
            offset = 8
        offset = max(-12, min(14, offset))
        return timezone(timedelta(hours=offset))

    def _now(self) -> datetime:
        """返回配置时区下的当前时间。"""
        return datetime.now(self._timezone())

    @filter.command_group("热量")
    def calorie():
        """管理每日热量目标和饮食记录。"""

    @calorie.command("开始")
    async def start_profile(self, event: AstrMessageEvent):
        """通过多轮问答创建或重建个人热量档案。"""
        # 问题列表，顺序与 profile_waiter 中 step 的处理分支一一对应。
        prompts = [
            "请输入年龄（支持 16～100 岁）：",
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
        yield plain_result(event,
            "我们先建立个人档案。随时发送“退出”可以取消。\n\n" + prompts[0]
        )

        # 用会话等待器接管该用户的后续消息：每收到一条回复就处理一步，
        # 答错不推进（重发当前问题并重置超时），答完六步即建档。
        @session_waiter(timeout=SESSION_TIMEOUT, record_history_chains=False)
        async def profile_waiter(
            controller: SessionController, next_event: AstrMessageEvent
        ) -> None:
            nonlocal step
            value = next_event.message_str.strip()
            if value == "退出":
                await next_event.send(plain_result(next_event, "已取消建立档案。"))
                controller.stop()
                return

            # 每一步单独校验取值范围，非法输入抛 ValueError 统一处理。
            try:
                if step == 0:
                    parsed = int(value)
                    if not AGE_MIN <= parsed <= AGE_MAX:
                        raise ValueError
                    answers["age"] = parsed
                elif step == 1:
                    parsed = float(value)
                    if not HEIGHT_MIN <= parsed <= HEIGHT_MAX:
                        raise ValueError
                    answers["height_cm"] = parsed
                elif step == 2:
                    parsed = float(value)
                    if not WEIGHT_MIN <= parsed <= WEIGHT_MAX:
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
                    plain_result(next_event,
                        "输入格式不正确，请重新输入。\n" + prompts[step]
                    )
                )
                controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                return

            step += 1
            if step < len(prompts):
                await next_event.send(plain_result(next_event, prompts[step]))
                controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                return

            # 六个问题全部回答完毕：计算 TDEE 与目标并写入档案。
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
            answers["updated_at"] = self._now().isoformat()
            state = await self.store.load(next_event)
            state["profile"] = answers
            await self.store.save(next_event, state)
            sex_note = (
                "\n你选择了不提供生理性别，因此结果采用两种公式的中间估值。"
                if answers["sex"] == "不提供"
                else ""
            )
            # 16～17 岁为未成年人：代谢公式本面向成人，附加专业建议提示。
            minor_note = (
                "\n提示：你还未成年，个体差异较大，"
                "建议在家长或专业人士指导下管理饮食。"
                if answers["age"] < 18
                else ""
            )
            await next_event.send(
                plain_result(next_event,
                    f"档案已保存。估算每日总消耗约 {tdee} kcal，"
                    f"{answers['goal']}目标为 {target} kcal/天。{sex_note}{minor_note}\n"
                    "热量记录已默认开启，发送食物图片或直接用文字描述饮食，"
                    "都会自动分析并入账。\n"
                    "这是日常管理估算，不替代医生或营养师建议。"
                )
            )
            controller.stop()

        try:
            await profile_waiter(event, session_filter=CalorieUserSessionFilter())
        except TimeoutError:
            yield plain_result(event, "建立档案已超时，请发送 /热量 开始 重新填写。")
        finally:
            # 阻止原始指令继续进入聊天管线，避免问答内容再触发一次普通回复。
            event.stop_event()

    @calorie.command("配置")
    async def configure(self, event: AstrMessageEvent):
        """通过对话查看和局部修改档案，修改后自动重算目标。"""
        state = await self.store.load(event)
        profile = state["profile"]
        if not profile:
            yield plain_result(event, "尚未建立档案，请先发送 /热量 开始。")
            return
        yield plain_result(event,
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
        # 交互状态：None 表示下一步先选字段，选定后进入取值步骤。
        selected_field: str | None = None
        # 字段名到追问文案的映射。
        value_prompts = {
            "年龄": "请输入新年龄（16～100）：",
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

        @session_waiter(timeout=SESSION_TIMEOUT, record_history_chains=False)
        async def config_waiter(
            controller: SessionController, next_event: AstrMessageEvent
        ) -> None:
            nonlocal selected_field
            value = next_event.message_str.strip()
            if value in {"完成", "退出", "取消"}:
                await next_event.send(plain_result(next_event, "配置已结束。"))
                controller.stop()
                return

            # 第一步：确认要修改的字段；兼容英文键名和“字段名包含在回复里”的模糊匹配。
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
                        plain_result(next_event,
                            "我没识别出要修改的项目。请回复："
                            "年龄、身高、体重、性别、活动或目标。"
                        )
                    )
                    controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                    return
                selected_field = normalized
                await next_event.send(
                    plain_result(next_event, value_prompts[selected_field])
                )
                controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                return

            # 第二步：按字段校验新值；非法输入抛 ValueError 统一重问。
            try:
                if selected_field == "年龄":
                    parsed = int(value)
                    if not AGE_MIN <= parsed <= AGE_MAX:
                        raise ValueError
                    profile["age"] = parsed
                elif selected_field == "身高":
                    parsed = float(value)
                    if not HEIGHT_MIN <= parsed <= HEIGHT_MAX:
                        raise ValueError
                    profile["height_cm"] = parsed
                elif selected_field == "体重":
                    parsed = float(value)
                    if not WEIGHT_MIN <= parsed <= WEIGHT_MAX:
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
                    plain_result(next_event,
                        "输入值无效，请重新输入。\n" + value_prompts[selected_field]
                    )
                )
                controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)
                return

            changed_field = selected_field
            # 任一字段变化都会重算 TDEE 与每日目标。
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
            profile["updated_at"] = self._now().isoformat()
            key = self.store.state_key(next_event)
            # 锁内重新加载最新状态再保存，避免覆盖并发期间的其他改动。
            async with self._locks.setdefault(key, asyncio.Lock()):
                fresh_state = await self.store.load(next_event)
                fresh_state["profile"] = profile
                await self.store.save(next_event, fresh_state)
            selected_field = None
            # 把年龄改成未成年人区间时，附加专业建议提示。
            minor_note = (
                "\n提示：你还未成年，个体差异较大，"
                "建议在家长或专业人士指导下管理饮食。"
                if changed_field == "年龄" and profile["age"] < 18
                else ""
            )
            await next_event.send(
                plain_result(next_event,
                    f"已更新{changed_field}并自动重算：TDEE 约 {tdee} kcal，"
                    f"每日{profile['goal']}目标 {target} kcal。{minor_note}\n\n"
                    "还想修改哪一项？回复年龄、身高、体重、性别、活动或目标；"
                    "回复“完成”结束。"
                )
            )
            controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)

        try:
            await config_waiter(event, session_filter=CalorieUserSessionFilter())
        except TimeoutError:
            yield plain_result(event, "配置对话已超时；已完成的修改均已保存。")
        finally:
            event.stop_event()

    @calorie.command("今日")
    async def today(self, event: AstrMessageEvent):
        """查看今日摄入和剩余热量。"""
        state = await self.store.load(event)
        if not state["profile"]:
            yield plain_result(event, "请先发送 /热量 开始 建立个人档案。")
            return
        date, total, remaining = self.store.today_summary(state)
        status = (
            f"还可摄入约 {remaining} kcal"
            if remaining >= 0
            else f"已超过目标约 {-remaining} kcal"
        )
        entries = [entry for entry in state["entries"] if entry.get("date") == date]
        if entries:
            # 有记录时用 Markdown 表格输出明细；编号仅用于本次展示，不对应记录 ID。
            rows = [
                "| 编号 | 描述 | 热量 |",
                "| --- | --- | --- |",
            ]
            for number, entry in enumerate(entries, start=1):
                description = table_cell(entry.get("description", "饮食记录"))
                rows.append(
                    f"| {number} | {description} | "
                    f"{entry.get('calories', 0)} kcal |"
                )
            details = "\n".join(rows)
        else:
            details = ""
        summary = (
            f"{date} 已记录 {total} kcal，目标 {state['profile']['target']} kcal，"
            f"{status}。"
        )
        body = f"\n\n{details}" if details else "\n今天还没有饮食记录。"
        yield event.make_result().message(summary + body).use_markdown(True)

    @calorie.command("撤销")
    async def undo(self, event: AstrMessageEvent):
        """列出今天的记录，并通过后续对话选择要撤销的一笔。"""
        state = await self.store.load(event)
        today = self._now().date().isoformat()
        entries = [entry for entry in state["entries"] if entry.get("date") == today]
        if not entries:
            yield plain_result(event, "今天还没有可撤销的热量记录。")
            return
        yield plain_result(event,
            "请选择要撤销的记录：\n"
            + "\n".join(
                f"{number}. #{entry['id']} "
                f"{entry.get('description', '饮食记录')} "
                f"{entry.get('calories', 0)} kcal"
                for number, entry in enumerate(entries, start=1)
            )
            + "\n\n请回复编号、记录 ID 或明确描述；回复“取消”退出。"
        )

        @session_waiter(timeout=UNDO_SESSION_TIMEOUT, record_history_chains=False)
        async def undo_waiter(
            controller: SessionController, next_event: AstrMessageEvent
        ) -> None:
            selector = next_event.message_str.strip()
            if selector in {"取消", "退出"}:
                await next_event.send(plain_result(next_event, "已取消撤销。"))
                controller.stop()
                return

            key = self.store.state_key(next_event)
            # 锁内重新加载状态再做删除，保证撤销的是最新数据。
            async with self._locks.setdefault(key, asyncio.Lock()):
                current_state = await self.store.load(next_event)
                normalized = selector.lstrip("#")
                # 非编号、非 ID 的纯描述可能命中多条记录；此时不能盲删，
                # 先检查唯一性，不唯一就要求用户改用编号或记录 ID。
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
                            plain_result(next_event,
                                "这个描述匹配到多条记录，请回复对应编号或记录 ID。"
                            )
                        )
                        controller.keep(timeout=UNDO_SESSION_TIMEOUT, reset_timeout=True)
                        return
                index = self.store.find_entry_index(current_state, selector, today)
                if index is None:
                    await next_event.send(
                        plain_result(next_event,
                            "没有找到对应记录，请回复列表中的编号、记录 ID 或描述。"
                        )
                    )
                    controller.keep(timeout=UNDO_SESSION_TIMEOUT, reset_timeout=True)
                    return
                removed = current_state["entries"].pop(index)
                await self.store.save(next_event, current_state)

            await next_event.send(
                plain_result(next_event,
                    f"已撤销：{removed.get('description', '饮食记录')}，"
                    f"{removed.get('calories', 0)} kcal。"
                )
            )
            controller.stop()

        try:
            await undo_waiter(event, session_filter=CalorieUserSessionFilter())
        except TimeoutError:
            yield plain_result(event, "撤销选择已超时，没有删除任何记录。")
        finally:
            event.stop_event()

    @filter.command("自动记录")
    async def auto_record(self, event: AstrMessageEvent, action: str = "状态"):
        """开启或关闭整个插件的新增热量记录功能。"""
        # 开关保存在用户状态里，只影响该用户自己的新增记录，不影响他人。
        state = await self.store.load(event)
        if action in {"开启", "开"}:
            if not state["profile"]:
                yield plain_result(event, "请先发送 /热量 开始 建立个人档案。")
                return
            key = self.store.state_key(event)
            async with self._locks.setdefault(key, asyncio.Lock()):
                state = await self.store.load(event)
                state["recording_enabled"] = True
                await self.store.save(event, state)
            yield plain_result(event,
                "热量记录已开启。文字描述饮食和食物图片均可自动分析入账。"
            )
        elif action in {"关闭", "关"}:
            key = self.store.state_key(event)
            async with self._locks.setdefault(key, asyncio.Lock()):
                state = await self.store.load(event)
                state["recording_enabled"] = False
                await self.store.save(event, state)
            yield plain_result(event,
                "热量记录已关闭。文字描述和食物图片都不会再分析或入账；"
                "已有记录仍可查询和撤销。"
            )
        elif action == "状态":
            enabled = "已开启" if state["recording_enabled"] else "已关闭"
            yield plain_result(event,
                f"热量记录{enabled}；开启时文字描述饮食和食物图片都会自动分析并直接入账。"
            )
        else:
            yield plain_result(event,
                "用法：/自动记录 开启、/自动记录 关闭、/自动记录 状态"
            )

    @filter.llm_tool(name="record_daily_calorie_intake")
    async def record_calorie_tool(
        self,
        event: AstrMessageEvent,
        description: str = "",
        calories: int = 0,
        lower_bound: int = 0,
        upper_bound: int = 0,
        confidence: str = "",
    ) -> str:
        """记录用户通过文字描述的一笔饮食热量。

        当用户以文字描述自己已经吃下或正在吃的食物、并希望记入每日热量时调用，
        例如"我午饭吃了一碗牛肉面，帮我记一下"。调用前先根据描述的分量和烹饪
        方式估算整餐热量。用户只是在询问热量而没有记录意愿，或描述的不是用户
        本人的饮食时，不要调用。是否入账以本工具的返回结果为准。

        Args:
            description(string): 食物与分量的简短描述，例如"一碗牛肉面加一个鸡蛋"。
            calories(number): 估算的整餐总热量，单位 kcal，取 1～10000 的整数。
            lower_bound(number): 估算热量下限，单位 kcal；不确定时与 calories 相同。
            upper_bound(number): 估算热量上限，单位 kcal；不确定时与 calories 相同。
            confidence(string): 估算可信度：high、medium 或 low。
        """
        clean_description = (
            (description or "").strip()[:MAX_DESCRIPTION_LENGTH] or "饮食记录"
        )
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

        key = self.store.state_key(event)
        async with self._locks.setdefault(key, asyncio.Lock()):
            state = await self.store.load(event)
            if not state["profile"]:
                return "用户尚未建立热量档案，未记录。请先让用户发送 /热量 开始。"
            if not state["recording_enabled"]:
                return "热量记录当前已关闭，未记录。请让用户发送 /自动记录 开启 后重试。"
            now = self._now()
            entry = {
                "id": uuid.uuid4().hex[:8],
                "date": now.date().isoformat(),
                "created_at": now.isoformat(),
                "description": clean_description,
                "calories": calories,
                "lower_bound": lower,
                "upper_bound": upper,
                "confidence": conf,
                "source": "text",
            }
            state["entries"].append(entry)
            await self.store.save(event, state)  # save 内按配置裁剪旧记录

        date, total, remaining = self.store.today_summary(state, now)
        remaining_text = (
            f"还可摄入约 {remaining} kcal"
            if remaining >= 0
            else f"已超过目标约 {-remaining} kcal"
        )
        # 返回给模型的文本会进入下一轮 prompt，由模型组织成对用户的回复。
        return (
            f"已记录：{clean_description}，约 {calories} kcal"
            f"（区间 {lower}～{upper}，可信度 {conf}），记录 ID {entry['id']}。"
            f"{date} 今日累计 {total} kcal，目标 {state['profile']['target']} kcal，"
            f"{remaining_text}。"
        )

    @filter.llm_tool(name="list_daily_calorie_records")
    async def list_records_tool(
        self, event: AstrMessageEvent, date: str = "today"
    ) -> str:
        """列出指定日期的热量记录，供查询或撤销前定位记录。

        Args:
            date(string): 日期，使用 today 表示今天，或使用 YYYY-MM-DD。
        """
        state = await self.store.load(event)
        if not state["profile"]:
            return "用户尚未建立热量档案。"
        # 日期参数支持 today 别名和 YYYY-MM-DD 两种写法。
        date = (date or "today").lower()
        if date in {"today", "今天"}:
            date = self._now().date().isoformat()
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
        self, event: AstrMessageEvent, selector: str = "", date: str = "today"
    ) -> str:
        """撤销用户明确指定的一条热量记录，含糊时应先列出记录。

        Args:
            selector(string): 记录 ID、当天显示编号或足以唯一定位的描述。
            date(string): 记录日期，使用 today 表示今天，或使用 YYYY-MM-DD。
        """
        selector = (selector or "").strip()
        if not selector:
            return "未指定要撤销的记录。请先调用 list_daily_calorie_records。"
        date = (date or "today").lower()
        if date in {"today", "今天"}:
            date = self._now().date().isoformat()
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return "日期格式无效，请使用 today 或 YYYY-MM-DD。"
        key = self.store.state_key(event)
        # 锁内重新加载状态再做删除，保证撤销的是最新数据。
        async with self._locks.setdefault(key, asyncio.Lock()):
            state = await self.store.load(event)
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
                    return (
                        "描述匹配到多条记录，不能确定要撤销哪一条。"
                        "请先调用 list_daily_calorie_records，再使用记录 ID。"
                    )
            index = self.store.find_entry_index(state, selector, date)
            if index is None:
                return "没有找到对应记录。请先调用 list_daily_calorie_records 定位。"
            removed = state["entries"].pop(index)
            await self.store.save(event, state)
        return (
            f"已撤销记录 {removed.get('id')}："
            f"{removed.get('description', '饮食记录')}，"
            f"{removed.get('calories', 0)} kcal。"
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def listen_for_food_images(self, event: AstrMessageEvent):
        """监听所有消息，在记录开启时自动分析食物图片并入账。

        完整流程：过滤 → 去重 → 多模态识别 → 锁内入账 → 生成回复。
        非食物图片不处理，事件继续走正常聊天管线。
        """
        # 未开启记录或未建档的用户直接跳过，不干扰正常聊天。
        state = await self.store.load(event)
        if not state["recording_enabled"] or not state["profile"]:
            return

        # 只关心带图片的消息；纯文字记录由 record_daily_calorie_intake 工具链路处理。
        images = [
            component
            for component in event.get_messages()
            if isinstance(component, Comp.Image)
        ]
        if not images:
            return

        # 以消息 ID 做去重，防止平台重推同一张图片导致重复入账。
        source_message_id = str(getattr(event.message_obj, "message_id", ""))
        if source_message_id and (
            any(
                entry.get("source_message_id") == source_message_id
                for entry in state["entries"]
            )
        ):
            event.stop_event()
            return

        # 解析当前会话可用的聊天模型，用于图片分析和回复生成。
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
        except Exception as exc:
            logger.exception("Failed to resolve chat provider: %s", exc)
            event.stop_event()
            yield plain_result(event, "没有可用的聊天模型，无法分析食物图片。")
            return

        try:
            # 调用多模态模型分析图片：判定是否食物、估算热量并要求只输出 JSON。
            image_paths = [await image.convert_to_file_path() for image in images]
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
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
                    f"用户附带文字：{(event.message_str or '无')[:MAX_USER_MESSAGE_LENGTH]}"
                ),
            )
            analysis = parse_food_analysis(response.completion_text)
        except Exception as exc:
            logger.exception("Food image analysis failed: %s", exc)
            event.stop_event()
            yield plain_result(event,
                "图片分析失败。请确认当前模型支持图片输入，或稍后重试。"
            )
            return

        # 非食物图片不记录、不打扰，事件继续由正常聊天管线处理。
        if not analysis["is_food"]:
            return

        now = self._now()
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
        key = self.store.state_key(event)
        # 锁内复查开关与消息去重：并发场景下可能已有其他协程先入账。
        async with self._locks.setdefault(key, asyncio.Lock()):
            state = await self.store.load(event)
            if not state["recording_enabled"]:
                return
            if source_message_id and any(
                saved.get("source_message_id") == source_message_id
                for saved in state["entries"]
            ):
                event.stop_event()
                return
            state["entries"].append(entry)
            await self.store.save(event, state)  # save 内按配置裁剪旧记录

        date, total, remaining = self.store.today_summary(state, now)
        remaining_text = (
            f"还可摄入约 {remaining} kcal"
            if remaining >= 0
            else f"已超过目标约 {-remaining} kcal"
        )

        if not self._config_value("image_ai_reply", True):
            # 关闭 AI 回复：只回纯文本摘要，并终止事件避免聊天管线再次响应图片。
            event.stop_event()
            yield plain_result(event,
                f"已记录：{analysis['description']}，约 {analysis['calories']} kcal"
                f"（区间 {analysis['lower_bound']}～{analysis['upper_bound']}）。"
                f"今日累计 {total} kcal，目标 {state['profile']['target']} kcal，"
                f"{remaining_text}。"
            )
            return

        # 组装给回复模型的精确数据：模型只负责组织语言，不得修改数字。
        today_entries = [
            {
                "description": saved.get("description", "饮食记录"),
                "calories": int(saved.get("calories", 0)),
            }
            for saved in state["entries"]
            if saved.get("date") == date
        ]
        reply_context = {
            "profile": state["profile"],
            "latest_food_analysis": analysis,
            "today_entries": today_entries,
            "today_total_calories": total,
            "daily_target_calories": int(state["profile"]["target"]),
            "remaining_calories": remaining,
            "user_message_with_image": (event.message_str or "").strip(),
        }
        reply_prompt = (
            "你正在回复用户刚刚发送的食物图片。插件已完成识别和入账，"
            "请结合当前对话上下文、你的人设以及下方精确数据，直接生成最终回复。"
            "回复必须自然、简洁并个性化，说明识别到的食物、估算热量与范围、"
            "主要不确定因素、已自动记录、今日累计和剩余或超出热量，"
            "再根据用户的目标和当日记录给出一条有用的建议或鼓励。"
            "不得修改、重算或编造下方数字，不要输出 JSON、标题或分析过程。"
            "热量仅为日常估算，不要做医疗诊断。\n\n"
            f"插件数据：{json.dumps(reply_context, ensure_ascii=False)}"
        )

        # 复用当前会话的对话上下文生成回复；没有会话时退化为一次性生成。
        conversation_id = None
        conversation = None
        try:
            conversation_id = (
                await self.context.conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin
                )
            )
            if not conversation_id:
                conversation_id = (
                    await self.context.conversation_manager.new_conversation(
                        event.unified_msg_origin,
                        platform_id=event.get_platform_id(),
                    )
                )
            conversation = await self.context.conversation_manager.get_conversation(
                event.unified_msg_origin,
                conversation_id,
            )
        except Exception as exc:
            logger.exception("Failed to load calorie reply context: %s", exc)

        # 这里必须让请求继续走 AstrBot 的结果管线；
        # 若 stop_event 会吞掉 request_llm 生成的回复。
        event.call_llm = True
        if conversation:
            yield event.request_llm(
                prompt=reply_prompt,
                session_id=conversation_id or "",
                conversation=conversation,
            )
            return

        try:
            reply = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=reply_prompt,
            )
            await event.send(plain_result(event, reply.completion_text.strip()))
        except Exception as exc:
            logger.exception("Failed to generate calorie reply: %s", exc)
            await event.send(plain_result(event, "已记录这次饮食，但 AI 回复生成失败。"))

    async def terminate(self) -> None:
        """插件卸载时释放内存中的锁对象。"""
        self._locks.clear()
