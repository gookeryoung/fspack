# iter-153: dep_analyzer.py 子包化（pe/elf/macho 三解析器）

## 需求清单
- [x] dep_analyzer.py（487 行）子包化：`dep_analyzer/` 目录下 pe/elf/macho/common/__init__ 五文件
- [x] 保持 facade：公共 API（DepGraph/BinaryInfo/analyze_*/find_*/strip_*）不变
- [x] 兼容 patch：`dep_analyzer.subprocess.run` / `dep_analyzer._parse_pe_imports`
- [x] 全量 552 测试通过

## 迭代目标
1. 将 487 行的单文件分析器拆分为"公共模型 + 三平台解析器"结构
2. 消除 PE/ELF/Mach-O 三解析器的混合耦合，新增平台只需加一个子模块
3. 保持 facade 导入路径与 patch 点 100% 兼容

## 改动文件清单

### 删除
- `packaging/dep_analyzer.py`（旧 487 行单文件）

### 新增子包 `packaging/dep_analyzer/`
1. `common.py`（~180 行）
   - 常量：`_PARALLEL_THRESHOLD` / `_MAX_WORKERS` / `_BINARY_EXTS` / `_ENTRY_EXTS` / `_SYSTEM_PREFIXES`
   - 数据类：`BinaryInfo`（frozen，path+deps+name_lower）、`DepGraph`（binaries+entries+unresolved）
   - 扫描/入口辅助：`_iter_binary_files`（rglob + 排序）、`_identify_entries` / `_collect_loader_entries`
   - 依赖解析辅助：`_dep_basename`、`_is_system_dep`、`_detect_platform_from_path`
   - 并行：`_parse_deps_parallel(parse_fn, paths, target)`（ThreadPoolExecutor 包装）

2. `pe.py`（~130 行）—— 纯 Python PE 导入表解析
   - `_parse_pe_imports(path)`：DOS header → PE signature → COFF → Optional → DataDirectory[1] → Section table → IMAGE_IMPORT_DESCRIPTOR 遍历
   - `_read_ascii_string(data, offset)`：NUL 终止 ASCII 字符串读取
   - 不依赖外部库（pefile 未引入）

3. `elf.py`（~60 行）—— ELF（objdump -p NEEDED）
   - `_parse_objdump_deps(path)`：`objdump -p` 输出中 `NEEDED ` 行提取
   - `_D(attr_name, fallback)` dispatch：从 facade 取 `subprocess` 模块属性（patch 兼容）

4. `macho.py`（~55 行）—— Mach-O（otool -L）
   - `_parse_otool_deps(path)`：`otool -L` 输出中跳过首行（文件自身），取每行首个空格前路径
   - `_D(attr_name, fallback)` dispatch：同上

5. `__init__.py`（~155 行）—— facade + 主 API
   - 显式 `import subprocess`（属性暴露给 patch）
   - 从子模块 re-export：BinaryInfo / DepGraph / _parse_pe_imports / _parse_dependencies / 三大 API
   - `_S(attr_name, fallback)`：从自身取属性（使 `_parse_pe_imports` 走动态 dispatch）
   - `_parse_dependencies(path, target)`：PE/MACOS/ELF 分发，PE 走 `_S` dispatch
   - 主 API：`analyze_binary_dependencies`（扫描→并行解析→入口识别→未解析依赖收集）、`find_unused_binaries`（BFS 可达集合，返回未引用）、`strip_unused_binaries`（unlink + 字节数累计）

## 关键决策与依据

### 决策 1：子包化而非多文件
计划明确写"子包化（pe/elf/macho 三解析器）"，所以用 `dep_analyzer/__init__.py`。代价：删除旧 dep_analyzer.py。收益：新平台解析器加一个文件 + 在 `__init__._parse_dependencies` 加一分支即可，零污染公共 API。

### 决策 2：elf/macho 通过 `_D` 取 `subprocess`，pe 通过 `_S` 取 `_parse_pe_imports`
关键 patch 点：
- `dep_analyzer.subprocess.run`：3 处 test_dep_analyzer
- `dep_analyzer._parse_pe_imports`：4 处 test_dep_analyzer

分别在子模块中延迟解析 facade 属性，确保测试 setattr 后的值被感知。

## 代码实现情况
- 拆分后规模：common 180 / pe 130 / elf 60 / macho 55 / __init__ 155，全部 < 200 行
- 删除旧 `dep_analyzer.py`（487 行），单文件减 67%，每子模块职责单一

## 测试验证结果
```
test_dep_analyzer 专项：74 passed
全量回归：552 passed, 11 skipped in 5.83s

性能基线：
  warm small/medium  4.39ms / 4.87ms（baseline 一致）
  cold small/medium  5.67ms / 8.88ms（baseline 一致）
```

## 遗留事项
- 后续若引入 `pefile` / `pyelftools` 可直接替换对应子模块实现，公共 API 零改动
- 目前公共 `_parse_dependencies` 只对 PE 走 dispatch，macOS/ELF 暂无直接 patch 需求。未来若有需要，同样加 `_S` 即可

## 下一轮计划
1. iter-154：cli_parser.py 按子命令拆分（build/init/doctor/manifest）
2. 保持 cli_parser facade + 主入口 dispatch
3. 跑全量测试验证
