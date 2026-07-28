# iter-101: DLL/so 传递依赖分析

## 需求清单

- [x] 实现 `dep_analyzer.py` 跨平台二进制依赖解析模块
- [x] Windows 纯 Python 解析 PE 导入表（无 pefile 依赖）
- [x] Linux 用 objdump、macOS 用 otool 解析依赖
- [x] 构建依赖图，BFS 识别无引用二进制并剥离
- [x] 扩展配置与 CLI，新增 `--analyze-deps` 选项
- [x] 集成到构建流程（pipeline_stages + pipeline）
- [x] 添加 dep_analyzer 单元测试（74 个，覆盖率 98.34%）
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

对应 req-47 阶段 4 第 1 轮：DLL/so 传递依赖分析。为 `fsp b` 新增可选的
`--analyze-deps` 选项，扫描 dist 下所有 `.dll`/`.so`/`.dylib`/`.pyd`，构建
依赖图，从入口（loader + .pyd/.so + runtime python）BFS 找出不可达的二进制
并剥离，典型项目体积减少 5-15%。

## 改动文件清单

### 新增

- `src/fspack/packaging/dep_analyzer.py`
  - `BinaryInfo`：单个二进制文件信息（path + deps + name_lower 属性）
  - `DepGraph`：依赖图数据类（binaries/entries/unresolved）
  - `analyze_binary_dependencies(dist_dir, target, *, runtime_dir)`：扫描 dist
    下所有二进制，构建依赖图
  - `find_unused_binaries(graph)`：从入口 BFS 可达集合，返回不可达二进制路径
  - `strip_unused_binaries(unused)`：删除未引用二进制，返回节省字节数
  - `_parse_pe_imports(path)`：纯 Python 解析 PE 导入表（DOS header → PE header
    → DataDirectory[1] → IMAGE_IMPORT_DESCRIPTOR 数组）
  - `_parse_objdump_deps(path)`：用 `objdump -p` 解析 ELF NEEDED 条目
  - `_parse_otool_deps(path)`：用 `otool -L` 解析 Mach-O dylib 依赖
  - `_dep_basename`/`_is_system_dep`/`_detect_platform_from_path`：辅助函数
  - `_iter_binary_files`/`_identify_entries`/`_collect_loader_entries`：文件
    扫描与入口识别
- `tests/test_dep_analyzer.py`
  - 74 个测试覆盖 PE 解析（含 PE32+ 与各种错误分支）、objdump/otool 输出解析、
    依赖图构建、BFS 可达性、剥离、入口识别、阶段函数集成、端到端 Windows 流程
  - `_make_minimal_pe(deps)`：构造包含指定导入 DLL 名的最小 PE32 文件

### 修改

- `src/fspack/config/models.py`
  - `BuildDefaults` 新增 `analyze_deps: bool | None = None`
  - `BuildOptions` 新增 `analyze_deps: bool = False`（默认关闭，分析耗时）
- `src/fspack/config/parsing.py`
  - `_BUILD_DEFAULT_KEYS` 新增 `"analyze_deps": "analyze_deps"` 映射
  - `_parse_build_defaults` docstring 更新含 `analyze_deps`
- `src/fspack/cli.py`
  - `_add_build_subparser` 新增 `--analyze-deps` 选项（action="store_true"）
  - `_run_build` 合并配置：`analyze_deps=ns.analyze_deps or base.analyze_deps`
- `src/fspack/packaging/pipeline_stages.py`
  - 新增 `_analyze_binary_dependencies(ctx)` 阶段函数：调用 dep_analyzer 三步
    流程（analyze → find_unused → strip），节省字节数写入 tracker 的"依赖分析"
    stage
  - `__all__` 新增 `_analyze_binary_dependencies`
- `src/fspack/packaging/pipeline.py`
  - `_execute_build` 在 `_build_entry_loaders` 之后、体积报告之前调用
    `_analyze_binary_dependencies(ctx)`（仅当 `opts.analyze_deps` 启用时）

## 关键决策与依据

1. **Windows 纯 Python PE 解析**：避免引入 `pefile` 依赖。读 DOS header →
   PE header → DataDirectory[1] Import Table → IMAGE_IMPORT_DESCRIPTOR 数组，
   每个 descriptor 的 Name 字段是 RVA，经 section table 转为文件偏移读取 ASCII
   DLL 名。支持 PE32 (0x10b) 与 PE32+ (0x20b)。

2. **Linux/macOS 用系统工具**：`objdump -p`（binutils 自带）与 `otool -L`
   （Xcode 自带）静态解析，支持交叉构建。工具缺失时返回 None 跳过该平台
   分析，不阻断主流程。

3. **入口定义**：loader 可执行文件 + 所有 `.pyd`/`.so`（Python import 加载，
   不在 PE/ELF 依赖图中）+ runtime python 解释器。确保运行时必要的二进制
   被正确识别为可达。

4. **保守剥离策略**：仅剥离同级或下级目录的未引用 DLL；系统库
   （`/usr/lib`/`/System/Library`/`KERNEL32` 等）不参与剥离判定；`--analyze-deps`
   默认关闭，仅 CLI 显式启用时执行。

5. **不可达防御分支加 `# pragma: no cover`**：PE 解析中 6 处 `except struct.error`
   因前面的长度检查永远不会触发；`find_unused_binaries` 中 `info is None` 因
   current 来自 binaries 键集合不可能为 None；`_read_ascii_string` 的 except
   因 `errors="ignore"` 不会抛异常。均标记 pragma 提升覆盖率真实性。

## 代码实现情况

- `dep_analyzer.py` 256 行（含 docstring），公共 API 完整类型注解与中文 docstring
- 测试 74 个，覆盖 PE32/PE32+ 解析、单/多 DLL 导入、各种截断/损坏文件、objdump
  /otool 输出解析（含空行/工具缺失/超时/非零返回码）、依赖图构建、BFS 可达性
  （含循环/重复入口/大小写不敏感）、剥离（含字节统计/文件缺失）、入口识别
  （Windows/Linux/macOS）、阶段函数集成、端到端 Windows 流程
- 集成点：`pipeline._execute_build` 在 loader 生成后调用，`pipeline_stages.
  _analyze_binary_dependencies` 封装三步流程

## 整合优化情况

- 修复 ruff PLW2901：`_parse_objdump_deps`/`_parse_otool_deps` 中循环变量
  `line` 被重新赋值，改为 `raw_line` + `stripped`
- 修复 pyrefly 类型错误：测试桩 `_make_completed` 从动态属性赋值改为
  `@dataclass class _SubprocessResult`
- 修复测试 `__import__` 写法：改为显式 `from fspack.config import AppType`

## 测试验证结果

- ruff check: All checks passed!
- ruff format --check: 104 files already formatted
- pyrefly check: 0 errors（7 suppressed, 7 warnings not shown）
- pytest: 1569 passed, 1 skipped, 32 deselected
- 覆盖率: 97.61% ≥ 95%（dep_analyzer 单模块 98.34%）

## 遗留事项

- macOS/Linux 实际场景验证待后续迭代（需真实 dist 目录）
- 依赖分析缓存（避免重复扫描）待评估必要性
- rpath/@loader_path 解析目前仅取 basename，未按 rpath 完整解析

## 下一轮计划

iter-102: req-47 阶段 4 第 2 轮，启动速度优化（site.py 跳过 / .pyc 优化级别
评估 / 冷启动基线测量）。
