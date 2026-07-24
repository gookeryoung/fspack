# iter-34: 构建阶段体现精简解压与预编译

## 需求清单

- [x] P0: 「解压 wheel」重命名为「解压 wheel(精简)」体现 slim_unpack 按需解压语义
- [x] P1: 新增「精简标准库」阶段，剥离 Linux standalone 的 test/ensurepip/idlelib 等无用目录
- [x] P1: 新增「预编译字节码」阶段，用 runtime python 编译 src+site-packages 为 .pyc 加速首次启动
- [x] CLI 新增 `--no-stdlib-trim`/`--no-pyc`/`--pyc-strip` 选项控制新功能
- [x] 交叉构建时（构建机平台≠目标平台）自动跳过预编译

## 迭代目标

把精简解压体现在构建阶段汇总中（如「解压 wheel(精简)」），把预编译作为独立过程体现到汇总统计中，让用户清晰看到 slim_unpack 与预编译的执行情况。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| src/fspack/builder.py | 新增 `_trim_stdlib`/`_precompile_pyc` 函数；`build()` 新增「精简标准库」「预编译字节码」阶段；「解压 wheel」重命名为「解压 wheel(精简)」；交叉构建守卫 |
| src/fspack/cli.py | 新增 `--no-stdlib-trim`/`--no-pyc`/`--pyc-strip` 选项 |
| src/fspack/commands/build.py | `run()` 新增 `no_stdlib_trim`/`no_pyc`/`pyc_strip` 参数透传 |
| tests/test_builder.py | 新增 `_trim_stdlib`/`_precompile_pyc` 单元测试；`build()` 阶段汇总集成测试 |
| tests/test_cli.py | 新增 CLI 选项解析与默认值测试 |
| tests/test_commands.py | 新增参数透传测试 |

## 关键决策与依据

### 阶段命名体现精简语义

「解压 wheel」→「解压 wheel(精简)」：slim_unpack 按需解压（AST 子模块分析 + Qt 闭包），剥离未用 .pyd/.so/Qt5*.dll。阶段名加「(精简)」后缀让用户直观看到精简已生效。

### 精简标准库仅对 Linux standalone 生效

Windows embed zip 已由官方精简（无 test/ensurepip/idlelib），`_trim_stdlib` 检测到 Windows 目标时设 detail「embed zip 已精简，跳过」并跳过。Linux standalone 含完整 stdlib，需剥离 `test`/`ensurepip`/`idlelib`/`pydoc_data`/`turtledemo` 五个目录。

### 预编译用 runtime 自身 python

用 runtime 的 `python.exe`（Windows）或 `python{ver}`（Linux）调 `compileall` 编译 src + site-packages。确保 ABI 兼容（runtime python 版本与目标一致）。

### 交叉构建跳过预编译

`build()` 中 `if not no_pyc and target is detect_platform()` 守卫。交叉构建时（如 Windows 构建机 + Linux 目标）runtime python 是 Linux 二进制，无法在 Windows 执行 compileall，自动跳过预编译阶段。`_trim_stdlib` 不受影响（仅删除目录，不执行 runtime python）。

### pyc_strip 保留 __init__.py

`--pyc-strip` 删除非 `__init__.py` 的 `.py` 源码（仅保留 `.pyc`）。保留 `__init__.py` 避免 PEP 420 命名空间包导致 `.pyc` 不加载。

## 代码实现情况

### `_trim_stdlib`

```python
_STDLIB_TRIM_DIRS = ("test", "ensurepip", "idlelib", "pydoc_data", "turtledemo")

def _trim_stdlib(runtime_dir, py_version, target, stage):
    if target is not Platform.LINUX:
        stage.set_detail("embed zip 已精简，跳过")
        return
    major, minor = py_version.split(".")[:2]
    stdlib = runtime_dir / "python" / "lib" / f"python{major}.{minor}"
    if not stdlib.is_dir():
        stage.set_detail("stdlib 目录不存在，跳过")
        return
    removed = 0
    for name in _STDLIB_TRIM_DIRS:
        d = stdlib / name
        if d.is_dir():
            shutil.rmtree(d)
            removed += 1
    stage.skip(removed)
    stage.set_detail(f"移除 {removed} 个目录")
```

### `_precompile_pyc`

用 `subprocess.run([py_exe, "-m", "compileall", d, "-q", "-f", "-j", "0"])` 编译。`strip_py=True` 时 `rglob("*.py")` 删除非 `__init__.py` 文件。runtime python 不存在时 warning 跳过。compileall 非零退出码仅 warning 不抛异常。

### `build()` 阶段编排

```
解析项目 → 下载运行时 → 解压运行时 → 精简标准库 → 分析依赖 → [补充内置库] → 下载依赖 → 解压 wheel(精简) → 复制源码 → 预编译字节码 → 生成 C loader
```

## 整合优化情况

- 移除 `subprocess.run` 上多余的 `# noqa: S603` 注释（ruff RUF100 报未启用规则）
- 交叉构建守卫 `target is detect_platform()` 复用已有 `detect_platform()` 导入

## 测试验证结果

- ruff check: All checks passed
- ruff format --check: 49 files already formatted
- pyrefly check: 0 errors
- pytest -m "not slow": 690 passed, 覆盖率 97.53%
- 新增测试：
  - `test_trim_stdlib_linux_strips_unwanted_dirs`: Linux 剥离 5 目录保留有用模块
  - `test_trim_stdlib_windows_skips`: Windows 跳过
  - `test_trim_stdlib_missing_stdlib_skips`: stdlib 目录不存在跳过
  - `test_precompile_pyc_windows_calls_compileall`: Windows 调 compileall 2 次
  - `test_precompile_pyc_linux_uses_python3_bin`: Linux 用 python3.11
  - `test_precompile_pyc_strip_deletes_non_init_py`: strip 删非 __init__.py
  - `test_precompile_pyc_strip_keeps_init_py`: strip 保留 __init__.py
  - `test_precompile_pyc_python_missing_skips`: runtime python 缺失跳过
  - `test_precompile_pyc_compileall_failure_warns_not_raises`: compileall 失败仅 warning
  - `test_build_includes_new_stages_in_summary`: 阶段汇总含三个新阶段名
  - `test_build_no_pyc_skips_precompile_stage`: --no-pyc 跳过预编译
  - `test_build_no_stdlib_trim_skips_trim_stage`: --no-stdlib-trim 跳过精简标准库

## 遗留事项

- slow 端到端测试（test_build_and_run_linux_helloworld/clitool）因用户跳过未验证，但交叉构建守卫逻辑已通过单测覆盖

## 下一轮计划

无明确下一轮计划，等待用户反馈。
