# iter-32 代码、注释清理与功能优化

## 需求清单

- [x] P0-1：从 `slim/base.py` 的 `__all__` 移除 `override`
- [x] P0-2：移除 `_parse_entries` 未使用的 `deps` 参数
- [x] P1-3：删除 `parse_wheel_filename` deprecated 函数及兼容性测试类
- [x] P1-4：测试改为从 `fspack.slim.qt` 直接导入私有函数，移除 `slim/__init__.py` 私有 re-export
- [x] P2-5：`SlimSpec.normalize_submodule`/`expand_closure` 改为基类默认实现，消除 5 个子类共 10 个重复方法
- [x] P2-6：提取 `_download_online` 的 sdist 回退逻辑为 `_handle_sdist_fallback`
- [x] P3-7：批量归档 17 个已完成 req 文件到 `done/`
- [x] P3-8：评估 `ensure_embed`/`ensure_standalone` 简化方案，确认保留现状（测试 monkeypatch 设计）
- [x] P4-9：同步更新 `slim/base.py`、`slim/libs.py` docstring 反映默认实现
- [x] 全套门禁通过（ruff/format/pyrefly/pytest/coverage 97.93%）

## 迭代目标

iter-28 至 iter-31 完成模块整合（packaging 包抽象、console/entry/net 重构、
tkinter 内置库打包、favicon 自动 icon）后，项目结构趋于稳定。本次针对代码
质量进行系统清理与优化：消除死代码、移除违规导出、消除重复实现、归档产物。

## 改动文件清单

- `src/fspack/slim/base.py`：`__all__` 移除 `override`；`normalize_submodule`/
  `expand_closure` 从 `@abstractmethod` 改为默认实现（原样返回/返回副本）；
  更新类与模块 docstring 反映默认实现语义
- `src/fspack/slim/qt.py`：新增 `sys.version_info` 版本守卫导入 `override`；
  删除 2 个冗余方法（现由基类提供默认实现）
- `src/fspack/slim/default.py`：新增 `override` 导入；删除 2 个冗余方法；
  移除 `match` 上的冗余 `# noqa: ARG003`
- `src/fspack/slim/libs.py`：新增 `override` 导入；删除 8 个冗余方法（4 个子类
  各 2 个）
- `src/fspack/slim/__init__.py`：移除 `_qt_module_closure`/`_qt_dll_submodule`
  /`_normalize_qt_sub` 私有函数 re-export 与「向后兼容」docstring 段
- `src/fspack/config.py`：`_parse_entries` 移除未使用的 `deps` 参数及调用点
- `src/fspack/packaging/wheels.py`：提取 sdist 回退公共逻辑为
  `_handle_sdist_fallback`；修复裸 `raise` 为 `raise e from None` 保留异常上下文
- `tests/test_slim.py`：新增 `override` 版本兼容导入（3.12+ 从 `typing`，
  低版本从 `typing_extensions`）；删除 `TestParseWheelFilenameCompat` 兼容性
  测试类；测试方法内私有函数改为从 `fspack.slim.qt` 直接导入；移除 `@override`
  方法上冗余的 `# noqa: ARG003`（ruff 对 @override 方法不报 ARG003）
- `.trae/req/done/`（新增 17 个文件）：req-06~req-25 已完成需求归档
- `.trae/req/req-26-code-cleanup-and-refactor.md`（新增）：本次需求记录

## 关键决策与依据

### P2-5 默认实现而非 Mixin

直接在 `SlimSpec` 基类提供 `normalize_submodule`/`expand_closure` 默认实现，
比提取 Mixin 更简单，符合 rule-11「模块级函数优于 Mixin」原则。QtSlimSpec 仍
覆盖这两个方法（归一化 + 依赖闭包），其他 5 个子类（Numpy/Lxml/Matplotlib/
Scipy/Default）删除冗余实现，共减少 10 个方法定义。

### P3-8 保留函数式 API

`ensure_embed`/`ensure_standalone` 与基类 `ensure` 逻辑相同，但调用函数式
`download_*`/`extract_*` 是有意设计——测试通过 monkeypatch 模块级函数拦截
调用路径（req-12 记录的设计决策）。强行委托基类会破坏测试拦截能力，保留现状。

### P2-6 仅提取公共逻辑

两处 sdist 回退的命令构造不同（uv 路径用 req_file，pip 路径用 filtered），
仅提取「解析缺失包 + 构建」公共部分为 `_handle_sdist_fallback`，不过度抽象。
裸 `raise` 修复为 `raise e from None` 保留异常上下文，符合 rule-11 异常处理
要求。

### @override 方法无需 ARG003 noqa

ruff 对 `@override` 装饰的方法不报 ARG003（子类必须匹配父类签名，未用参数
是接口契约的一部分）。移除测试中 `classify_entry` 方法参数上的 `# noqa:
ARG003`，消除 RUF100 冗余指令。

### override 版本兼容导入

项目目标 Python 3.8（`.python-version=3.8`），`typing.override` 仅 3.12+
可用。测试文件采用与源码相同的版本守卫模式：`if sys.version_info >= (3, 12)`
从 `typing` 导入，否则从 `typing_extensions` 导入。避免 pyrefly 报
`missing-module-attribute`。

## 代码实现情况

- `slim/base.py`：`SlimSpec.normalize_submodule` 默认返回 `sub`，
  `expand_closure` 默认返回 `set(subs)`（副本，不就地修改）
- `slim/qt.py`：保留 `normalize_submodule`（`_normalize_qt_sub`）与
  `expand_closure`（`_qt_module_closure`）覆盖实现
- `packaging/wheels.py`：`_handle_sdist_fallback(e, py, pypi_index, cache_dir)`
  返回缺失包列表，调用方据此重试下载
- `config.py`：`_parse_entries(project_dir, entries_tbl)` 签名简化

## 整合优化情况

- 消除 10 个重复方法定义（5 个子类 × 2 个方法）
- 移除 `slim/__init__.py` 的 3 个私有函数 re-export，降低耦合
- 提取 sdist 回退公共逻辑，减少代码重复
- 归档 17 个已完成 req 文件，保持 `.trae/req/` 整洁

## 测试验证结果

- ruff check：All checks passed!
- ruff format --check：49 files already formatted
- pyrefly check：0 errors (18 suppressed, 5 warnings not shown)
- pytest -m "not slow" --cov=fspack：630 passed, 21 deselected,
  coverage 97.93%（≥ 95% 门禁，≥ 97.80% iter-31 基线）
- `slim/base.py` 100% 覆盖，`slim/qt.py` 100% 覆盖，`slim/libs.py` 100% 覆盖

## 遗留事项

无。

## 下一轮计划

无（本次为代码质量清理，无后续计划）。如需进一步优化，可考虑：
- 评估 `packaging/runtime.py` 覆盖率 90%（最低）的未覆盖分支是否可补测试
- 评估 `builder.py` 覆盖率 94% 的未覆盖分支是否可补测试
