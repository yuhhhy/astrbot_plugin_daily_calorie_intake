"""每日热量助手插件的业务逻辑子包。

按职责划分的内部模块，只被 ``main.py``（插件入口）调用：
- constants.py：全局常量与默认值
- nutrition.py：TDEE 与每日目标计算
- llm_parsing.py：模型输出的容错解析
- state_store.py：每用户状态的存取与汇总（UserStateStore）
- reply_ui.py：回复消息渲染
- session_filter.py：交互式会话隔离
"""
