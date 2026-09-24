"""每用户状态的读取、归一化、保存与汇总。

``UserStateStore`` 封装原来散落在插件类中的状态方法。
通过构造时注入的函数访问 AstrBot 插件 KV 与 WebUI 配置，
本模块不直接依赖插件实例，便于独立测试。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .constants import (
    DEFAULT_MAX_ENTRIES,
    MAX_ENTRIES_LIMIT,
    MIN_ENTRIES_LIMIT,
)

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class UserStateStore:
    """封装每用户状态的存取与查询。

    Args:
        get_kv_data: 插件 KV 异步读取函数（如 ``Star.get_kv_data``）。
        put_kv_data: 插件 KV 异步写入函数（如 ``Star.put_kv_data``）。
        config_value: 配置实时读取函数，签名 ``(key, default) -> value``。
        now: 返回配置时区当前时间的可调用对象。
    """

    def __init__(
        self,
        get_kv_data: Callable[..., Awaitable[Any]],
        put_kv_data: Callable[..., Awaitable[None]],
        config_value: Callable[[str, Any], Any],
        now: Callable[[], datetime],
    ) -> None:
        self._get_kv_data = get_kv_data
        self._put_kv_data = put_kv_data
        self._config_value = config_value
        self._now = now

    def state_key(self, event: "AstrMessageEvent") -> str:
        """生成稳定且保护隐私的每用户存储键。

        对“平台 ID + 发送者 ID”做哈希后作为 KV 键，
        避免在存储中留下原始用户 ID。
        """
        sender = event.get_sender_id() or event.unified_msg_origin
        identity = f"{event.get_platform_id()}:{sender}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
        return f"user:{digest}"

    async def load(self, event: "AstrMessageEvent") -> dict[str, Any]:
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

    async def save(
        self, event: "AstrMessageEvent", state: dict[str, Any]
    ) -> None:
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

    def today_summary(
        self, state: dict[str, Any], now: datetime | None = None
    ) -> tuple[str, int, int]:
        """汇总今日摄入情况。

        Args:
            state: 完整用户状态。
            now: 可选的“当前时间”；缺省使用注入的 ``now``。

        Returns:
            （本地日期, 今日总摄入, 剩余额度 = 目标 − 摄入）。
        """
        if now is None:
            now = self._now()
        date = now.date().isoformat()
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
