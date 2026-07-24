# 需求：dataclass 工厂方法下沉

## 背景

config.py 集中定义了 5 个 dataclass（AppType/MirrorConfig/ProjectInfo/DependencyReport/BuildConfig），
但创建这些 dataclass 的工厂函数散落在 project.py（parse_project）、analyzer.py（analyze_dependencies）、
wheel_cache.py（parse_wheel_filename）。违反"功能下沉到类"原则——调用方需知道工厂函数的模块位置，
而非直接通过类方法构造。

## 需求

- [x] `WheelInfo.from_filename()` 类方法：迁移 `parse_wheel_filename()` 逻辑，返回 `WheelInfo | None`。
- [x] `ProjectInfo.from_dir()` 类方法：迁移 `parse_project()` 逻辑，解析 pyproject.toml 并构造实例。
- [x] `DependencyReport.from_src()` 类方法：迁移 `analyze_dependencies()` 逻辑，扫描源码并构造实例。
- [x] 保留原模块级函数作为兼容入口（内部委托类方法），避免破坏外部调用。
- [x] 更新所有内部调用点优先使用类方法。
- [x] 测试通过，覆盖率 ≥ 95%。
- [x] 不改动运行时管理（embed/standalone）——留到后续迭代。

## 验收标准

- 三个 dataclass 各有 `from_*` 类方法作为工厂构造入口。
- 原模块级函数保留但内部委托类方法（向后兼容）。
- 全套门禁通过，覆盖率不低于 98.42%（iter-16 基线）。
