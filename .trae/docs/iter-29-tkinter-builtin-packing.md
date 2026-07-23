# iter-29 tkinter 内置库打包

## 需求清单

- [x] 1. 新增 `packaging/builtin.py`，实现 `TkinterBundler`（req-24）
- [x] 2. 新增 `BuiltinError` 异常类
- [x] 3. `EntryWrapper.generate_wrapper_source` 新增 `has_tkinter` 参数
- [x] 4. `builder.py` 新增"补充内置库"阶段
- [x] 5. `packaging/__init__.py` 导出 `TkinterBundler`
- [x] 6. 测试覆盖（test_builtin.py + test_entry.py + test_builder.py 集成）
- [x] 7. 全套门禁通过（ruff/pyrefly/pytest/coverage 97.91%）
- [x] 8. 更新 README.md（移除 tkinter 限制章节，工作原理新增第 4 步）
- [x] 9. req-15 移到 done/（限制已消除，被 req-24 取代）

## 迭代目标

解决 embed python 不含 tkinter 的长期限制（req-15 原仅文档化）。
从 python-build-standalone Windows 构建提取 tkinter 组件补充到 embed python runtime，
使 Windows 打包的 tkinter 应用可直接运行。Linux standalone 已含完整 stdlib，无需补充。

## 改动文件清单

### 新增

- `src/fspack/packaging/builtin.py` —— `TkinterBundler` 类（3 层缓存：runtime→cache zip→下载）
- `tests/test_builtin.py` —— 15 个测试用例
- `.trae/req/req-24-tkinter-builtin-packing.md` —— 需求记录

### 修改

- `src/fspack/exceptions.py` —— 新增 `BuiltinError`，更新 `__all__`
- `src/fspack/packaging/entry.py` —— 模板注入 Tcl/Tk 环境变量，`generate_wrapper_source` 新增 `has_tkinter` 参数
- `src/fspack/packaging/__init__.py` —— 导出 `TkinterBundler`，文档注释新增 builtin 模块
- `src/fspack/builder.py` —— 新增 import + "补充内置库"阶段 + `has_tkinter` 传递
- `tests/test_entry.py` —— 新增 2 个 tkinter 环境变量注入测试
- `tests/test_builder.py` —— 新增 2 个 builder 集成测试（触发/不触发 tkinter 补充）
- `README.md` —— 移除"Windows 打包不支持 tkinter"章节，工作原理新增第 4 步

### 移动

- `.trae/req/req-15-tk-pack-limitation-doc.md` → `.trae/req/done/`（限制已消除）

## 关键决策与依据

1. **数据源选 python-build-standalone Windows 构建**：fspack 已有 `STANDALONE_BASE_URL` 和
   `STANDALONE_RELEASE_TAG`（Linux 运行时用），Windows 构建同源同 tag，URL 模式仅平台段不同。
   无需引入新依赖或新下载源。

2. **3 层缓存策略**：
   - L1 runtime 检查：`runtime/Lib/tkinter/__init__.py` 已存在 → 跳过（`fsp c` 清理前复用）
   - L2 cache zip：`~/.fspack/cache/tkinter/tkinter-{version}.zip` → 秒级解压
   - L3 下载 tarball：~40MB → 提取 tkinter 组件 → 生成 ~3-5MB 缓存 zip → 解压
   首次构建下载 40MB，后续构建秒级。standalone tarball 也缓存供复用。

3. **`_build_tkinter_zip` 文件映射**：
   - `.../tkinter/**` → `Lib/tkinter/...`（纯 Python 包，embed python 的 Lib/ 下）
   - `.../_tkinter*.pyd` → `_tkinter.pyd`（C 扩展，runtime 根目录，python.exe 同级）
   - `.../tcl{ver}/...` → `tcl/tcl{ver}/...`（Tcl 运行时脚本）
   - `.../tk{ver}/...` → `tcl/tk{ver}/...`（Tk 运行时脚本）
   用正则 `/(tcl\d+\.\d+)/`、`/(tk\d+\.\d+)/` 匹配版本目录，兼容 tcl8.6/tcl9.0 等。

4. **wrapper 注入 `TCL_LIBRARY`/`TK_LIBRARY`**：embed python 缺失 Tcl/Tk 脚本路径，
   `_tkinter.pyd` 加载时需显式指定。用 `glob.glob` 匹配 `runtime/tcl/tcl*` 目录，
   `os.environ.setdefault` 不覆盖用户已有设置。`{has_tkinter}` 渲染为 `True`/`False`
   字面量，编译期决定是否注入环境变量设置代码。

5. **Linux 不触发**：`is_needed()` 检查 `target is Platform.WINDOWS`，Linux standalone
   已含完整 stdlib（含 tkinter），无需补充。

6. **req-15 取代**：原 req-15 选择"策略 D：文档化限制"，本次彻底解决限制。
   req-15 移到 done/，README 移除限制章节。

## 代码实现情况

### TkinterBundler 类

```python
class TkinterBundler:
    @staticmethod
    def standalone_windows_tarball_name(version, release_tag) -> str
    @staticmethod
    def standalone_windows_url(version, release_tag) -> str
    @classmethod
    def is_needed(cls, ast_stdlib, target) -> bool
    @classmethod
    def ensure(cls, runtime_dir, version, cache_dir, stage) -> None  # 3 层缓存
    @staticmethod
    def _build_tkinter_zip(tar_path) -> bytes  # 提取 4 类组件
    @staticmethod
    def _unpack_tkinter_zip(zip_path, runtime_dir) -> None
```

### builder.py 集成

```python
has_tkinter = False
if TkinterBundler.is_needed(report.ast_stdlib, target):
    builtin_cache = Path.home() / ".fspack" / "cache"
    with tracker.stage("补充内置库") as st:
        TkinterBundler.ensure(runtime_dir, info.py_version, builtin_cache, stage=st)
        has_tkinter = True
        st.set_detail("tkinter")
```

### entry.py 模板注入

```python
# tkinter 环境变量（embed python 缺失 Tcl/Tk 脚本路径，需手动指定）
_RUNTIME_DIR = os.path.join(_DIST_DIR, "runtime")
if {has_tkinter}:
    import glob
    _tcl_lib = glob.glob(os.path.join(_RUNTIME_DIR, "tcl", "tcl*"))
    if _tcl_lib:
        os.environ.setdefault("TCL_LIBRARY", _tcl_lib[0])
    _tk_lib = glob.glob(os.path.join(_RUNTIME_DIR, "tcl", "tk*"))
    if _tk_lib:
        os.environ.setdefault("TK_LIBRARY", _tk_lib[0])
```

## 整合优化情况

- `BuiltinError` 继承 `FspackError`，归入既有异常层级
- `TkinterBundler` 复用 `Downloader`（packaging/net.py）下载 tarball
- `TkinterBundler` 复用 `STANDALONE_BASE_URL`/`STANDALONE_RELEASE_TAG`（packaging/runtime.py）
- `StageRecorder` 记录缓存命中与下载字节数，统一进度展示

## 测试验证结果

- ruff check：All checks passed
- ruff format：54 files already formatted
- pyrefly check：0 errors
- pytest：557 passed, 20 deselected（slow）
- coverage：97.91%（builder.py 93%，builtin.py 97%，entry.py 100%）

### 测试覆盖

- `test_builtin.py`（15 用例）：URL/名称生成、is_needed 4 种组合、_build_tkinter_zip 提取+
  内容保留+无 tkinter 报错、_unpack_tkinter_zip、ensure 三层缓存（runtime 就绪/cache zip/
  下载+缓存/tarball 缓存复用）
- `test_entry.py`（+2 用例）：has_tkinter=False 注入 `if False:`、has_tkinter=True 注入
  `if True:` + TCL/TK 环境变量
- `test_builder.py`（+2 用例）：AST 检出 tkinter 触发 ensure + wrapper 注入 `if True:`、
  未检出 tkinter 不触发 + wrapper 注入 `if False:`

## 遗留事项

- 无 tkinter 示例项目（tk_basic 已在 iter-15 删除）。若需端到端验证，可创建 `examples/tk_basic`
  并跑 slow 测试。当前实现通过单元测试 + builder 集成测试验证逻辑正确性。
- 实际网络下载 tkinter 组件未在 CI 验证（slow 测试需真实网络）。首次使用时下载 ~40MB tarball。

## 下一轮计划

- 视用户需求决定是否新增 tkinter 示例项目并补充 slow 端到端测试
- 无其他待办
