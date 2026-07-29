# iter-107：启动时间优化（req-47 iter-102）

## 需求清单

延续 req-47 阶段 4 路线，实施 iter-102 启动时间优化，通过 entry wrapper 注入
import 系统钩子降低运行时启动时间。

- [x] entry wrapper 注入 `sys.path_importer_cache` 预填充，优先匹配 site-packages
- [x] 重量级模块延迟导入钩子：`--lazy-import numpy,pandas` 首次 import 时不执行
  模块 `__init__.py`，首次属性访问时才加载
- [x] `.pth` 文件优化：`no_site=True` 时剥离 site-packages 下所有 `.pth` 文件
- [x] 测量启动时间基线（wrapper 生成耗时基线）

## 迭代目标

1. **启动时间**：通过 `LazyLoader` 延迟重量级模块导入，典型收益 numpy ~80ms、
   pandas ~150ms；`path_importer_cache` 预填充避免 lazy FileFinder 创建开销
2. **配置链路**：`--lazy-import` CLI 参数 + `[tool.fspack] lazy_imports` 配置项，
   CLI 完全覆盖配置默认（与 extras 语义一致）
3. **.pth 剥离**：`no_site=True` 时 site.py 不加载，.pth 文件不会被处理，剥离
   避免占空间与误导
4. **不退化**：所有现有测试通过；基线测试不退化

## 改动文件清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `src/fspack/packaging/entry.py` | 增强 | wrapper 模板注入 `path_importer_cache` 预填充 + `_LazyImportFinder` meta path finder；`generate_wrapper_source` 新增 `lazy_imports` 参数 |
| `src/fspack/config/models.py` | 增强 | `BuildOptions`/`BuildDefaults` 新增 `lazy_imports` 字段；`build_options_from_defaults` 透传 |
| `src/fspack/config/parsing.py` | 增强 | `_parse_build_defaults` 解析 `[tool.fspack] lazy_imports` 字符串列表 |
| `src/fspack/cli.py` | 增强 | `_add_build_subparser` 新增 `--lazy-import` 参数；`_run_build` 合并到 `BuildOptions`；新增 `_parse_lazy_imports` 辅助函数 |
| `src/fspack/packaging/pipeline_stages.py` | 增强 | `_build_entry_loaders` 传递 `lazy_imports` 到 `generate_wrapper_source` |
| `src/fspack/packaging/pipeline.py` | 增强 | `no_site=True` 时剥离 site-packages 下 `.pth` 文件 |
| `tests/test_entry.py` | 测试 | 5 个新测试：path_importer_cache 预填充、lazy_imports 默认/启用/单模块/空元组 |
| `tests/test_cli.py` | 测试 | 9 个新测试：`_parse_lazy_imports` 7 个 + dispatch 2 个 |
| `tests/test_config.py` | 测试 | 3 个新测试：lazy_imports 配置解析/默认空/类型校验 |
| `tests/test_perf_baseline.py` | 测试 | 1 个新基线：`test_generate_wrapper_source_baseline` |

## 关键决策与依据

### 决策 1：用 `importlib.util.LazyLoader` 实现延迟导入

依据：`LazyLoader` 是 Python 标准库（3.5+）提供的延迟加载机制，包装 `SourceFileLoader`
后首次属性访问时才执行模块 `__init__.py`。无需新依赖，与 `sys.meta_path` finder
机制无缝集成。C 扩展模块（.pyd/.so）无法延迟（C 初始化必须即时执行），finder
返回 `None` 让默认 finder 处理。

### 决策 2：`_LazyImportFinder` 仅拦截顶层模块名

依据：`import numpy.array` 会先触发 `import numpy`，lazy finder 拦截 `numpy`
后返回 lazy spec，`numpy.array` 通过 lazy 顶层的属性访问触发真正加载。子模块
（`name != top`）直接返回 `None` 让默认 finder 处理，避免子模块加载时序复杂。

### 决策 3：`path_importer_cache` 预填充而非自定义 `path_hook`

依据：`sys.path_importer_cache` 是 Python import 机制的缓存层。预创建 `FileFinder`
注入缓存使首次 import 直接命中，跳过 `path_hooks` 迭代。比自定义 `path_hook` 更
简单且无副作用——`FileFinder` 是 Python 默认使用的 finder 类型，预创建等效于
让 Python 提前完成 lazy 初始化。

### 决策 4：`.pth` 剥离仅在 `no_site=True` 时执行

依据：`no_site=True` 时 `_pth` 文件省略 `import site`，site.py 不加载，.pth 文件
不会被处理。保留它们仅占空间（pywin32_postinstall.pth、distutils-precedence.pth 等）。
`no_site=False` 时 site.py 启动时处理 .pth 文件（如设置 sys.path），必须保留。

### 决策 5：CLI `--lazy-import` 完全覆盖配置默认（非合并）

依据：与 `--extra` 语义一致。CLI 指定时完全替换 `[tool.fspack] lazy_imports`
配置默认值，避免 CLI 与配置合并的复杂性。空字符串 `--lazy-import ''` 显式清除。

## 代码实现情况

### 已完成

1. **entry.py wrapper 模板**：
   - `path_importer_cache` 预填充：site-packages 存在时预创建 `FileFinder` 注入缓存
   - `_LazyImportFinder`：`lazy_imports` 非空时注册到 `sys.meta_path` 前端，拦截
     顶层模块用 `LazyLoader` 包装 `SourceFileLoader`
2. **config 链路**：`BuildOptions.lazy_imports`/`BuildDefaults.lazy_imports` 字段；
   `parsing.py` 解析 `[tool.fspack] lazy_imports` 字符串列表
3. **CLI**：`--lazy-import MODULES` 参数（逗号分隔），`_parse_lazy_imports` 解析为
   去空白去重元组
4. **pipeline_stages.py**：`_build_entry_loaders` 传递 `ctx.opts.lazy_imports`
5. **pipeline.py**：`no_site=True` 时 `site_packages.glob("*.pth")` 删除所有 .pth 文件

## 测试验证结果

- **ruff check/format**：全部通过
- **pyrefly check**：0 错误
- **pytest（非 slow）**：1745 passed, 12 skipped, 8 deselected
- **pytest 基线**：8 个基线测试全部通过（含新增 `test_generate_wrapper_source_baseline`）
- **coverage**：96.52%（≥ 95% 要求）

## 遗留事项

- 实际启动时间收益需在真实打包项目（含 numpy/pandas）中端到端测量验证
- `LazyLoader` 对 `from numpy import *` 场景可能不兼容（star import 触发立即加载）
- `.pth` 剥离的覆盖率未达 100%（pipeline.py 262-263 行未覆盖），后续可补集成测试

## 下一轮计划

- iter-108：可考虑 iter-103 安全加固（依赖哈希校验 + SBOM + 签名，req-47 阶段 4）
