"""插件回复消息的渲染工具。"""

from __future__ import annotations

from typing import Any

from astrbot.api.event import AstrMessageEvent


def plain_result(event: AstrMessageEvent, text: str):
    """构造禁用 Markdown 的纯文本回复。

    插件的报表使用普通换行和 ``#id`` 标记；Markdown 渲染器（如 QQ 官方平台）
    会把 ``#id`` 渲染成标题、把单个换行合并成一行。强制纯文本可以保留
    换行与记录 ID 的原样显示。
    """
    return event.make_result().message(text).use_markdown(False)


def table_cell(value: Any) -> str:
    """清洗用于 Markdown 表格单元格的文本，避免竖线或换行破坏表格结构。"""
    return (
        str(value)
        .replace("|", "｜")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )
