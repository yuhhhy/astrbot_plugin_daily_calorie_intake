"""热量目标计算。

基于 Mifflin-St Jeor 公式估算基础代谢（BMR），
结合活动系数得到每日总消耗（TDEE）与按目标调整后的每日热量目标。
本模块为纯计算，不依赖 AstrBot，可独立单元测试。
"""

from __future__ import annotations


def calculate_targets(
    age: int,
    height_cm: float,
    weight_kg: float,
    sex: str,
    activity_factor: float,
    goal: str,
) -> tuple[int, int]:
    """根据个人信息计算每日总消耗（TDEE）与热量目标。

    Args:
        age: 周岁年龄。
        height_cm: 身高（厘米）。
        weight_kg: 体重（公斤）。
        sex: 用于代谢公式的生理性别。
        activity_factor: 活动水平对基础代谢的放大系数。
        goal: ``减重``、``维持`` 或 ``增重`` 之一。

    Returns:
        四舍五入后的 TDEE 和每日热量目标。

    Raises:
        ValueError: 性别或目标不受支持时抛出。
    """
    # Mifflin-St Jeor 公式的性别修正项；“不提供”取男女常数的中间值。
    if sex == "男":
        sex_constant = 5
    elif sex == "女":
        sex_constant = -161
    elif sex == "不提供":
        sex_constant = -78
    else:
        raise ValueError("Unsupported biological sex")

    # 基础代谢：BMR = 10×体重 + 6.25×身高 − 5×年龄 + 性别修正。
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + sex_constant
    tdee = round(bmr * activity_factor)
    # 减重目标以基础代谢为下限，避免给出极端节食的数字。
    if goal == "减重":
        target = max(round(bmr), tdee - 400)
    elif goal == "增重":
        target = tdee + 250
    elif goal == "维持":
        target = tdee
    else:
        raise ValueError("Unsupported goal")
    return tdee, target
