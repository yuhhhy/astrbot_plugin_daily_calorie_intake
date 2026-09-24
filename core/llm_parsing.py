"""模型输出的容错解析。

模型返回的数字/布尔可能是浮点、字符串等变体，这里统一做宽容转换；
食物分析 JSON 的结构、取值范围和区间一致性也在此校验和归一化。
本模块为纯函数，不依赖 AstrBot，可独立单元测试。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .constants import (
    CONFIDENCE_LEVELS,
    MAX_CALORIES,
    MAX_DESCRIPTION_LENGTH,
    MAX_MACRO_GRAMS,
    MAX_NOTES_LENGTH,
    MIN_CALORIES,
)


def coerce_float(value: Any) -> float:
    """把模型给出的数值宽容地转成 float（布尔同样拒绝）。

    其他类型抛出 ``ValueError``。
    """
    if isinstance(value, bool):
        raise ValueError("Expected a number, got a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("Expected a number, got an empty string")
        try:
            return float(stripped)
        except ValueError:
            raise ValueError(f"Expected a number, got {value!r}") from None
    raise ValueError(f"Expected a number, got {type(value).__name__}")


def normalize_macro_grams(value: Any) -> float:
    """把模型给出的营养素数值归一化为克。

    缺省或无法解析时按 0 处理（营养素是补充信息，不应让记录失败），
    负值钳为 0，超高钳到 ``MAX_MACRO_GRAMS``。
    """
    if value is None:
        return 0.0
    try:
        grams = coerce_float(value)
    except ValueError:
        return 0.0
    return round(min(max(0.0, grams), MAX_MACRO_GRAMS), 1)


def coerce_int(value: Any) -> int:
    """把模型给出的数值宽容地转成 int。

    接受 int、float 和数字字符串，浮点数四舍五入到整数卡路里；
    其他类型抛出 ``ValueError``。
    """
    # 布尔是 int 的子类，但把 true/false 当数字没有意义，显式拒绝。
    if isinstance(value, bool):
        raise ValueError("Expected a number, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("Expected a number, got an empty string")
        try:
            return round(float(stripped))
        except ValueError:
            raise ValueError(f"Expected a number, got {value!r}") from None
    raise ValueError(f"Expected a number, got {type(value).__name__}")


def coerce_bool(value: Any) -> bool:
    """把模型给出的布尔含义值（true/1/"yes" 等）宽容地转成 bool。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in {"true", "yes", "y", "1"}:
            return True
        if stripped in {"false", "no", "n", "0"}:
            return False
    raise ValueError("Missing is_food")


def parse_food_analysis(text: str) -> dict[str, Any]:
    """解析并校验模型返回的食物分析 JSON。

    Args:
        text: 模型原始回复文本。

    Returns:
        归一化后的食物分析结果；非食物时仅含 ``is_food: False``。

    Raises:
        ValueError: 回复不是合法的食物分析 JSON、热量越界或区间矛盾时抛出。
    """
    # 模型有时会在 JSON 前后附加说明文字，取第一个 {...} 块再解析。
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("The model did not return a JSON object")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("The model did not return a JSON object")
    is_food = coerce_bool(payload.get("is_food"))
    if not is_food:
        return {"is_food": False}

    calories = coerce_int(payload.get("calories"))
    lower_raw = payload.get("lower_bound")
    upper_raw = payload.get("upper_bound")
    # 区间缺省时收敛为点估计。
    lower = coerce_int(lower_raw) if lower_raw is not None else calories
    upper = coerce_int(upper_raw) if upper_raw is not None else calories
    if not MIN_CALORIES <= calories <= MAX_CALORIES:
        raise ValueError("Calories are outside the supported range")
    # 区间必须包住估计值本身，否则视为模型输出矛盾，按失败处理。
    if lower > calories or upper < calories:
        raise ValueError("Invalid calorie range")
    confidence = str(payload.get("confidence") or "low").lower()
    if confidence not in CONFIDENCE_LEVELS:
        confidence = "low"
    return {
        "is_food": True,
        "description": str(payload.get("description", "食物图片"))[
            :MAX_DESCRIPTION_LENGTH
        ],
        "calories": calories,
        # 三大宏量营养素（克）；旧模型输出可能缺失，缺省为 0。
        "protein": normalize_macro_grams(payload.get("protein")),
        "carbs": normalize_macro_grams(payload.get("carbs")),
        "fat": normalize_macro_grams(payload.get("fat")),
        # 区间钳制到全局支持的 [0, MAX_CALORIES] 范围。
        "lower_bound": max(0, lower),
        "upper_bound": min(MAX_CALORIES, upper),
        "confidence": confidence,
        "notes": str(payload.get("notes", ""))[:MAX_NOTES_LENGTH],
    }
