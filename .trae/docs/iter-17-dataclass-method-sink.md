# 迭代 17：dataclass 工厂方法下沉

## 迭代目标

用户更新了 rules 和 skills（新增 `python-class-design` SKILL 等），要求完善项目，
重点是"整合 dataclass 的内容，功能下沉到类"。将散落在 project.py、analyzer.py、
wheel_cache.py 的工厂函数下沉为对应 dataclass 的 `from_*` 类方法，使调用方通过类
方法构造实例，而非知道工厂函数的模块位置。

## 需求清单

- [x] `WheelInfo.from_filename()` 类方法
- [x] `ProjectInfo.from_dir()` 类方法
- [x] `DependencyReport.from_src()` 类方法
- [x] 保留原模块级函数作为兼容入口（内部委托类方法）
- [x] 更新所有内部调用点优先使用类方法
- [x] 测试通过，覆盖率 ≥ 95%
- [x] 不改动运行时管理（embed/standalone）——留到后续迭代

## 改动文件清单

### 类方法下沉

- `src/fspack/wheel_cache.py`：新增 `WheelInfo.from_filename()` 类方法；
  `parse_wheel_filename()` 改为兼容包装（委托类方法）
- `src/fspack/config.py`：
  - `ProjectInfo.from_dir()` 类方法——惰性导入 `parse_project` 打破 config↔project
    循环依赖
  - `DependencyReport.from_src()` 类方法——惰性导入 `analyze_dependencies` 打破
    config↔analyzer 循环依赖

### 调用点更新

- `src/fspack/builder.py`：`parse_project(...)` → `ProjectInfo.from_dir(...)`；
  `analyze_dependencies(...)` → `DependencyReport.from_src(...)`
- `src/fspack/installer.py`：`parse_project(...)` → `ProjectInfo.from_dir(...)`
- `src/fspack/linux_installer.py`：`parse_project(...)` → `ProjectInfo.from_dir(...)`
- `src/fspack/commands/run.py`：`parse_project(...)` → `ProjectInfo.from_dir(...)`
- `src/fspack/slim.py`：`parse_wheel_filename(...)` → `WheelInfo.from_filename(...)`

### 配置文件修复（用户更新 rules 后独立配置文件缺失项补全）

- `pyrefly.toml`：
  - `project-excludes` 补回 `examples/**`（示例代码不纳入类型检查）
  - `search-path` 从 `["."]` 改为 `["src", "."]`——修复 pyrefly 将本地文件解析为
    `src.fspack.config.ProjectInfo` 而惰性导入返回 `fspack.config.ProjectInfo` 致
    `bad-return` 错误的问题。`src/` 在前使 pyrefly 将 `src/fspack/config.py` 的模块
    路径解析为 `fspack.config`（与 import 解析一致）
- `ruff.toml`：`[lint.per-file-ignores]` 补回 `ARG005`（tests 中 lambda 未用参数）

### 代码风格修复

- `src/fspack/wheel_cache.py`：`from_filename` 返回类型从 `"WheelInfo | None"`
  （字符串前向引用）改为 `WheelInfo | None`——`from __future__ import annotations`
  已生效，无需字符串包装（UP037）
- `tests/test_wheel_cache.py`：移除冗余 `# noqa: ARG005`（ARG005 已在 per-file-ignores）

### 测试更新

- `tests/test_wheel_cache.py`：
  - 新增 `TestWheelInfoFromFilename`（4 个测试）——直接测试类方法
  - 原 `TestParseWheelFilename` 改为 `TestParseWheelFilenameCompat`（3 个测试）——
    验证兼容包装行为，含 `test_delegates_to_classmethod` 断言两者返回值一致
- `tests/test_config.py`：新增 5 个测试
  - `test_project_info_from_dir_helloworld`：解析 cli_helloworld 示例
  - `test_project_info_from_dir_with_explicit_py_version`：py_version 参数透传
  - `test_project_info_from_dir_pyside2_app`：GUI 示例与 requires-python
  - `test_dependency_report_from_src_classification`：依赖分类
  - `test_dependency_report_from_src_submodules`：子模块收集

## 关键决策与依据

### 1. 惰性导入打破循环依赖

`config.py` ← `project.py`/`analyzer.py` 存在循环依赖：
- `project.py` 顶部 `from fspack.config import ProjectInfo`
- `analyzer.py` 顶部 `from fspack.config import DependencyReport`

若 `config.py` 顶部也导入它们，则形成循环。`from_dir`/`from_src` 在函数体内
`from fspack.project import parse_project`（惰性导入），运行时才求值，打破循环。
`# noqa: PLC0415` 注释表明这是有意的（ruff.toml 全局忽略 PLC0415）。

### 2. 兼容包装保留原函数

`parse_project`/`analyze_dependencies`/`parse_wheel_filename` 保留为模块级函数，
内部委托类方法。理由：
- 避免破坏外部调用（这些函数在 `__all__` 中导出）
- 测试文件仍直接测试原函数（如 test_project.py 测 `parse_project`）
- 渐进式迁移：新代码用类方法，旧代码无需改动

### 3. pyrefly search-path 修复

用户 commit `66eec2c` 添加独立 `pyrefly.toml` 时 `search-path = ["."]`，导致
pyrefly 将 `src/fspack/config.py` 的本地类解析为 `src.fspack.config.ProjectInfo`
（按文件路径），而 `from fspack.project import parse_project` 的返回类型解析为
`fspack.config.ProjectInfo`（按 import 路径），strict 模式下视为不同类型报
`bad-return`。改为 `["src", "."]` 后，`src/` 优先使本地文件模块路径解析为
`fspack.config`，与 import 一致。

### 4. 不整合 embed/standalone 运行时管理

评估 `embed.py`（download_embed/extract_embed/ensure_embed）与 `standalone.py`
（download_standalone/extract_standalone/ensure_standalone）后决定不在本迭代整合：
- 这些是 IO 密集的函数（下载/解压），状态管理（缓存命中/字节统计）依赖
  `StageRecorder`，封装为类需引入运行时状态字段，复杂度高
- 当前模式（模块级函数 + StageRecorder 参数注入）清晰且测试充分
- 留到后续迭代评估是否引入 `Runtime`/`RuntimeManager` 类

## 代码实现情况

### WheelInfo.from_filename（纯解析，最简单）

```python
@classmethod
def from_filename(cls, filename: str) -> WheelInfo | None:
    """从 wheel 文件名构造实例，无法解析返回 None。."""
    m = _WHEEL_RE.match(filename)
    if m is None:
        return None
    return cls(
        name=m.group("name"),
        version=m.group("ver"),
        python_tags=tuple(m.group("py").split(".")),
        abi_tag=m.group("abi"),
        platform_tags=tuple(m.group("plat").split(".")),
    )
```

### ProjectInfo.from_dir / DependencyReport.from_src（惰性导入）

```python
@classmethod
def from_dir(cls, project_dir: Path, py_version: str | None = None) -> ProjectInfo:
    """从项目目录解析 pyproject.toml 并构造实例。

    惰性导入 :func:`fspack.project.parse_project` 打破 config ↔ project 循环依赖。
    """
    from fspack.project import parse_project

    return parse_project(project_dir, py_version)
```

## 整合优化情况

- 配置文件修复：补全用户独立配置文件（pyrefly.toml/ruff.toml）相对旧 pyproject.toml
  `[tool.*]` 段缺失的项（examples 排除、ARG005 忽略、search-path 调整）
- 测试去重：移除 `# noqa: ARG005`（per-file-ignores 已覆盖）

## 测试验证结果

### 门禁

- `ruff check`：All checks passed
- `ruff format --check`：45 files already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack`：306 passed，覆盖率 98.43%（iter-16 基线 98.42%）

### 新增测试

- `tests/test_wheel_cache.py`：+4（WheelInfo.from_filename）+1（兼容包装委托断言）
- `tests/test_config.py`：+5（from_dir/from_src 类方法）

## 遗留事项

- pyproject.toml 中 `[tool.pyrefly]`/`[tool.ruff]`/`[tool.coverage]`/`[tool.pytest]`/
  `[tool.bumpversion]` 段与独立配置文件重复，rule-11 约定"工具链独立配置文件，
  pyproject.toml 仅含项目元数据"，后续迭代可清理重复段
- embed/standalone 运行时管理是否整合为类，留待后续评估

## 下一轮计划

无。本迭代需求清单全部完成，门禁通过。
