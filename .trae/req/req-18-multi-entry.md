# 需求：单项目多入口打包

## 背景

fspack 原仅支持单入口打包：`pyproject.toml` 的 `name` 字段对应生成的可执行文件
名，`detect_entry` 自动识别入口脚本。但实际项目常需要从同一份代码生成多个可执行
文件（如 cli 工具 + gui 配置面板 + web 监控页），共享同一套 Python 运行时与
第三方依赖。重复打包三次会浪费空间与构建时间。

## 需求

- [x] 单个项目支持在 `pyproject.toml` 声明多个入口，每个入口生成独立 exe
- [x] 多入口共享项目依赖（`[project.dependencies]`）与 Python 运行时（runtime/）
- [x] 多入口支持混合类型：cli/gui/web 各自按脚本 import 推断 app_type
- [x] `fsp r --entry <name>` 选择运行指定入口
- [x] 单入口项目完全向后兼容（无 `[tool.fspack.entries]` 走原路径）
- [x] C loader 缓存仍可跨项目复用（loader 源码不含入口路径硬编码）
- [x] 增加示例 `examples/multi_entry`，含 cli+gui+web 三入口
- [x] 测试通过，覆盖率 ≥ 95%

## 验收标准

- `examples/multi_entry` 声明三个入口，`fsp b` 生成 3 个 exe（cli.exe/gui.exe/web.exe）
- `fsp r --entry <name>` 运行指定入口，输出正确
- 单入口项目行为不变（无 `[tool.fspack.entries]` 走 `detect_entry` 路径）
- C loader 缓存键仍由 `(py_xy, app_type, platform)` 决定，跨项目复用
- 门禁通过，覆盖率不低于 iter-17 基线（98.43%）
