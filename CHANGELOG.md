# 更新日志

## 未发布

### 修复
- 插件所有纯文本回复改为强制纯文本发送（`.use_markdown(False)`），修复 QQ 官方把 `#id` 渲染成标题、单个换行被合并成一行的问题
- 统一使用 UTC+8（Asia/Shanghai）作为“今天”的日期边界，修复服务器时区导致记录被划到错误日期的问题
- `/热量 今天` 明细改为 Markdown 表格输出，且不再显示记录 ID

## 1.2.1

### 修复
- 给 `list_daily_calorie_records` / `undo_daily_calorie_record` 两个 LLM 工具补默认参数，避免模型省略 `date`/`selector` 时抛出 `TypeError`
- 强化 `parse_food_analysis`：容忍模型返回的布尔/数值变体（如 `"true"`、`650.5`、`"650"`），避免误判或崩溃
- `_load_state` 对损坏的 KV 数据做归一化，避免类型异常导致崩溃
- 修复 `configure` 并发丢更新：保存前在锁内重载状态
- `record` / `auto_record` 的读-改-写补锁；`record` 不再跨 `yield` 持锁

## 1.2.0

- 使用当前会话的多模态模型分析食物图片并自动入账
- 入账后的回复由 AI 结合当前会话上下文、人设、用户档案与当日记录生成
- 新增 `/热量 配置` 局部修改档案，修改后自动重算 TDEE 与目标
- 新增 `/自动记录 开启|关闭|状态` 控制记录开关
- 向 AI 注册 `list_daily_calorie_records` 与 `undo_daily_calorie_record` 工具
