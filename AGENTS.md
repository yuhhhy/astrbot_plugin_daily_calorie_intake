# AGENTS.md

开发前请先阅读 AstrBot 插件开发指南：https://docs.astrbot.app/dev/star/plugin-new.html

## 分层约定

**职责边界**：`main.py` 为插件入口，仅承担插件类声明、事件与指令 handler 注册插件配置读取三类职责。handler 方法体一律为单行委托，业务逻辑统一下沉至 `core/` 下的对应模块实现。

**约束依据**：AstrBot 于类定义阶段完成 handler 注册，handler 与其声明所在模块相绑定；故插件类及其装饰器方法不可迁出 `main.py`，可拆分者仅为方法体实现。

## 提交规范

提交前须使用 ruff 格式化代码：先运行 `ruff format`，再运行 `ruff check`；
两者均无告警（必要时以 `--fix` 修复）后方可提交。
