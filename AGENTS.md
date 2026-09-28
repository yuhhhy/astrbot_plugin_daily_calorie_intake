# AGENTS.md

开发前请先阅读 AstrBot 插件开发指南：https://docs.astrbot.app/dev/star/plugin-new.html

## 分层约定

**职责边界**：`main.py` 为插件入口，仅承担插件类声明、事件与指令 handler 注册插件配置读取三类职责。handler 方法体一律为单行委托，业务逻辑统一下沉至 `core/` 下的对应模块实现。

**约束依据**：AstrBot 于类定义阶段完成 handler 注册，handler 与其声明所在模块相绑定；故插件类及其装饰器方法不可迁出 `main.py`，可拆分者仅为方法体实现。

## 提交规范

代码注释与 docstring 统一使用中文，重点说明复杂逻辑的设计意图、边界条件和异常处理原因，避免为显而易见的代码添加逐行复述式注释。函数与方法的 docstring 仍须遵循 Google 风格，其中 `Args:`、`Returns:`、`Raises:` 等结构化标题保留英文。

提交前须使用 ruff 格式化代码：先运行 `ruff format`，再运行 `ruff check`；
两者均无告警（必要时以 `--fix` 修复）后方可提交。

测试文件仅用于本地验证，不得提交或上传到远程仓库，也不得包含在插件发布包中。
