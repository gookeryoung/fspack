# 需求：代码、注释清理与功能优化

## 背景

iter-28 至 iter-31 完成模块整合（packaging 包抽象、console/entry/net 重构、
tkinter 内置库打包、favicon 自动 icon）后，项目结构趋于稳定。本次针对代码
质量进行清理与优化：消除死代码、移除违规导出、消除重复实现、归档产物。

## 需求

- [x] 1. P0-1：从 `slim/base.py` 的 `__all__` 移除 `override`（typing 工具不应
      作为公开 API 导出）
- [x] 2. P0-2：移除 `_parse_entries` 未使用的 `deps` 参数（死代码）
- [x] 3. P1-3：删除 `parse_wheel_filename` deprecated 函数及 `TestParseWheelFilenameCompat`
      测试类（req-17 已完成迁移到 `WheelInfo.from_filename`）
- [x] 4. P1-4：测试改为从 `fspack.slim.qt` 直接导入私有函数，移除 `slim/__init__.py`
      的私有函数 re-export 与 docstring「向后兼容」段
- [x] 5. P2-5：`SlimSpec.normalize_submodule`/`expand_closure` 从 `@abstractmethod`
      改为基类默认实现，消除 Numpy/Lxml/Matplotlib/Scipy/Default 五个子类共 10 个
      重复方法定义
- [x] 6. P2-6：提取 `_download_online` 的 sdist 回退逻辑为 `_handle_sdist_fallback`
      公共辅助函数，简化两处调用点
- [x] 7. P3-7：批量归档 17 个已完成的 req 文件到 `.trae/req/done/`
- [x] 8. P3-8：评估 `ensure_embed`/`ensure_standalone` 简化方案，确认函数式 API
      调用路径是测试 monkeypatch 设计（req-12），非真重复，保留现状
- [x] 9. P4-9：同步更新 `slim/base.py`、`slim/libs.py` 模块与类 docstring，
      反映 `normalize_submodule`/`expand_closure` 默认实现
- [x] 10. 全套门禁通过（ruff/format/pyrefly/pytest/coverage ≥ 95%）

## 验收标准

- `__all__` 不再导出 `override` 等第三方工具符号
- `parse_wheel_filename` deprecated 函数已删除，`WheelInfo.from_filename` 是唯一入口
- `slim/__init__.py` 不再 re-export 下划线开头的私有函数
- `SlimSpec` 子类不再重复实现默认行为（normalize_submodule/expand_closure）
- `_download_online` 的 sdist 回退逻辑无重复
- `.trae/req/` 仅保留未完成需求，已完成的在 `done/` 下
- 门禁全过，覆盖率不低于 97.80%（iter-31 基线）

## 关键决策

- **P3-8 保留现状**：`ensure_embed`/`ensure_standalone` 与基类 `ensure` 逻辑相同，
  但调用函数式 `download_*`/`extract_*` 是有意设计——测试通过 monkeypatch 模块级
  函数拦截调用路径。强行委托基类会破坏测试拦截能力（req-12 记录的设计决策）
- **P2-5 默认实现而非 Mixin**：直接在基类提供默认实现比提取 Mixin 更简单，
  符合「模块级函数优于 Mixin」原则。QtSlimSpec 仍覆盖这两个方法
- **P2-6 仅提取公共逻辑**：两处 sdist 回退的命令构造不同（uv 路径用 req_file，
  pip 路径用 filtered），仅提取「解析缺失包 + 构建」公共部分，不过度抽象
- **@override 方法无需 ARG003 noqa**：ruff 对 `@override` 装饰的方法不报 ARG003
  （子类必须匹配父类签名，未用参数是接口契约的一部分）
- **override 版本兼容导入**：项目目标 Python 3.8，`typing.override` 仅 3.12+
  可用，测试文件采用与源码相同的版本守卫模式

## 验证结果

- ruff check：All checks passed!
- ruff format --check：49 files already formatted
- pyrefly check：0 errors
- pytest：630 passed, 21 deselected, coverage 97.93%
