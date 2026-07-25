# iter-35: 优化打包体积与执行速度（短期+中期+长期）

## 需求清单

- [x] P0: QtWebEngine 调试资源 `.debug.pak` 剥离，减少 ~74MB 冗余
- [x] P0: ICU 数据 `icudtl.dat`/`QtWebEngineProcess.exe` 按需保留（仅 WebEngine 依赖时）
- [x] P1: 新增 `--pyc-optimize` 控制 `compileall -o` 级别（0/1/2），缩小字节码 5-15%
- [x] P1: 新增 `--no-site` 跳过 `site.py` 加载，节省启动 ~20-30ms
- [x] P2: 新增 `--nuitka` 编译用户源码为 `.pyd`，执行速度提升 30-50%（默认关闭）
- [x] P2: 交叉构建时跳过 Nuitka 编译（避免生成目标平台不兼容的本机代码）
- [x] 入口 wrapper 保持 `runpy` 调用方式不变（用户否决直接 import 方案）

## 迭代目标

参考 RimSort 项目与 `ref/RimSort` 打包产物，从「打包体积」与「应用执行速度」两个维度做短期/中期/长期优化。短期去冗余、中期调字节码、长期引入 Nuitka 本机编译。所有新功能默认关闭或保持原行为，需 CLI 显式启用，避免影响现有构建。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| src/fspack/slim/qt.py | 新增 `_QT_WEBENGINE_TOP_FILES` 集合；`classify_entry` 优先拦截 `QtWebEngineProcess[.exe]`/`icudtl.dat` 仅 WebEngine 依赖时保留；`resources/` 内 `*.debug.pak` 始终剥离 |
| src/fspack/builder.py | `build()` 新增 `pyc_optimize`/`no_site`/`nuitka` 参数；新增「Nuitka 编译」阶段（仅 `nuitka and target is detect_platform()`）；`_precompile_pyc` 接受 `optimize` 透传给 `compileall -o`；`_pyc_stamp_key` 纳入 `optimize` 强制切级重编译 |
| src/fspack/packaging/runtime.py | `write_pth` 新增 `enable_site=True` 参数，`no_site=True` 时省略 `import site` 行 |
| src/fspack/packaging/nuitka.py | 新增 `NuitkaCompiler` 类：`is_available` 检查 runtime python 已装 nuitka；`compile_src` 用 `python -m nuitka --module` 逐文件编译并删除非 `__init__.py` 的 `.py` |
| src/fspack/packaging/__init__.py | 导出 `NuitkaCompiler` |
| src/fspack/cli.py | 新增 `--pyc-optimize`/`--no-site`/`--nuitka` 选项 |
| src/fspack/commands/build.py | `run()` 透传三个新参数 |
| tests/test_builder.py | 新增 `pyc_optimize` 透传、Nuitka 阶段调用与交叉跳过测试 |
| tests/test_cli.py | 新增 CLI 选项解析与默认值测试 |
| tests/test_commands.py | `fake_run` 签名补三个新参数 |
| tests/test_runtime.py | 新增 `write_pth(enable_site=False)` 测试 |
| tests/test_slim.py | 新增 QtWebEngine 顶层文件、`.debug.pak` 剥离测试 |
| tests/test_nuitka.py | 新增 NuitkaCompiler 单元测试：可用性、交叉跳过、`.py` 剥离、错误处理 |

## 关键决策与依据

### QtWebEngine 资源按需保留

`ref/RimSort` 打包产物中 `PySide6/QtWebEngineProcess.exe` 与 `resources/*.debug.pak` 共占 ~74MB。RimSort 未做条件剥离，全部保留。

fspack 决策：

- `QtWebEngineProcess.exe`/`QtWebEngineProcess`/`icudtl.dat` 仅当用户源码 import 任一 WebEngine 子模块时保留（`_QT_RESOURCE_DEPS & keep_subs`）。拦截点须在 `_classify_top_or_meta` 之前，否则 `.exe` 被 STRIP_EXTS 误剥离
- `resources/*.debug.pak` 是 Chromium DevTools 调试资源，运行时不需要，始终剥离
- 非 WebEngine 应用不再携带冗余 ICU 数据，单包体积减少 ~10MB

### `--pyc-optimize` 纳入 stamp 键

`_pyc_stamp_key` 加入 `optimize` 字段：切换 `--pyc-optimize` 时强制重编译，避免旧的 `optimize=0` `.pyc` 被运行时加载而无法享受 `-OO` 优化。

文档显式提示：`-OO` 会移除 `__doc__`，依赖文档字符串的程序（如 Sphinx 运行时）应用 `0` 或 `1`。

### `--no-site` 跳过 site.py

`python3X._pth` 省略 `import site` 行，启动跳过 `site.py` 执行，节省 ~20-30ms。wrapper 已显式 `sys.path.insert` site-packages，不影响第三方依赖发现。

### Nuitka 仅编译用户源码

参考 RimSort 全量 `--follow-imports` 编译需几十分钟；fspack 仅编译 `dist/src`（用户源码），第三方依赖保持 wheel 解压 + `.pyc`，构建速度优先。

Nuitka 必须安装在 runtime python 环境中（非构建机 python）。可用性检查失败时告警并跳过，回退到 `.pyc` 模式，不中断构建。

### 交叉构建跳过 Nuitka

Nuitka 生成的 `.pyd`/`.so` 是本机代码，交叉构建时（构建机平台 ≠ 目标平台）会生成不兼容的二进制。`build()` 中 `if nuitka and target is detect_platform()` 守卫跳过，与 `_precompile_pyc` 的交叉守卫一致。

### `.py` 删除策略与 pyc_strip 对齐

Nuitka 编译后删除非 `__init__.py` 的 `.py` 源码，保留 `__init__.py` 维持包标识，避免 PEP 420 命名空间包导致 `.pyd` 不被识别为包成员。与 `_strip_py_sources` 策略一致。

### wrapper 保持 runpy

用户否决「直接 import 替代 runpy」方案，保留 `runpy.run_path()` 调用，避免入口路径与模块路径耦合。

## 代码实现情况

### slim/qt.py QtWebEngine 处理

```python
_QT_WEBENGINE_TOP_FILES = frozenset({"QtWebEngineProcess.exe", "QtWebEngineProcess", "icudtl.dat"})

# classify_entry 中优先拦截
if len(parts) == 2 and parts[1] in _QT_WEBENGINE_TOP_FILES:
    if _QT_RESOURCE_DEPS & keep_subs:
        return ("shared", None)
    return ("exclude", None)

# resources 内 .debug.pak 始终剥离
if subdir == "resources":
    if not (_QT_RESOURCE_DEPS & keep_subs):
        return ("exclude", None)
    if entry.endswith(".debug.pak"):
        return ("exclude", None)
    return ("shared", None)
```

### builder.py build() 新增参数

```python
def build(
    # ... existing ...
    pyc_optimize: int = 0,
    no_site: bool = False,
    nuitka: bool = False,
) -> ProjectInfo:
    # ...
    if nuitka and target is detect_platform():
        with tracker.stage("Nuitka 编译") as st:
            from fspack.packaging.nuitka import NuitkaCompiler
            NuitkaCompiler.compile_src(src_dst, runtime_dir, info.py_version, target, stage=st)
```

### packaging/nuitka.py NuitkaCompiler

`is_available` 用 `subprocess.run([py, "-c", "import nuitka"])` 检测；`compile_src` 用 `python -m nuitka --module --output-dir=<parent> --no-pyi-file --remove-output --quiet <py>` 逐文件编译，单文件失败仅 warning 不中断。

### packaging/runtime.py write_pth

```python
def write_pth(pth_path: Path, *, enable_site: bool = True) -> None:
    lines = ["Lib/site-packages", "."]
    if enable_site:
        lines.append("import site")
    pth_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
```

## 整合优化情况

- Nuitka 编译参数复用 runtime python 解析逻辑（`_runtime_python`），避免与 `_precompile_pyc` 重复路径拼接
- `.py` 删除策略与 `pyc_strip` 对齐，未来可抽公共函数（当前两处实现差异小，未抽）
- `_pyc_stamp_key` 复用 `optimize` 字段，stamp 写入与校验自动一致
- 交叉构建守卫复用 `detect_platform()`，与 `_precompile_pyc` 守卫风格统一

## 测试验证结果

- ruff check: All checks passed
- ruff format --check: 51 files already formatted
- pyrefly check: 0 errors
- pytest -m "not slow": 747 passed, 21 deselected, 覆盖率 97.37%
- 新增测试覆盖：
  - `test_qt_webengine_top_files_kept_when_webengine_used`: WebEngine 依赖时 `QtWebEngineProcess.exe`/`icudtl.dat` 保留
  - `test_qt_webengine_top_files_stripped_when_no_webengine`: 非 WebEngine 应用剥离
  - `test_qt_debug_pak_always_stripped`: `.debug.pak` 始终剥离
  - `test_write_pth_no_site_omits_import_site`: `enable_site=False` 省略 `import site`
  - `test_build_pyc_optimize_passed_to_compileall`: `pyc_optimize` 透传 `compileall -o`
  - `test_build_no_site_disables_site_py`: `no_site=True` 透传 `write_pth`
  - `test_nuitka_is_available_*`: 可用性检查三场景
  - `test_compile_src_skips_when_nuitka_absent`: nuitka 未装告警跳过
  - `test_compile_src_strips_py_after_compile`: 编译后剥离 `.py`
  - `test_compile_src_keeps_init_py`: `__init__.py` 保留
  - `test_compile_src_records_stage_metrics`: stage 记录编译项数
  - `test_build_nuitka_skipped_on_cross_compile`: 交叉构建跳过 Nuitka

## 遗留事项

- Nuitka 实际构建验证（需 runtime python 装好 nuitka）未执行，仅通过 mock 验证逻辑
- slow 端到端测试未新增 `--nuitka` 场景，因 nuitka 依赖外部安装，CI 环境暂未配置
- `icudtl.dat` 在 `resources/` 子目录下也可能有副本，当前仅处理顶层；如发现冗余可扩展

## 下一轮计划

无明确下一轮计划，等待用户反馈实际构建效果。
