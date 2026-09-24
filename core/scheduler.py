"""订阅提醒的调度与投递服务。

封装三件事，使 ``main.py`` 的 handler 只做一行委托：

1. cron 任务的注册/删除，以及 **AstrBot 重启后的任务恢复**
   （basic 任务的 handler 只存在于内存，重启后 ``sync_from_db`` 会跳过它们）；
2. 投递：定时走 ``context.send_message`` 主动推送，补发走 ``event.send``；
3. ``/热量 订阅`` 指令的处理与文案。

纯逻辑（时间解析、到期判断、总结/体重提示文案）在 ``reminders.py``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import MessageChain

from . import reminders
from .constants import (
    DEFAULT_SUMMARY_TIME,
    DEFAULT_WEIGHT_CHECK_THRESHOLD,
    DEFAULT_WEIGHT_CHECK_WEEKDAY,
    PLUGIN_ID,
)
from .reply_ui import plain_result

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .state_store import UserStateStore

_WEEKDAY_NAMES = "周一 周二 周三 周四 周五 周六 周日".split()


class ReminderService:
    """按用户管理订阅提醒的定时任务与投递。

    Args:
        context: AstrBot Context（提供 cron_manager 与 send_message）。
        store: 用户状态存取（含按 key 的事务与加锁）。
        config_value: 配置实时读取函数，签名 ``(key, default) -> value``。
        now: 返回配置时区当前时间的可调用对象。
    """

    def __init__(
        self,
        *,
        context: Any,
        store: UserStateStore,
        config_value: Any,
        now: Any,
    ) -> None:
        self._context = context
        self._store = store
        self._config_value = config_value
        self._now = now

    # ---------- 配置 ----------

    def config(self) -> tuple[str, int, int]:
        """读取提醒配置：默认时间、每周检查日（1=周一…7=周日）、累计阈值。"""
        default_time = str(
            self._config_value("daily_summary_time", DEFAULT_SUMMARY_TIME)
        )
        try:
            weekday = int(
                self._config_value("weight_check_weekday", DEFAULT_WEIGHT_CHECK_WEEKDAY)
            )
        except (TypeError, ValueError):
            weekday = DEFAULT_WEIGHT_CHECK_WEEKDAY
        weekday = max(1, min(7, weekday))
        try:
            threshold = int(
                self._config_value(
                    "weight_check_threshold", DEFAULT_WEIGHT_CHECK_THRESHOLD
                )
            )
        except (TypeError, ValueError):
            threshold = DEFAULT_WEIGHT_CHECK_THRESHOLD
        return default_time, weekday, max(0, threshold)

    def _cron_timezone(self) -> str | None:
        """把配置的时区偏移换算成 cron 可用的时区名。"""
        try:
            offset = int(self._config_value("timezone_offset", 8))
        except (TypeError, ValueError):
            offset = 8
        return reminders.cron_timezone_name(offset)

    # ---------- 任务生命周期 ----------

    async def register_job(self, state_key: str, umo: str, time_str: str) -> str | None:
        """为该用户注册每日总结 cron 任务，返回 job_id（失败返回 None）。"""
        payload = {"plugin": PLUGIN_ID, "state_key": state_key, "umo": umo}
        kwargs = {
            "name": f"{PLUGIN_ID}:summary:{state_key[-8:]}",
            "cron_expression": reminders.cron_expression(time_str),
            "handler": self.push_scheduled,
            "description": f"每日 {time_str} 总结与每周体重提示",
            "payload": payload,
            "persistent": True,
        }
        try:
            # 先带配置时区创建；个别环境缺少 Etc 时区数据时回退到调度器默认时区。
            try:
                job = await self._context.cron_manager.add_basic_job(
                    timezone=self._cron_timezone(), **kwargs
                )
            except Exception:
                job = await self._context.cron_manager.add_basic_job(
                    timezone=None, **kwargs
                )
            return job.job_id
        except Exception as exc:
            logger.exception("注册订阅任务失败：%s", exc)
            return None

    async def remove_job(self, job_id: str | None) -> None:
        """删除订阅任务（不存在时静默忽略）。"""
        if not job_id:
            return
        try:
            await self._context.cron_manager.delete_job(job_id)
        except Exception as exc:
            logger.warning("删除订阅任务 %s 失败：%s", job_id, exc)

    async def restore_jobs(self) -> int:
        """插件加载后重建本插件的持久化任务，返回恢复数量。

        AstrBot 重启后 basic 任务的 handler 会丢失（``sync_from_db`` 会跳过
        无 handler 的任务），因此需要按 payload 中的插件标记找回并重建。
        """
        try:
            jobs = await self._context.cron_manager.list_jobs("basic")
        except Exception as exc:
            logger.warning("读取定时任务失败，订阅推送可能未恢复：%s", exc)
            return 0
        restored = 0
        for job in jobs:
            payload = getattr(job, "payload", None) or {}
            if payload.get("plugin") != PLUGIN_ID:
                continue
            state_key = payload.get("state_key")
            umo = payload.get("umo")
            if not state_key or not umo:
                continue
            try:
                await self._context.cron_manager.delete_job(job.job_id)
                new_job = await self._context.cron_manager.add_basic_job(
                    name=job.name,
                    cron_expression=job.cron_expression,
                    handler=self.push_scheduled,
                    description=job.description,
                    timezone=job.timezone,
                    payload=payload,
                    persistent=True,
                )
                async with self._store.locked_by_key(state_key):
                    state = await self._store.load_by_key(state_key)
                    state["subscription"]["job_id"] = new_job.job_id
                    await self._store.save_by_key(state_key, state)
                restored += 1
            except Exception as exc:
                logger.exception("恢复订阅任务 %s 失败：%s", job.job_id, exc)
        if restored:
            logger.info("每日热量助手：已恢复 %d 个订阅推送任务", restored)
        return restored

    async def push_scheduled(self, state_key: str, umo: str) -> None:
        """cron 定时回调：主动推送到期内容。"""
        await self.deliver(state_key, umo)

    # ---------- 投递 ----------

    async def _send(self, umo: str, text: str, event: AstrMessageEvent | None) -> bool:
        """投递提醒文本：有 event 时直接回复（补发），否则走主动推送。"""
        if event is not None:
            try:
                await event.send(plain_result(event, text))
                return True
            except Exception as exc:
                logger.warning("补发订阅提醒失败：%s", exc)
                return False
        try:
            await self._context.send_message(umo, MessageChain().message(text))
            return True
        except Exception as exc:
            # QQ 官方等平台可能因主动消息配额/无 msg_id 而失败，留给下次发言补发。
            logger.warning("定时推送失败（%s），将在用户下次发言时补发：%s", umo, exc)
            return False

    async def deliver(
        self,
        state_key: str,
        umo: str,
        event: AstrMessageEvent | None = None,
    ) -> bool:
        """投递到期的总结/体重提示；成功投递才记账，失败留待下次补发。"""
        default_time, weekday, threshold = self.config()
        async with self._store.locked_by_key(state_key):
            state = await self._store.load_by_key(state_key)
            current = self._now()
            text, summary_due, weight_due = reminders.compose_due_message(
                state,
                current,
                default_time=default_time,
                weekday=weekday,
                threshold=threshold,
            )
            if text is None and not summary_due and not weight_due:
                return False
            if text and not await self._send(umo, text, event):
                return False
            # 记账：只有真正发出（或无需发送）才推进日期，避免重复推送。
            today = current.date().isoformat()
            subscription = state["subscription"]
            if summary_due:
                subscription["last_daily_date"] = today
            if weight_due:
                subscription["last_weight_check"] = today
            state["subscription"] = subscription
            await self._store.save_by_key(state_key, state)
            return bool(text)

    # ---------- 指令 ----------

    async def handle_subscribe(
        self, event: AstrMessageEvent, action: str, time_spec: str
    ) -> str:
        """处理 ``/热量 订阅`` 指令，返回要回复的文本。"""
        state = await self._store.load(event)
        subscription = state["subscription"]
        default_time, weekday, _ = self.config()

        if action in {"开启", "开"}:
            if not state["profile"]:
                return "请先发送 /热量 开始 建立个人档案。"
            try:
                time_str = reminders.parse_summary_time(time_spec, default_time)
            except ValueError as exc:
                return f"时间格式不正确：{exc}"
            # 重建前先删掉旧任务，避免同一用户出现重复推送。
            await self.remove_job(subscription.get("job_id"))
            job_id = await self.register_job(
                self._store.state_key(event), event.unified_msg_origin, time_str
            )
            if not job_id:
                return "订阅任务创建失败，请查看 AstrBot 日志后重试。"

            def mutate(fresh: dict[str, Any]) -> None:
                sub = fresh["subscription"]
                sub.update(enabled=True, time=time_str, job_id=job_id)
                fresh["subscription"] = sub

            await self._store.update(event, mutate)
            return (
                f"已订阅：每天 {time_str} 推送当日饮食总结（{_WEEKDAY_NAMES[weekday - 1]}"
                "还会检查近 7 天累计缺口/超标，必要时提醒更新体重）。\n"
                "发送 /热量 订阅 关闭 可随时退订。\n"
                "注：部分平台受限时推送可能延迟到您下次发言时送达。"
            )

        if action in {"关闭", "关"}:
            await self.remove_job(subscription.get("job_id"))

            def mutate(fresh: dict[str, Any]) -> None:
                sub = fresh["subscription"]
                sub.update(enabled=False, job_id=None)
                fresh["subscription"] = sub

            await self._store.update(event, mutate)
            return "已关闭订阅，不会再推送总结与体重提醒。"

        if action == "状态":
            if not subscription.get("enabled"):
                return (
                    "当前未订阅定时推送。发送 /热量 订阅 开启 [HH:MM] 即可订阅，"
                    f"默认时间 {default_time}。"
                )
            last = subscription.get("last_daily_date") or "暂无"
            return (
                f"已订阅：每天 {subscription.get('time') or default_time} 推送当日总结；"
                f"每周{_WEEKDAY_NAMES[weekday - 1]}检查累计缺口/超标。\n"
                f"最近一次投递日期：{last}。"
            )

        return "用法：/热量 订阅 开启 [HH:MM]、/热量 订阅 关闭、/热量 订阅 状态"
