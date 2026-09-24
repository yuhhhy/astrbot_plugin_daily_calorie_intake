"""每日热量助手插件（入口）。

通过多模态模型识别食物图片、由对话模型调用 LLM 工具记录文字描述的饮食，
为每个用户维护热量档案（基于 Mifflin-St Jeor 公式估算 TDEE 与每日目标），
自动累计当日摄入并计算剩余额度。

数据结构：每个用户在插件 KV 中保存一份状态（user:<哈希>），包含：
- profile：个人档案（年龄、身高、体重、性别、活动水平、目标、TDEE、每日目标）
- recording_enabled：该用户是否开启热量记录
- entries：饮食记录列表（每条含 id、日期、描述、热量、营养素、来源等）

模块划分（业务逻辑在 core/ 子包内，main.py 只保留 AstrBot 要求的
插件类与全部 @装饰器 handler，方法体一行委托给 core/ 内的模块）：
- core/constants.py：全局常量与默认值
- core/nutrition.py：TDEE 与每日目标计算
- core/llm_parsing.py：模型输出的容错解析
- core/state_store.py：每用户状态的存取/事务/汇总（UserStateStore）
- core/interactive.py：交互式会话流程（建档/配置/撤销/清空）
- core/tools.py：LLM 工具的业务逻辑
- core/image_flow.py：食物图片分析与入账流程
- core/reply_ui.py：回复消息/报表/账单/CSV 渲染
- core/session_filter.py：交互式会话隔离
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import gettempdir
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core import image_flow, interactive, tools
from .core.reply_ui import (
    build_text_bill,
    day_report,
    plain_result,
    write_export_csv,
)
from .core.state_store import UserStateStore, resolve_date

# “今天”的日期边界默认使用 UTC+8（Asia/Shanghai，中国无夏令时），避免服务器时区
# （常见为 UTC）导致记录被划到错误的日期；可在 WebUI 插件配置中调整时区偏移。


@register(
    "astrbot_plugin_daily_calorie_intake",
    "yuhhhy",
    "通过多模态模型与对话描述估算并记录每日热量摄入",
    "1.4.0",
)
class DailyCalorieIntakePlugin(Star):
    """按用户维护热量档案，自动识别食物图片与文字描述并记录每日摄入。

    注意：AstrBot 在类定义时注册所有 @filter 装饰的 handler，
    因此本类不可拆分；方法体全部委托给 core/ 内的模块。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # 插件目录缺少 _conf_schema.json 时 AstrBot 不会注入 config，回退为空字典。
        self.config = config if isinstance(config, dict) else {}
        # 状态存取委托给 UserStateStore（内置每用户事务锁），
        # 注入 KV 读写删除、配置读取与当前时间。
        self.store = UserStateStore(
            get_kv_data=self.get_kv_data,
            put_kv_data=self.put_kv_data,
            delete_kv_data=self.delete_kv_data,
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
        await interactive.run_profile_flow(event, store=self.store, now=self._now)

    @calorie.command("配置")
    async def configure(self, event: AstrMessageEvent):
        """通过对话查看和局部修改档案，修改后自动重算目标。"""
        await interactive.run_configure_flow(event, store=self.store, now=self._now)

    @calorie.command("今日")
    async def today(self, event: AstrMessageEvent):
        """查看今日摄入和剩余热量。"""
        state = await self.store.load(event)
        if not state["profile"]:
            yield plain_result(event, "请先发送 /热量 开始 建立个人档案。")
            return
        today = self._now().date().isoformat()
        yield event.make_result().message(day_report(state, today)).use_markdown(True)

    @calorie.command("查询")
    async def query_date(self, event: AstrMessageEvent, date: str = ""):
        """查询指定日期的摄入明细，支持 昨天、前天 或 YYYY-MM-DD。"""
        state = await self.store.load(event)
        if not state["profile"]:
            yield plain_result(event, "请先发送 /热量 开始 建立个人档案。")
            return
        try:
            target_date = resolve_date(date, self._now())
        except ValueError:
            yield plain_result(
                event,
                "日期格式无法识别。支持：今天（缺省）、昨天、前天或 YYYY-MM-DD，"
                "例如 /热量 查询 2025-09-20。",
            )
            return
        yield (
            event.make_result()
            .message(day_report(state, target_date))
            .use_markdown(True)
        )

    @calorie.command("撤销")
    async def undo(self, event: AstrMessageEvent, date: str = ""):
        """列出指定日期（缺省今天）的记录，并通过后续对话选择要撤销的一笔。"""
        await interactive.run_undo_flow(
            event, store=self.store, now=self._now, date=date
        )

    @calorie.command("清空")
    async def clear_data(self, event: AstrMessageEvent):
        """清空个人档案与全部饮食记录（需二次确认，删除后无法恢复）。"""
        await interactive.run_clear_flow(event, store=self.store)

    @calorie.command("导出")
    async def export_data(self, event: AstrMessageEvent):
        """把全部饮食记录导出为 CSV 文件发送（需平台支持文件发送）。"""
        state = await self.store.load(event)
        entries = state["entries"]
        if not entries:
            yield plain_result(event, "当前没有可导出的饮食记录。")
            return

        # 写入系统临时目录；文件名按用户哈希区分，重复导出覆盖旧文件，
        # 因此临时目录不会无限堆积。
        export_dir = Path(gettempdir()) / "astrbot_plugin_daily_calorie_intake"
        export_dir.mkdir(parents=True, exist_ok=True)
        identity = self.store.state_key(event).removeprefix("user:")
        file_path = export_dir / f"calorie_records_{identity[:8]}.csv"
        write_export_csv(str(file_path), entries)

        total = sum(int(e.get("calories", 0)) for e in entries)
        # 主动发送文件（而不是 yield 交给管线），这样发送异常能在本地捕获。
        try:
            await event.send(
                event.make_result().message(
                    Comp.File(name=file_path.name, file=str(file_path))
                )
            )
        except Exception as exc:
            # 降级：平台不支持/上传失败时改发纯文本账单，数据不受影响。
            logger.warning("CSV 文件发送失败，降级为纯文本账单：%s", exc)
            yield plain_result(
                event,
                build_text_bill(entries) + "\n\n（文件发送失败，已自动改发文本版；"
                "完整 CSV 需平台支持文件发送，如 OneBot/NapCat。）",
            )
            return
        yield plain_result(
            event,
            f"已导出 {len(entries)} 条记录（累计 {total} kcal）。"
            "注：文件发送需要平台支持（OneBot/NapCat 等）；QQ 官方平台仅群聊可用。",
        )

    @filter.command("自动记录")
    async def auto_record(self, event: AstrMessageEvent, action: str = "状态"):
        """开启或关闭整个插件的新增热量记录功能。"""
        # 开关保存在用户状态里，只影响该用户自己的新增记录，不影响他人。
        state = await self.store.load(event)
        if action in {"开启", "开"}:
            if not state["profile"]:
                yield plain_result(event, "请先发送 /热量 开始 建立个人档案。")
                return
            await self.store.update(event, lambda s: s.update(recording_enabled=True))
            yield plain_result(
                event, "热量记录已开启。文字描述饮食和食物图片均可自动分析入账。"
            )
        elif action in {"关闭", "关"}:
            await self.store.update(event, lambda s: s.update(recording_enabled=False))
            yield plain_result(
                event,
                "热量记录已关闭。文字描述和食物图片都不会再分析或入账；"
                "已有记录仍可查询和撤销。",
            )
        elif action == "状态":
            enabled = "已开启" if state["recording_enabled"] else "已关闭"
            yield plain_result(
                event,
                f"热量记录{enabled}；开启时文字描述饮食和食物图片都会自动分析并直接入账。",
            )
        else:
            yield plain_result(
                event, "用法：/自动记录 开启、/自动记录 关闭、/自动记录 状态"
            )

    @filter.llm_tool(name="record_daily_calorie_intake")
    async def record_calorie_tool(
        self,
        event: AstrMessageEvent,
        description: str = "",
        calories: int = 0,
        protein: float = 0,
        carbs: float = 0,
        fat: float = 0,
        lower_bound: int = 0,
        upper_bound: int = 0,
        confidence: str = "",
        date: str = "today",
    ) -> str:
        """记录一笔饮食热量与宏量营养素，默认记在今天，也支持补记最近几天。

        当用户以文字描述自己已经吃下或正在吃的食物、并希望记入每日热量时调用，
        例如"我午饭吃了一碗牛肉面，帮我记一下"。调用前先根据描述的分量和烹饪
        方式估算整餐热量与三大宏量营养素（蛋白质/碳水/脂肪，单位克）。
        用户提到所吃的是之前某天（如"昨天晚饭忘了记"）时，
        把 date 参数设为对应日期，最多补记最近 7 天，不能记录未来日期。
        用户只是在询问热量而没有记录意愿，或描述的不是用户本人的饮食时，
        不要调用。是否入账以本工具的返回结果为准。

        Args:
            description(string): 食物与分量的简短描述，例如"一碗牛肉面加一个鸡蛋"。
            calories(number): 估算的整餐总热量，单位 kcal，取 1～10000 的整数。
            protein(number): 估算的蛋白质克数；无法可靠估算时填 0。
            carbs(number): 估算的碳水克数；无法可靠估算时填 0。
            fat(number): 估算的脂肪克数；无法可靠估算时填 0。
            lower_bound(number): 估算热量下限，单位 kcal；不确定时与 calories 相同。
            upper_bound(number): 估算热量上限，单位 kcal；不确定时与 calories 相同。
            confidence(string): 估算可信度：high、medium 或 low。
            date(string): 记录日期：today/今天（默认）、yesterday/昨天、前天，或 YYYY-MM-DD。
        """
        return await tools.record_calorie(
            event,
            store=self.store,
            now=self._now,
            description=description,
            calories=calories,
            protein=protein,
            carbs=carbs,
            fat=fat,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            confidence=confidence,
            date=date,
        )

    @filter.llm_tool(name="list_daily_calorie_records")
    async def list_records_tool(
        self, event: AstrMessageEvent, date: str = "today"
    ) -> str:
        """列出指定日期的热量记录，供查询或撤销前定位记录。

        Args:
            date(string): 日期：today/今天（默认）、yesterday/昨天、前天，或 YYYY-MM-DD。
        """
        return await tools.list_records(
            event, store=self.store, now=self._now, date=date
        )

    @filter.llm_tool(name="undo_daily_calorie_record")
    async def undo_record_tool(
        self, event: AstrMessageEvent, selector: str = "", date: str = "today"
    ) -> str:
        """撤销用户明确指定的一条热量记录，含糊时应先列出记录。

        Args:
            selector(string): 记录 ID、当天显示编号或足以唯一定位的描述。
            date(string): 记录日期：today/今天（默认）、yesterday/昨天、前天，或 YYYY-MM-DD。
        """
        return await tools.undo_record(
            event, store=self.store, now=self._now, selector=selector, date=date
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def listen_for_food_images(self, event: AstrMessageEvent):
        """监听所有消息，在记录开启时自动分析食物图片并入账。

        完整流程（过滤 → 去重 → 识别 → 入账）在 core/image_flow 中完成；
        这里只负责生成 AI 个性化回复。非食物图片不处理，
        事件继续走正常聊天管线。
        """
        recorded = await image_flow.analyze_and_record(
            event,
            context=self.context,
            store=self.store,
            config_value=self._config_value,
            now=self._now,
        )
        if recorded is None:
            return

        # 组装给回复模型的精确数据：模型只负责组织语言，不得修改数字。
        analysis = recorded.analysis
        today_entries = [
            {
                "description": saved.get("description", "饮食记录"),
                "calories": int(saved.get("calories", 0)),
                "protein": float(saved.get("protein") or 0),
                "carbs": float(saved.get("carbs") or 0),
                "fat": float(saved.get("fat") or 0),
            }
            for saved in recorded.state["entries"]
            if saved.get("date") == recorded.date
        ]
        reply_context = {
            "profile": recorded.state["profile"],
            "latest_food_analysis": analysis,
            "today_entries": today_entries,
            "today_total_calories": recorded.total,
            "daily_target_calories": int(recorded.state["profile"]["target"]),
            "remaining_calories": recorded.remaining,
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
        conversation_id, conversation = await image_flow.resolve_conversation(
            self.context, event
        )

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
                chat_provider_id=recorded.provider_id,
                prompt=reply_prompt,
            )
            await event.send(plain_result(event, reply.completion_text.strip()))
        except Exception as exc:
            logger.exception("Failed to generate calorie reply: %s", exc)
            await event.send(
                plain_result(event, "已记录这次饮食，但 AI 回复生成失败。")
            )

    async def terminate(self) -> None:
        """插件卸载时释放内存中的锁对象（锁已下沉到 store）。"""
        self.store.close()
