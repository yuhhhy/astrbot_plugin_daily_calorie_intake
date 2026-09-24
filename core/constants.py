"""插件全局常量与默认值。

所有可调的阈值、范围和文案参数集中在这里，
避免魔法数字散落在业务代码中。
"""

from __future__ import annotations

# 活动水平：选项编号 → (展示名称, 基础代谢放大系数)
ACTIVITY_LEVELS = {
    "1": ("久坐（很少运动）", 1.2),
    "2": ("轻量活动（每周运动 1～3 天）", 1.375),
    "3": ("中等活动（每周运动 3～5 天）", 1.55),
    "4": ("高强度活动（每周运动 6～7 天）", 1.725),
    "5": ("非常高强度活动或体力工作", 1.9),
}

# 目标：选项编号 → 目标名称
GOALS = {"1": "减重", "2": "维持", "3": "增重"}

# 单笔热量允许范围（kcal）
MIN_CALORIES = 1
MAX_CALORIES = 10000

# 每用户保留的饮食记录条数默认值（可在 WebUI 配置中调整，键名 max_entries）
DEFAULT_MAX_ENTRIES = 1000
# 上面条数配置的允许范围，防止误填导致记录被清空或无限膨胀
MIN_ENTRIES_LIMIT = 1
MAX_ENTRIES_LIMIT = 100000

# 交互式会话超时（秒）：建档/配置对话用同一超时，撤销选择单独一个
SESSION_TIMEOUT = 180
UNDO_SESSION_TIMEOUT = 120

# 文本长度限制（字符）
MAX_DESCRIPTION_LENGTH = 200
MAX_NOTES_LENGTH = 300
MAX_USER_MESSAGE_LENGTH = 500

# 档案字段取值范围
# 年龄放宽到 16 岁：16～17 岁为未成年人，建档/修改时会附加专业建议提示
AGE_MIN, AGE_MAX = 16, 100
HEIGHT_MIN, HEIGHT_MAX = 120, 230
WEIGHT_MIN, WEIGHT_MAX = 30, 300

# 模型估算可信度枚举
CONFIDENCE_LEVELS = {"high", "medium", "low"}
