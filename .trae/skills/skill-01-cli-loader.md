# skill-01 CLI 与 C Loader 基础架构

## 核心决策

### 模块结构
`src/fspack/` 下按职责分模块：cli(子命令分发) → commands/(build/clean/run/package) → project(解析) → analyzer(AST) → embed/standalone(运行时) → loader(C loader) → builder(编排)。

### C loader 设计
- 不依赖 Python.h（embed 包不含开发头文件），用动态加载 `python3X.dll` → `Py_Main(argc, argv)`。
- 配置在生成时 `#define` 烧入：PYTHON_HOME、PYTHON_DLL。
- GUI 子系统：mingw `-mwindows`；CLI：console。
- 版本无关：优先 `python3.dll`（稳定 ABI），回退 `python3X.dll`。
- 后续演进（iter-10）：入口路径改为运行时从 `dist/.entry` 文件读取，不再硬编码。

### 依赖下载策略
- dev python 执行 `pip download --platform <tag> --only-binary=:all:` 拉 wheel，再用 zipfile 解包到 site-packages。
- 后续演进：wheel 缓存到 `~/.fspack/cache/wheels/`，`pip download -d <cache> --find-links <cache>` 自动跳过已存在 wheel。

### Python 3.8 兼容
- `from __future__ import annotations` 延迟注解求值。
- `sys.version_info >= (3, 11)` 用 `tomllib`，否则 `tomli`（条件依赖）。
- `sys.stdlib_module_names`（3.10+）不可用，用 curated frozenset 回退。

## 验证基线
门禁：ruff check + ruff format --check + pyrefly check + pytest --cov≥95%。网络/编译/运行类测试标 `@pytest.mark.slow`。
