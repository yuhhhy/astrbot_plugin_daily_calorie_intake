"""插件全局常量与默认值。

所有可调的阈值、范围和文案参数集中在这里，
避免魔法数字散落在业务代码中。
"""

from __future__ import annotations

# 插件标识（与 @register 的插件名一致），也用于 cron 任务的归属标记
PLUGIN_ID = "astrbot_plugin_daily_calorie_intake"

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

# 交互式会话超时（秒）：建档/配置对话用同一超时；撤销选择、清空确认等
# 破坏性/短交互共用更短的超时
SESSION_TIMEOUT = 180
CONFIRM_SESSION_TIMEOUT = 120

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

# 文字补记允许回溯的最大天数（如“昨天晚饭忘了记”）
BACKFILL_MAX_DAYS = 7

# 记录来源的中文标签（用于 CSV 导出等展示场景）
SOURCE_LABELS = {"image": "图片识别", "text": "文字记录", "manual": "手动记录"}

# 单项营养素（蛋白质/碳水/脂肪）的合理上限（克），防止模型输出异常值
MAX_MACRO_GRAMS = 1000

# 文本版账单（文件发送失败降级）最多展示的记录条数，避免刷屏
TEXT_BILL_MAX_ENTRIES = 20

# 订阅提醒默认值（可在 WebUI 插件配置中覆盖）
DEFAULT_SUMMARY_TIME = "21:00"  # 每日总结的默认时间
DEFAULT_WEIGHT_CHECK_WEEKDAY = 7  # 每周体重检查日：1=周一 … 7=周日
DEFAULT_WEIGHT_CHECK_THRESHOLD = 3500  # 累计缺口/超标达到该值才提示更新体重（kcal）
