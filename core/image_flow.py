"""食物图片的分析与入账流程。

从 ``main.py`` 的 ``listen_for_food_images`` 中拆出：过滤、去重、
多模态识别、锁内入账、纯文本摘要降级都在这里完成；
生成 AI 个性化回复（需要 ``yield event.request_llm``）的部分
仍留在入口 handler 中。

所有依赖通过参数显式注入，不依赖插件实例。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .llm_parsing import parse_food_analysis
from .reply_ui import plain_result

if TYPE_CHECKING:
    from .state_store import UserStateStore


@dataclass
class ImageRecordResult:
    """图片成功识别并入账后的上下文，供入口 handler 生成回复。"""

    analysis: dict[str, Any]
    state: dict[str, Any]
    entry: dict[str, Any]
    now: datetime
    provider_id: str
    date: str
    total: int
    remaining: int


async def analyze_and_record(
    event: AstrMessageEvent,
    *,
    context: Any,
    store: UserStateStore,
    config_value: Any,
    now: Any,
) -> ImageRecordResult | None:
    """分析食物图片并入账。

    完整流程：过滤 → 去重 → 多模态识别 → 锁内入账 →（可选）纯文本摘要。

    Returns:
        成功入账且需要 AI 生成个性化回复时返回 :class:`ImageRecordResult`；
        其余情况（未开启记录、无图片、非食物、失败、已降级）返回 ``None``，
        调用方直接结束即可。需要拦截事件（去重/失败/降级）时，
        本函数内部已调用 ``event.stop_event()``。
    """
    # 未开启记录或未建档的用户直接跳过，不干扰正常聊天。
    state = await store.load(event)
    if not state["recording_enabled"] or not state["profile"]:
        return None

    # 只关心带图片的消息；纯文字记录由 record_daily_calorie_intake 工具链路处理。
    images = [
        component
        for component in event.get_messages()
        if isinstance(component, Comp.Image)
    ]
    if not images:
        return None

    # 以消息 ID 做去重，防止平台重推同一张图片导致重复入账。
    source_message_id = str(getattr(event.message_obj, "message_id", ""))
    if source_message_id and (
        any(
            entry.get("source_message_id") == source_message_id
            for entry in state["entries"]
        )
    ):
        event.stop_event()
        return None

    # 解析当前会话可用的聊天模型，用于图片分析和回复生成。
    try:
        provider_id = await context.get_current_chat_provider_id(
            event.unified_msg_origin
        )
    except Exception as exc:
        logger.exception("Failed to resolve chat provider: %s", exc)
        event.stop_event()
        await event.send(plain_result(event, "没有可用的聊天模型，无法分析食物图片。"))
        return None

    try:
        # 调用多模态模型分析图片：判定是否食物、估算热量与营养素，要求只输出 JSON。
        image_paths = [await image.convert_to_file_path() for image in images]
        response = await context.llm_generate(
            chat_provider_id=provider_id,
            image_urls=image_paths,
            prompt=(
                "分析这些图片是否展示了发送者已经食用或准备食用的一餐。"
                "菜单、包装营养表、食谱、别人的食物和无法判断的图片不要视为可记录饮食。"
                "若图片包含食物，请综合可见分量、烹饪方式、用油和酱汁，估算整餐热量，"
                "并估算三大宏量营养素（蛋白质/碳水/脂肪，单位克）。"
                "只输出一个 JSON 对象，不要 Markdown，不要解释。格式："
                '{"is_food":true,"description":"菜品和分量简述",'
                '"calories":650,"lower_bound":520,"upper_bound":800,'
                '"protein":25,"carbs":80,"fat":20,'
                '"confidence":"high|medium|low","notes":"主要不确定因素"}。'
                "营养素无法可靠估算时对应字段填 0。"
                "若不应记录，输出："
                '{"is_food":false}。'
                f"用户附带文字：{(event.message_str or '无')[:500]}"
            ),
        )
        analysis = parse_food_analysis(response.completion_text)
    except Exception as exc:
        logger.exception("Food image analysis failed: %s", exc)
        event.stop_event()
        await event.send(
            plain_result(
                event, "图片分析失败。请确认当前模型支持图片输入，或稍后重试。"
            )
        )
        return None

    # 非食物图片不记录、不打扰，事件继续由正常聊天管线处理。
    if not analysis["is_food"]:
        return None

    current = now()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "date": current.date().isoformat(),
        "created_at": current.isoformat(),
        "description": analysis["description"],
        "calories": analysis["calories"],
        "protein": analysis.get("protein", 0),
        "carbs": analysis.get("carbs", 0),
        "fat": analysis.get("fat", 0),
        "lower_bound": analysis["lower_bound"],
        "upper_bound": analysis["upper_bound"],
        "confidence": analysis["confidence"],
        "notes": analysis["notes"],
        "source": "image",
        "source_message_id": source_message_id,
    }

    # 锁内事务：复查开关与消息去重（并发场景下可能已有其他协程先入账）后追加。
    duplicate = False

    def mutate(fresh: dict[str, Any]) -> None:
        nonlocal duplicate
        if not fresh["recording_enabled"]:
            duplicate = True
            return
        if source_message_id and any(
            saved.get("source_message_id") == source_message_id
            for saved in fresh["entries"]
        ):
            duplicate = True
            event.stop_event()
            return
        fresh["entries"].append(entry)

    state, _ = await store.update(event, mutate)
    if duplicate:
        return None

    date, total, remaining = store.today_summary(state, current)
    remaining_text = (
        f"还可摄入约 {remaining} kcal"
        if remaining >= 0
        else f"已超过目标约 {-remaining} kcal"
    )

    if not config_value("image_ai_reply", True):
        # 关闭 AI 回复：只回纯文本摘要，并终止事件避免聊天管线再次响应图片。
        event.stop_event()
        await event.send(
            plain_result(
                event,
                f"已记录：{analysis['description']}，约 {analysis['calories']} kcal"
                f"（区间 {analysis['lower_bound']}～{analysis['upper_bound']}）。"
                f"今日累计 {total} kcal，目标 {state['profile']['target']} kcal，"
                f"{remaining_text}。",
            )
        )
        return None

    return ImageRecordResult(
        analysis=analysis,
        state=state,
        entry=entry,
        now=current,
        provider_id=provider_id,
        date=date,
        total=total,
        remaining=remaining,
    )


async def resolve_conversation(
    context: Any, event: AstrMessageEvent
) -> tuple[str | None, Any | None]:
    """复用当前会话的对话上下文；没有会话时返回 (None, None)。"""
    conversation_id = None
    conversation = None
    try:
        conversation_id = await context.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        if not conversation_id:
            conversation_id = await context.conversation_manager.new_conversation(
                event.unified_msg_origin,
                platform_id=event.get_platform_id(),
            )
        conversation = await context.conversation_manager.get_conversation(
            event.unified_msg_origin,
            conversation_id,
        )
    except Exception as exc:
        logger.exception("Failed to load calorie reply context: %s", exc)
    return conversation_id, conversation
