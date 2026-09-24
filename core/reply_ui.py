"""插件回复与导出内容的渲染工具。"""

from __future__ import annotations

import csv
from typing import Any

from astrbot.api.event import AstrMessageEvent

from .constants import SOURCE_LABELS, TEXT_BILL_MAX_ENTRIES


def plain_result(event: AstrMessageEvent, text: str):
    """构造禁用 Markdown 的纯文本回复。

    插件的报表使用普通换行和 ``#id`` 标记；Markdown 渲染器（如 QQ 官方平台）
    会把 ``#id`` 渲染成标题、把单个换行合并成一行。强制纯文本可以保留
    换行与记录 ID 的原样显示。
    """
    return event.make_result().message(text).use_markdown(False)


def table_cell(value: Any) -> str:
    """清洗用于 Markdown 表格单元格的文本，避免竖线或换行破坏表格结构。"""
    return str(value).replace("|", "｜").replace("\r", " ").replace("\n", " ").strip()


def format_macros_cell(entry: dict[str, Any]) -> str:
    """格式化单条记录的营养素表格单元格（蛋白/碳水/脂肪 克数）。

    三项都为 0 或缺失（旧记录、未估算）时显示 ``-``。
    """
    protein = float(entry.get("protein") or 0)
    carbs = float(entry.get("carbs") or 0)
    fat = float(entry.get("fat") or 0)
    if (protein, carbs, fat) == (0, 0, 0):
        return "-"
    return f"{protein:g}/{carbs:g}/{fat:g} g"


def day_report(state: dict[str, Any], date: str) -> str:
    """生成某一天的摄入报告（汇总 + 营养素小计 + Markdown 表格明细）。

    Args:
        state: 完整用户状态（调用方需已确认档案存在）。
        date: ISO 日期字符串（YYYY-MM-DD）。

    Returns:
        可直接发送的报告文本（配合 Markdown 渲染）。
    """
    entries = [entry for entry in state["entries"] if entry.get("date") == date]
    total = sum(int(entry.get("calories", 0)) for entry in entries)
    target = int(state["profile"]["target"])
    remaining = target - total
    status = (
        f"还可摄入约 {remaining} kcal"
        if remaining >= 0
        else f"已超过目标约 {-remaining} kcal"
    )
    if entries:
        # 有记录时用 Markdown 表格输出明细；编号仅用于本次展示，不对应记录 ID。
        # 营养素列显示 蛋白/碳水/脂肪 克数；旧记录没有该数据时显示 -。
        rows = [
            "| 编号 | 描述 | 热量 | 蛋白/碳水/脂肪 |",
            "| --- | --- | --- | --- |",
        ]
        for number, entry in enumerate(entries, start=1):
            description = table_cell(entry.get("description", "饮食记录"))
            macros = format_macros_cell(entry)
            rows.append(
                f"| {number} | {description} | "
                f"{entry.get('calories', 0)} kcal | {macros} |"
            )
        body = "\n\n" + "\n".join(rows)
    else:
        body = "\n这一天还没有饮食记录。"
    # 三大营养素当日合计；全部为 0（旧数据/未估算）时不显示该行。
    protein_total = sum(float(e.get("protein") or 0) for e in entries)
    carbs_total = sum(float(e.get("carbs") or 0) for e in entries)
    fat_total = sum(float(e.get("fat") or 0) for e in entries)
    macro_summary = (
        f"\n蛋白质合计 {protein_total:g}g，碳水合计 {carbs_total:g}g，"
        f"脂肪合计 {fat_total:g}g。"
        if (protein_total, carbs_total, fat_total) != (0, 0, 0)
        else ""
    )
    summary = (
        f"{date} 已记录 {total} kcal，目标 {target} kcal，{status}。{macro_summary}"
    )
    return summary + body


def build_text_bill(entries: list[dict[str, Any]]) -> str:
    """生成纯文本版账单（文件发送失败时的降级方案）。

    汇总在前，明细按从新到旧排列；记录很多时只展示最近
    ``TEXT_BILL_MAX_ENTRIES`` 条，避免刷屏。
    """
    total = sum(int(e.get("calories", 0)) for e in entries)
    protein_total = sum(float(e.get("protein") or 0) for e in entries)
    carbs_total = sum(float(e.get("carbs") or 0) for e in entries)
    fat_total = sum(float(e.get("fat") or 0) for e in entries)
    lines = [
        "📋 热量账单（文本版）",
        f"共 {len(entries)} 条记录，累计 {total} kcal，"
        f"蛋白质 {protein_total:g}g／碳水 {carbs_total:g}g／脂肪 {fat_total:g}g。",
        "",
    ]
    recent = entries[-TEXT_BILL_MAX_ENTRIES:]
    skipped = len(entries) - len(recent)
    lines.append(f"最近 {len(recent)} 条（从新到旧）：")
    for number, entry in enumerate(reversed(recent), start=1):
        description = table_cell(entry.get("description", "饮食记录"))
        macros = format_macros_cell(entry)
        macro_part = f"（{macros}）" if macros != "-" else ""
        lines.append(
            f"{number}. [{entry.get('date', '')}] {description} "
            f"{entry.get('calories', 0)} kcal{macro_part}"
        )
    if skipped > 0:
        lines.append(
            f"（其余 {skipped} 条未展示；完整数据请在支持文件发送的平台重新导出）"
        )
    return "\n".join(lines)


def write_export_csv(file_path: str, entries: list[dict[str, Any]]) -> None:
    """把饮食记录写成 CSV 文件（utf-8-sig 带 BOM，Excel 打开不乱码）。"""
    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "日期",
                "描述",
                "热量(kcal)",
                "蛋白质(g)",
                "碳水(g)",
                "脂肪(g)",
                "下限(kcal)",
                "上限(kcal)",
                "可信度",
                "来源",
                "记录时间",
            ]
        )
        for entry in entries:
            writer.writerow(
                [
                    entry.get("date", ""),
                    entry.get("description", ""),
                    entry.get("calories", 0),
                    entry.get("protein", ""),
                    entry.get("carbs", ""),
                    entry.get("fat", ""),
                    entry.get("lower_bound", ""),
                    entry.get("upper_bound", ""),
                    entry.get("confidence", ""),
                    SOURCE_LABELS.get(entry.get("source"), entry.get("source", "")),
                    entry.get("created_at", ""),
                ]
            )
