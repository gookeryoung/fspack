# 修复 pip 探测与测试套件 Windows 兼容性

## Context

用户在 Windows 上运行 `fspack b` 构建 guicalc 项目时，于"分析依赖"阶段抛出 `DependencyError: 未找到可用的 pip`。根因是 `_find_pip_python` 在 PATH 中只找 `python3`，而 Windows 系统的 Python 标准命名是 `python.exe`；当 uv 管理的 venv 默认不含 pip 时（用户当前场景），既无法用 `sys.executable`，也无法在 PATH 中回退到系统 python。

随后跑测试套件又发现 5 个用例在 Windows 上失败：
- 4 个 `_build_cmd` 测试因 `Path("/tmp/app.exe")` 在 Windows 上 `str()` 化为 `\tmp\app.exe`，与硬编码期望 `/tmp/app.exe` 不符。
- 1 个 `test_build_deb_creates_deb` 因 `wrapper.chmod(0o755)` 在 Windows 上不设置 Unix 可执行位，`st_mode & 0o111` 恒为 0。

目标：让 `fspack b` 在 Windows + uv venv（无 pip）场景下能找到系统 python 跑 pip；同时让测试套件在 Windows 上全绿，不放松 Linux 上的覆盖。

## 方案

### 1. 修复 `_find_pip_python` 候选名（生产代码）

**文件**：[src/fspack/builder.py](file:///f:/Dev/fspack/src/fspack/builder.py)

新增模块级常量 `_PIP_PYTHON_NAMES`，按 `sys.platform` 决定候选名：
- `win32`：`("python.exe", "python3.exe")`（标准 python.exe + Microsoft Store 的 python3.exe stub）
- 其他：`("python3", "python")`

`_find_pip_python` 内层 `for path_dir` 循环里，对每个目录遍历 `_PIP_PYTHON_NAMES` 全部名字（原来只检查 `python3` 一个）。其余逻辑（venv_bin 跳过、`Path.resolve` 异常吞掉、`subprocess.run` 验证 pip）保持不变。

docstring 同步更新：把"遍历 PATH 找系统 `python3`"改为"遍历 PATH 找系统 python（Windows: `python.exe`/`python3.exe`；其他: `python3`/`python`）"。

### 2. 更新 `test_builder.py` 中 `_find_pip_python` 相关测试

**文件**：[tests/test_builder.py](file:///f:/Dev/fspack/tests/test_builder.py)

把 `from fspack.builder import ...` 行加入 `_PIP_PYTHON_NAMES`。

5 个测试中硬编码的 `python3` 文件名改为 `_PIP_PYTHON_NAMES[0]`（每个平台的主候选名）：
- `test_find_pip_python_falls_back_to_system`（line 155）
- `test_find_pip_python_skips_venv_dir`（line 173）
- `test_find_pip_python_all_fail`（line 192）
- `test_find_pip_python_skips_dir_without_python3`（line 228，docstring 也改"无系统 python"）
- `test_find_pip_python_skips_unresolvable_dir`（line 251）

`test_find_pip_python_skips_dir_without_python3` 的函数名保留（重命名公共测试名意义不大，且 docstring 会说明）。

### 3. 修复 `test_commands.py` 中 `_build_cmd` 测试路径断言

**文件**：[tests/test_commands.py](file:///f:/Dev/fspack/tests/test_commands.py)

4 个测试把硬编码的期望路径改为 `str(exe)`，让期望值与 `_build_cmd` 实际返回值在同一平台规范化：
- `test_build_cmd_linux_with_wine`（line 113-114）：`assert cmd == ["/usr/bin/wine", str(Path("/tmp/app.exe"))]`
- `test_build_cmd_linux_wine_missing`（line 120-121）：`assert cmd == ["wine", str(Path("/tmp/app.exe"))]`
- `test_build_cmd_non_linux`（line 126-127）：`assert cmd == [str(Path("/tmp/app.exe"))]`
- `test_build_cmd_linux_native`（line 133-134）：`assert cmd == [str(Path("/tmp/app"))]`

`wine` 路径 `/usr/bin/wine` 是 mock `shutil.which` 返回的字符串（非 Path），保持原样。

### 4. 修复 `test_build_deb_creates_deb` 可执行位断言

**文件**：[tests/test_linux_installer.py](file:///f:/Dev/fspack/tests/test_linux_installer.py)

line 114 的 `assert wrapper.stat().st_mode & 0o111, "wrapper 无可执行位"` 用 `os.name == "posix"` 守卫：

```python
if os.name == "posix":
    assert wrapper.stat().st_mode & 0o111, "wrapper 无可执行位"
```

文件顶部已 `import subprocess` 等，需补 `import os`（若尚未引入）。生产代码 [linux_installer.py:63](file:///f:/Dev/fspack/src/fspack/linux_installer.py#L63) 的 `wrapper.chmod(0o755)` 不动——Linux 上正确，Windows 上无副作用（.deb 本来就是 Linux 包）。

## 不改动的部分

- `_find_pip_python` 的 `subprocess.run([py, "-m", "pip", "--version"], ...)` 验证方式保留：能跑通即说明该 python 有 pip 模块。
- `_build_cmd` 生产代码不动：它用 `str(exe)` 是对的，是测试断言没匹配运行平台。
- `wrapper.chmod(0o755)` 生产代码不动：Linux 上正确，Windows 上无副作用。
- 错误信息文案不动（"未找到可用的 pip，请在当前 venv 执行 `uv pip install pip`..."）：仍然准确，是最后的兜底提示。

## 验证

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyrefly check
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95
```

预期：
- ruff/pyrefly 全过
- pytest 全绿（Windows 上 156 通过，0 失败；覆盖率 ≥ 95%）
- 在 `tests/examples/guicalc` 目录手动跑 `fspack b`，应能进入"下载依赖 wheel"阶段（不再因 pip 探测失败而中断；如 guicalc 无第三方依赖则直接到"复制源码"）
