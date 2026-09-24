"""交互式会话（session_waiter）的用户隔离过滤器。"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent
from astrbot.core.utils.session_waiter import SessionFilter


class CalorieUserSessionFilter(SessionFilter):
    """让交互式对话（session_waiter）按平台用户隔离，不同用户互不串话。"""

    def filter(self, event: AstrMessageEvent) -> str:
        """返回该用户稳定的交互会话 ID。

        Args:
            event: 当前消息事件。

        Returns:
            由平台实例 ID 和发送者 ID 组成的会话 ID。
        """
        sender = event.get_sender_id() or event.unified_msg_origin
        return f"daily-calorie:{event.get_platform_id()}:{sender}"
