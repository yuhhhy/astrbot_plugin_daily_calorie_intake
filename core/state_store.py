"""每用户状态的读取、归一化、保存与汇总。

``UserStateStore`` 封装原来散落在插件类中的状态方法。
通过构造时注入的函数访问 AstrBot 插件 KV 与 WebUI 配置，
本模块不直接依赖插件实例，便于独立测试。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .constants import (
    DEFAULT_MAX_ENTRIES,
    MAX_ENTRIES_LIMIT,
    MIN_ENTRIES_LIMIT,
)

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


def resolve_date(spec: str, today: datetime) -> str:
    """把各种日期说法解析为 ISO 日期字符串（YYYY-MM-DD）。

    支持 today/今天、yesterday/昨天、前天 与 YYYY-MM-DD；
    无法识别或日期不存在时抛出 ``ValueError``。

    Args:
        spec: 用户或模型给出的日期说法。
        today: 配置时区下的当前时间，用于解析相对日期。

    Returns:
        ISO 日期字符串。
    """
    spec = (spec or "").strip().lower()
    if spec in {"", "today", "今天"}:
        return today.date().isoformat()
    if spec in {"yesterday", "昨天"}:
        return (today - timedelta(days=1)).date().isoformat()
    if spec == "前天":
        return (today - timedelta(days=2)).date().isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", spec):
        # 校验日期真实存在（如拒绝 2025-02-30）。
        try:
            datetime.strptime(spec, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"日期不存在：{spec}") from None
        return spec
    raise ValueError("无法识别的日期")


class UserStateStore:
    """封装每用户状态的存取与查询。

    Args:
        get_kv_data: 插件 KV 异步读取函数（如 ``Star.get_kv_data``）。
        put_kv_data: 插件 KV 异步写入函数（如 ``Star.put_kv_data``）。
        delete_kv_data: 插件 KV 异步删除函数（如 ``Star.delete_kv_data``）。
        config_value: 配置实时读取函数，签名 ``(key, default) -> value``。
        now: 返回配置时区当前时间的可调用对象。
    """

    def __init__(
        self,
        get_kv_data: Callable[..., Awaitable[Any]],
        put_kv_data: Callable[..., Awaitable[None]],
        config_value: Callable[[str, Any], Any],
        now: Callable[[], datetime],
        delete_kv_data: Callable[[str], Awaitable[None]],
    ) -> None:
        self._get_kv_data = get_kv_data
        self._put_kv_data = put_kv_data
        self._delete_kv_data = delete_kv_data
        self._config_value = config_value
        self._now = now
        # 每用户一把读写锁：同一用户的 KV 读-改-写必须串行，避免并发丢更新。
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def locked(self, event: AstrMessageEvent):
        """按用户加锁的异步上下文，串行化该用户的读-改-写序列。"""
        async with self._locks.setdefault(self.state_key(event), asyncio.Lock()):
            yield

    async def update(
        self,
        event: AstrMessageEvent,
        mutator: Callable[[dict[str, Any]], Any],
    ) -> tuple[dict[str, Any], Any]:
        """事务式修改状态：锁内“加载 → mutator 修改 → 保存”。

        Args:
            event: 当前消息事件。
            mutator: 接收状态字典并原地修改的回调；可返回任意标记值
                （例如用于向调用方传递“本次是否跳过写入”等信息）。

        Returns:
            （修改后的状态, mutator 的返回值）。
        """
        async with self.locked(event):
            state = await self.load(event)
            result = mutator(state)
            if asyncio.iscoroutine(result):
                result = await result
            await self.save(event, state)
            return state, result

    def state_key(self, event: AstrMessageEvent) -> str:
        """生成稳定且保护隐私的每用户存储键。

        对“平台 ID + 发送者 ID”做哈希后作为 KV 键，
        避免在存储中留下原始用户 ID。
        """
        sender = event.get_sender_id() or event.unified_msg_origin
        identity = f"{event.get_platform_id()}:{sender}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
        return f"user:{digest}"

    async def load(self, event: AstrMessageEvent) -> dict[str, Any]:
        """加载并归一化当前用户的状态数据。

        KV 中可能存在旧版本或损坏的数据，这里统一校正类型，
        保证后续读写不会因结构异常而崩溃。
        """
        state = await self._get_kv_data(self.state_key(event), {})
        if not isinstance(state, dict):
            state = {}
        if not isinstance(state.get("profile"), dict):
            state["profile"] = None
        # 新用户（还没有记录开关）使用 WebUI 配置的默认值。
        if "recording_enabled" not in state:
            state["recording_enabled"] = bool(
                self._config_value("default_recording_enabled", True)
            )
        # 清理旧版本遗留的键，避免继续占用存储。
        state.pop("auto_record", None)
        if not isinstance(state.get("entries"), list):
            state["entries"] = []
        state.pop("auto_mode", None)
        state.pop("pending", None)
        # 为缺少 ID 的历史记录补一个由内容派生的稳定 ID。
        for entry in state["entries"]:
            if not isinstance(entry, dict):
                continue
            if not entry.get("id"):
                seed = (
                    f"{entry.get('created_at', '')}:{entry.get('description', '')}:"
                    f"{entry.get('calories', 0)}"
                )
                entry["id"] = hashlib.sha256(seed.encode()).hexdigest()[:8]
        # 只保留字典类型的记录，过滤损坏条目。
        state["entries"] = [
            entry for entry in state["entries"] if isinstance(entry, dict)
        ]
        return state

    def _max_entries(self) -> int:
        """读取“每用户最多保留的记录条数”配置，钳制到允许范围。"""
        try:
            limit = int(self._config_value("max_entries", DEFAULT_MAX_ENTRIES))
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_ENTRIES
        return max(MIN_ENTRIES_LIMIT, min(MAX_ENTRIES_LIMIT, limit))

    async def save(self, event: AstrMessageEvent, state: dict[str, Any]) -> None:
        """把当前用户的状态写回插件 KV。

        写入前按配置裁剪记录条数（超出后丢弃最旧的），
        保证任何写路径都不会让 KV 无限膨胀。
        """
        entries = state.get("entries")
        if isinstance(entries, list):
            limit = self._max_entries()
            if len(entries) > limit:
                state["entries"] = entries[-limit:]
        await self._put_kv_data(self.state_key(event), state)

    async def clear(self, event: AstrMessageEvent) -> None:
        """删除该用户的全部状态数据（档案与记录），用于“重新开始”。"""
        await self._delete_kv_data(self.state_key(event))

    def close(self) -> None:
        """释放内存中的锁对象（插件卸载时调用）。"""
        self._locks.clear()

    def date_summary(self, state: dict[str, Any], date: str) -> tuple[str, int, int]:
        """汇总指定日期的摄入情况。

        Args:
            state: 完整用户状态。
            date: ISO 日期字符串（YYYY-MM-DD）。

        Returns:
            （日期, 当日总摄入, 剩余额度 = 目标 − 摄入）。
        """
        total = sum(
            int(entry.get("calories", 0))
            for entry in state["entries"]
            if entry.get("date") == date
        )
        profile = state.get("profile")
        try:
            target = int(profile.get("target", 0)) if isinstance(profile, dict) else 0
        except (TypeError, ValueError):
            target = 0
        return date, total, target - total

    def today_summary(
        self, state: dict[str, Any], now: datetime | None = None
    ) -> tuple[str, int, int]:
        """汇总今日摄入情况（``date_summary`` 的“今天”快捷方式）。

        Args:
            state: 完整用户状态。
            now: 可选的“当前时间”；缺省使用注入的 ``now``。

        Returns:
            （本地日期, 今日总摄入, 剩余额度 = 目标 − 摄入）。
        """
        if now is None:
            now = self._now()
        return self.date_summary(state, now.date().isoformat())

    def find_entry_index(
        self, state: dict[str, Any], selector: str, date: str | None = None
    ) -> int | None:
        """把记录选择器解析为完整记录列表中的下标。

        匹配优先级：别名（“最近”等）→ 记录 ID → 当天显示编号 → 描述子串。

        Args:
            state: 完整用户状态。
            selector: 记录 ID、显示编号、描述文本或 ``最近`` 等别名。
            date: 可选的 ISO 日期，仅匹配该日期的编号与描述。

        Returns:
            ``state['entries']`` 中的下标；没有匹配时返回 ``None``。
        """
        # 先按日期缩小候选范围（编号和描述的匹配只在这些候选中进行）。
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
