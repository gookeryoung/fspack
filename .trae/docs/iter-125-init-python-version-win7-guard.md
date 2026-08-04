# iter-125: fsp init 指定 Python 版本 + Win7 fastapi 拦截

## 需求清单

- [x] `fsp init --python-version <X.Y>` 覆盖模板默认 `requires-python` 下限
- [x] Win7 下选择 fastapi 模板时前置报错

## 迭代目标

1. 新增 `--python-version` CLI 参数，渲染后覆盖 `pyproject.toml` 的 `requires-python` 行
2. 新增 Win7 检测，FastAPI 模板在 Win7 下直接报错，避免用户到打包阶段才遇依赖失败

## 改动文件清单

- `src/fspack/cli_init.py`：新增 `_is_windows_7()`/`_format_requires_python()` 与
  `_WIN7_UNSUPPORTED_TEMPLATES`/`_REQUIRES_PYTHON_RE` 常量；`init_project()` 新增
  `python_version` 参数与 Win7+fastapi 拦截逻辑；import 增加 `re`
- `src/fspack/cli_parser.py`：`_add_init_subparser` 新增 `--python-version X.Y` 参数
- `src/fspack/cli.py`：`_run_init` 透传 `python_version=ns.python_version` 到 `init_project`
- `tests/test_init_list.py`：新增 19 个测试覆盖 python_version 覆盖、格式校验、
  Win7 检测、fastapi 拦截

## 关键决策与依据

### `--python-version` 只设下限不设上界

用户主动指定版本时生成 `>=X.Y`（无上界），而非 `>=X.Y,<3.12`。理由：

1. 用户指定版本意味着已知目标环境，不需要 fspack 默认上界限制
2. 不设上界更灵活，避免与未来 Python 版本不兼容
3. 覆盖整个约束（含 PySide2 的 `<3.11` 上界）语义清晰——用户主动指定即覆盖全部

### requires-python 行用正则替换

模板 `_pyproject()` 生成的 `pyproject.toml` 已含具体 `requires-python = "..."` 值
（非占位符），不能用 `default_variables` 替换。用 `re.MULTILINE` 正则匹配
`^requires-python = "[^"]*"$` 行整体替换。无该行时 fallback 到 `description` 行后
追加（防御性，现有模板均有此行）。

### Win7 检测用 sys.getwindowsversion()

Win7 的 NT 版本号是 6.1（RTM/SP1），Win8 是 6.2，Win10+ 是 10.0+。
`sys.getwindowsversion()` 返回命名元组含 `major`/`minor`。用
`(major, minor) == (6, 1)` 精确判定 Win7。非 Windows 系统直接返回 False。

用 `getattr(sys, "getwindowsversion", lambda: None)()` 兜底，避免非 Windows
平台 AttributeError；同时便于测试 monkeypatch。

### FastAPI Win7 不可用的根因

FastAPI 0.100+ 依赖 pydantic 2.x → pydantic-core（Rust 编写）→ Windows wheel
调用 Win8+ API（PathCchSkipRoot 等），Win7 缺失 `api-ms-win-core-path-l1-1-0.dll`
无法加载。fspack 已有该 DLL 的运行时注入机制（pyc.py `_inject_win7_compat_dll`），
但那是针对 Python 3.9+ embed python 启动；pydantic-core 的 wheel 本身调用 Win8+
API，注入 DLL 无法解决（pydantic-core 用的是 PathCchSkipRoot 而非 PathFileExists等）。

错误信息明确告知根因与替代方案（升级 Win10+ 或换用 flask 模板）。

### 黑名单设计而非硬编码

用 `_WIN7_UNSUPPORTED_TEMPLATES = frozenset({"fastapi"})` 而非 `if template_id == "fastapi"`，
便于未来扩展（如其他依赖 pydantic-core 的模板）。

## 代码实现情况

### cli_init.py 核心逻辑

```python
# Win7 兼容性检查（init_project 内，get_template 之后）
if template_id in _WIN7_UNSUPPORTED_TEMPLATES and _is_windows_7():
    raise ValueError(
        f"模板 {template_id!r} 在 Win7 下不可用：依赖 pydantic 2.x / pydantic-core，"
        "其 Windows wheel 调用 Win8+ API（如 PathCchSkipRoot），Win7 无法加载。"
        "请升级到 Win10+ 或换用 flask 模板。"
    )

# python_version 覆盖（render_template 之后，写文件之前）
if python_version is not None:
    requires_python = _format_requires_python(python_version)
    pyproject_path = Path("pyproject.toml")
    if pyproject_path in files:
        content = files[pyproject_path]
        new_line = f'requires-python = "{requires_python}"'
        if _REQUIRES_PYTHON_RE.search(content):
            files[pyproject_path] = _REQUIRES_PYTHON_RE.sub(new_line, content)
```

### CLI 参数声明

```python
p.add_argument(
    "--python-version",
    default=None,
    metavar="X.Y",
    help="指定目标 Python 版本（如 3.8、3.10），覆盖模板默认 requires-python 下限",
)
```

argparse 自动把 `--python-version` 映射到 `ns.python_version`（连字符转下划线）。

## 测试验证结果

### 新增测试（19 个）

- `test_cli_init_python_version_overrides_requires_python`：CLI `--python-version 3.10` 覆盖
- `test_cli_init_python_version_3_8_for_pyside2`：覆盖 PySide2 模板默认上界
- `test_cli_init_python_version_invalid_format_errors`：`3` 缺 minor 报错退出码 1
- `test_cli_init_python_version_non_numeric_errors`：`3.x` 非数字报错
- `test_init_project_python_version_none_keeps_default`：None 保持模板默认
- `test_format_requires_python_3_8/3_10/3_11`：格式化函数 3 个正例
- `test_format_requires_python_invalid_single_component/non_numeric/empty`：3 个反例
- `test_is_windows_7_non_windows_returns_false`：linux 平台 False
- `test_is_windows_7_win10_returns_false`：Win10 NT 10.0 False
- `test_is_windows_7_win7_returns_true`：Win7 NT 6.1 True
- `test_init_project_fastapi_on_win7_raises`：Win7+fastapi ValueError
- `test_init_project_fastapi_on_non_win7_succeeds`：非 Win7+fastapi 正常
- `test_init_project_helloworld_on_win7_succeeds`：Win7+helloworld 正常
- `test_cli_init_fastapi_on_win7_errors`：CLI 入口 Win7+fastapi 退出码 1

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 118 files already formatted
- pyrefly: 0 errors (11 suppressed)
- pytest: 1896 passed, 12 skipped
- coverage: 95.24%（TOTAL 6479 stmts, 266 miss），达到 95% 门禁

## 遗留事项

- `_format_requires_python` 当前只支持 `X.Y` 格式，不支持 `X.Y.Z` 或 PEP 440 约束
  （如 `>=3.8,<3.11`）。未来若需更灵活约束可加 `--requires-python` 参数直传。
- `_WIN7_UNSUPPORTED_TEMPLATES` 目前仅含 fastapi，未来若新增依赖 pydantic-core 的
  模板需同步加入黑名单。
- Win7 检测基于 `sys.getwindowsversion()` 的 NT 版本号，Win7 兼容模式（Win7 SP1
  伪装 Win10）可能误判，但兼容模式本身不常见且属用户主动行为。

## 下一轮计划

iter-125 完成 init 命令的功能增强。下一轮可继续推进 req-47 中未完成项：
- iter-92 wheel_pip.py + pipeline.py 拆分（中风险，需基线守护）
- iter-93 mixin Protocol 类型声明（依赖 iter-92）
- iter-95 AST 分析内存优化（性能基线守护）
