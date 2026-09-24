"""交互式会话流程：建档、配置、撤销、清空。

四个流程共用同一模式：发送引导消息 → session_waiter 接管用户后续消息 →
逐条处理 → 超时/取消安全退出。``session_waiter`` 装饰的内部回调通过
``await next_event.send(...)`` 直接发送消息，外层 handler 不再 yield。

这些函数只依赖显式注入的 ``store``（状态事务）与 ``now``（配置时区当前时间），
不依赖插件实例，便于独立测试。
"""

from __future__ import annotations

from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.core.utils.session_waiter import (
    SessionController,
    session_waiter,
)

from .constants import (
    ACTIVITY_LEVELS,
    AGE_MAX,
    AGE_MIN,
    CONFIRM_SESSION_TIMEOUT,
    GOALS,
    HEIGHT_MAX,
    HEIGHT_MIN,
    SESSION_TIMEOUT,
    WEIGHT_MAX,
    WEIGHT_MIN,
)
from .nutrition import calculate_targets
from .reply_ui import plain_result
from .session_filter import CalorieUserSessionFilter
from .state_store import UserStateStore, resolve_date


def _now_iso(now: Any) -> str:
    """取当前时间的 ISO 字符串（now 为可调用对象）。"""
    return now().isoformat()


async def run_profile_flow(
    event: AstrMessageEvent, *, store: UserStateStore, now: Any
) -> None:
    """多轮问答创建或重建个人热量档案（对应 /热量 开始）。"""
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
    await event.send(
        plain_result(
            event, "我们先建立个人档案。随时发送“退出”可以取消。\n\n" + prompts[0]
        )
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
                plain_result(
                    next_event, "输入格式不正确，请重新输入。\n" + prompts[step]
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
        answers["updated_at"] = _now_iso(now)

        def mutate(state: dict[str, Any]) -> None:
            state["profile"] = answers

        await store.update(next_event, mutate)
        sex_note = (
            "\n你选择了不提供生理性别，因此结果采用两种公式的中间估值。"
            if answers["sex"] == "不提供"
            else ""
        )
        # 16～17 岁为未成年人：代谢公式本面向成人，附加专业建议提示。
        minor_note = (
            "\n提示：你还未成年，个体差异较大，建议在家长或专业人士指导下管理饮食。"
            if answers["age"] < 18
            else ""
        )
        await next_event.send(
            plain_result(
                next_event,
                f"档案已保存。估算每日总消耗约 {tdee} kcal，"
                f"{answers['goal']}目标为 {target} kcal/天。{sex_note}{minor_note}\n"
                "热量记录已默认开启，发送食物图片或直接用文字描述饮食，"
                "都会自动分析并入账。\n"
                "这是日常管理估算，不替代医生或营养师建议。",
            )
        )
        controller.stop()

    try:
        await profile_waiter(event, session_filter=CalorieUserSessionFilter())
    except TimeoutError:
        await event.send(
            plain_result(event, "建立档案已超时，请发送 /热量 开始 重新填写。")
        )
    finally:
        # 阻止原始指令继续进入聊天管线，避免问答内容再触发一次普通回复。
        event.stop_event()


async def run_configure_flow(
    event: AstrMessageEvent, *, store: UserStateStore, now: Any
) -> None:
    """对话式查看和局部修改档案（对应 /热量 配置）。"""
    state = await store.load(event)
    profile = state["profile"]
    if not profile:
        await event.send(plain_result(event, "尚未建立档案，请先发送 /热量 开始。"))
        return
    await event.send(
        plain_result(
            event,
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
            "修改完成后回复“完成”。",
        )
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
                    plain_result(
                        next_event,
                        "我没识别出要修改的项目。请回复："
                        "年龄、身高、体重、性别、活动或目标。",
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
                plain_result(
                    next_event,
                    "输入值无效，请重新输入。\n" + value_prompts[selected_field],
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
        profile["updated_at"] = _now_iso(now)

        # 锁内事务保存：加载最新状态再写回，避免覆盖并发期间的其他改动。
        def mutate(fresh: dict[str, Any]) -> None:
            fresh["profile"] = profile

        await store.update(next_event, mutate)
        selected_field = None
        # 把年龄改成未成年人区间时，附加专业建议提示。
        minor_note = (
            "\n提示：你还未成年，个体差异较大，建议在家长或专业人士指导下管理饮食。"
            if changed_field == "年龄" and profile["age"] < 18
            else ""
        )
        await next_event.send(
            plain_result(
                next_event,
                f"已更新{changed_field}并自动重算：TDEE 约 {tdee} kcal，"
                f"每日{profile['goal']}目标 {target} kcal。{minor_note}\n\n"
                "还想修改哪一项？回复年龄、身高、体重、性别、活动或目标；"
                "回复“完成”结束。",
            )
        )
        controller.keep(timeout=SESSION_TIMEOUT, reset_timeout=True)

    try:
        await config_waiter(event, session_filter=CalorieUserSessionFilter())
    except TimeoutError:
        await event.send(plain_result(event, "配置对话已超时；已完成的修改均已保存。"))
    finally:
        event.stop_event()


async def run_undo_flow(
    event: AstrMessageEvent, *, store: UserStateStore, now: Any, date: str
) -> None:
    """列出指定日期（缺省今天）的记录并撤销其中一笔（对应 /热量 撤销）。"""
    state = await store.load(event)
    try:
        target_date = resolve_date(date, now())
    except ValueError:
        await event.send(
            plain_result(
                event,
                "日期格式无法识别。支持：今天（缺省）、昨天、前天或 YYYY-MM-DD，"
                "例如 /热量 撤销 昨天。",
            )
        )
        return
    entries = [entry for entry in state["entries"] if entry.get("date") == target_date]
    if not entries:
        await event.send(plain_result(event, f"{target_date} 没有可撤销的热量记录。"))
        return
    await event.send(
        plain_result(
            event,
            f"请选择 {target_date} 要撤销的记录：\n"
            + "\n".join(
                f"{number}. #{entry['id']} "
                f"{entry.get('description', '饮食记录')} "
                f"{entry.get('calories', 0)} kcal"
                for number, entry in enumerate(entries, start=1)
            )
            + "\n\n请回复编号、记录 ID 或明确描述；回复“取消”退出。",
        )
    )

    @session_waiter(timeout=CONFIRM_SESSION_TIMEOUT, record_history_chains=False)
    async def undo_waiter(
        controller: SessionController, next_event: AstrMessageEvent
    ) -> None:
        selector = next_event.message_str.strip()
        if selector in {"取消", "退出"}:
            await next_event.send(plain_result(next_event, "已取消撤销。"))
            controller.stop()
            return

        # 锁内事务：重新加载状态再做删除，保证撤销的是最新数据。
        removed_holder: dict[str, Any] = {}

        def mutate(current: dict[str, Any]) -> None:
            normalized = selector.lstrip("#")
            # 非编号、非 ID 的纯描述可能命中多条记录；此时不能盲删，
            # 先检查唯一性，不唯一就要求用户改用编号或记录 ID。
            is_id = any(
                entry.get("id") == normalized and entry.get("date") == target_date
                for entry in current["entries"]
            )
            if not normalized.isdigit() and not is_id:
                matches = [
                    entry
                    for entry in current["entries"]
                    if entry.get("date") == target_date
                    and normalized in str(entry.get("description", ""))
                ]
                if len(matches) > 1:
                    removed_holder["error"] = (
                        "这个描述匹配到多条记录，请回复对应编号或记录 ID。"
                    )
                    return
            index = store.find_entry_index(current, selector, target_date)
            if index is None:
                removed_holder["error"] = (
                    "没有找到对应记录，请回复列表中的编号、记录 ID 或描述。"
                )
                return
            removed_holder["removed"] = current["entries"].pop(index)

        _, result = await store.update(next_event, mutate)
        if "error" in removed_holder:
            await next_event.send(plain_result(next_event, removed_holder["error"]))
            controller.keep(timeout=CONFIRM_SESSION_TIMEOUT, reset_timeout=True)
            return
        removed = removed_holder["removed"]

        await next_event.send(
            plain_result(
                next_event,
                f"已撤销：{removed.get('description', '饮食记录')}，"
                f"{removed.get('calories', 0)} kcal。",
            )
        )
        controller.stop()

    try:
        await undo_waiter(event, session_filter=CalorieUserSessionFilter())
    except TimeoutError:
        await event.send(plain_result(event, "撤销选择已超时，没有删除任何记录。"))
    finally:
        event.stop_event()


async def run_clear_flow(event: AstrMessageEvent, *, store: UserStateStore) -> None:
    """二次确认后清空个人档案与全部记录（对应 /热量 清空）。"""
    state = await store.load(event)
    entry_count = len(state["entries"])
    if not state["profile"] and entry_count == 0:
        await event.send(plain_result(event, "当前没有档案和饮食记录，无需清空。"))
        return

    # 列出将被删除的内容，让用户明确知道失去什么。
    summary_lines = []
    if state["profile"]:
        summary_lines.append("个人档案（年龄、身高等，含 TDEE 与每日目标）")
    if entry_count:
        summary_lines.append(f"历史饮食记录 {entry_count} 条")
    await event.send(
        plain_result(
            event,
            "即将清空以下数据，删除后无法恢复：\n"
            + "\n".join(f"- {line}" for line in summary_lines)
            + "\n\n清空后如需继续使用，要重新发送 /热量 开始 建档。\n"
            "确认请回复：确认清空；回复“取消”放弃。",
        )
    )

    @session_waiter(timeout=CONFIRM_SESSION_TIMEOUT, record_history_chains=False)
    async def clear_waiter(
        controller: SessionController, next_event: AstrMessageEvent
    ) -> None:
        value = next_event.message_str.strip()
        if value in {"取消", "退出"}:
            await next_event.send(plain_result(next_event, "已取消清空，数据未变动。"))
            controller.stop()
            return
        # 防误触：只接受完整的“确认清空”，其他输入一律重新提示。
        if value != "确认清空":
            await next_event.send(
                plain_result(next_event, "请回复“确认清空”或“取消”。")
            )
            controller.keep(timeout=CONFIRM_SESSION_TIMEOUT, reset_timeout=True)
            return

        # 锁内执行删除，避免与并发的记录写入交错。
        async with store.locked(next_event):
            await store.clear(next_event)

        await next_event.send(
            plain_result(
                next_event,
                "已清空全部档案与饮食记录。如需继续使用，请重新发送 /热量 开始 建档。",
            )
        )
        controller.stop()

    try:
        await clear_waiter(event, session_filter=CalorieUserSessionFilter())
    except TimeoutError:
        await event.send(plain_result(event, "清空确认已超时，没有删除任何数据。"))
    finally:
        event.stop_event()
