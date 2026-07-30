# iter-114：重复/无效代码扫描与 docstring 修复

## 需求清单

- [x] 扫描 packaging/ 子包及顶层模块识别重复代码、无效代码、潜在 BUG
- [x] 修复确认的 docstring 错误
- [x] 全套门禁通过（ruff/pyrefly/pytest 1835 passed/coverage ≥ 95%）

## 迭代目标

iter-113 完成 packaging/ 37 文件拆分为 5 子包后，本轮对全仓做系统性
"重复代码 + 无效代码 + 潜在 BUG" 扫描，确认子包拆分未引入回归并清理
遗留问题。

## 扫描方法与结论

### 1. 静态门禁基线

- `ruff check . --select F`：All checks passed（无 unused import / dead code）
- `ruff check .`（含 B/SIM/UP/C4/PL/PTH/RUF）：All checks passed
- `ruff format --check .`：114 files already formatted
- `pyrefly check`：0 errors（10 suppressed, 6 warnings not shown）

### 2. 重复代码识别（search subagent + 人工 Grep 验证）

search subagent 初版清单 30 条，人工 Grep 验证后**大部分为误判**：

- `_cleanup_build_dirs`/`_collect_py_files`/`_create_bootstrap_script`/
  `_is_nuitka_cached`/`_find_package_root` 等在 `nuitka/protocol.py` 与
  具体子模块（strip/compile/env/verify）的"重复"——**实为 Protocol 接口
  声明（`...` 占位）+ 真实实现**，不是重复（iter-111 引入的 Protocol 方案）
- `loader/compile.py` 的 `_supports_icon`/`_prepare_icon` 两次定义——
  **基类默认实现 + WindowsLoader override**，标准 OOP 模式
- `_dir_size` 在 `doctor_envs.py`/`sync.py`/`size_report.py` 三处定义——
  **签名不同**（int vs tuple[int,int]）、**性能要求不同**（sync 用 scandir
  优化，doctor/report 不敏感）、**跨层依赖问题**（提取会引入顶层依赖
  packaging 子包），不宜统一

### 3. 无效代码识别

- ruff F401 全过 → 无 unused import
- 无未引用的私有函数（grep 验证 `_xxx` 定义均有调用点）
- 无 unreachable branches（无 `if False:`/`return` 后语句）
- 无 TODO/FIXME/HACK 标记（仅 docstring 提及 "facade" 字样）

### 4. 潜在 BUG 识别

#### 4.1 路径计算（iter-113 拆分后重点核查）

- `pyc.py:103` `Path(__file__).parent.parent / "assets" / "runtime" / ...`
  → `src/fspack/packaging/pyc.py` 的 parent.parent = `src/fspack/`，正确
- `pipeline/stages.py:86` `Path(__file__).parent.parent.parent / "assets" / "icons" / ...`
  → `src/fspack/packaging/pipeline/stages.py` 的 parent.parent.parent = `src/fspack/`，正确
（iter-113 已修复深度，本轮复核确认无问题）

#### 4.2 异常处理

- `cli.py:770` `except BaseException` —— 批量打包循环中捕获单项目失败
  继续下一个，前置 `except SystemExit: raise` 保证 SystemExit 不被吞，
  注释清晰，**合理**
- 无裸 `except:`、无 `except Exception: pass`

#### 4.3 assert 语句（6 处）

- `pipeline/stages.py:220,244`：`assert tar_path/zip_path is not None`
  —— pyrefly 类型缩窄（跨 with 块无法跟踪 runtime_ready 状态），逻辑正确
- `runtime.py:261,303,305,313,315`：`assert isinstance(...)` —— 基类
  `**kwargs: object` 的运行时类型缩窄，合理
- `slim/unpack.py:147`：`assert zf.filename is not None` —— ZipFile 从
  文件路径打开必有 filename，合理
- `nuitka/compile.py:107`：`assert stream is not None` —— Popen 用 PIPE
  时 stream 必非 None，合理

**结论**：均为 pyrefly 类型缩窄用法，非保护性检查，`-O` 移除后逻辑仍正确
（后续自然抛 TypeError/AttributeError）。不建议改 raise（增加噪音无收益）。

#### 4.4 group() 调用安全

- `sbom.py:187`/`config/parsing.py:527-528`/`builtin.py:196,204` 等均前置
  `if not m: return/continue` 检查，安全

#### 4.5 其他

- 无 `eval`/`exec`
- 无可变默认参数（`def f(x=[])`）
- 无未关闭 `open()`（都用 `with` 或 `write_text`）
- `write_text` 调用均显式 `encoding="utf-8"`
- `_compat.py` 的 tomli/typing_extensions 兜底必要（`requires-python>=3.8`）

## 改动文件清单

### src/fspack/packaging/loader/compile.py

- L273 LinuxLoader.`_build_command` 参数注释：
  `"Linux 用 windres 而非 icon_obj"` → `"Linux 不支持 icon 资源嵌入"`
  - 原注释错误：Linux 不用 windres（windres 是 Windows mingw 工具），
    与同文件 MacLoader L312 `"macOS 不支持 icon 资源"` 注释风格对齐

## 关键决策与依据

1. **不提取 `_dir_size` 三处实现**：签名不同（int vs tuple）、性能要求不同
   （sync 用 scandir 缓存优化，doctor/report 不敏感）、跨层耦合问题（doctor_envs
   在顶层，依赖 packaging.sync 会引入跨层依赖）。三处各 ~10 行，重复程度可接受。

2. **不修改 assert 语句**：均为 pyrefly 类型缩窄的合理用法，非保护性检查。
   改为 raise 会增加噪音且 `-O` 模式下 assert 移除后逻辑仍正确（后续自然
   抛异常），无实际收益。

3. **仅修复 docstring**：search subagent 清单 30 条人工验证后仅 1 条确认为
   真 BUG（loader/compile.py:273 注释错误），其余均为误判或合理设计。

## 测试验证结果

- ruff check：All checks passed
- ruff format --check：114 files already formatted
- pyrefly check：0 errors（10 suppressed, 6 warnings not shown）
- pytest：1835 passed, 12 skipped, 10 deselected in 13.61s
- coverage：95.23% ≥ 95%

## 遗留事项

- 无

## 下一轮计划

- 待用户分配新需求
