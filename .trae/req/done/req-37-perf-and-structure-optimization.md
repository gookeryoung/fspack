# 性能与项目结构优化（10 项迭代）

## 背景

经过多轮功能迭代，fspack 已具备完整的打包能力（精简规则、Nuitka 编译、
Linux 发行包、Win7 兼容等）。当前需转向性能基线建立、热点优化与项目
结构整洁化，提升大项目打包速度与代码可维护性。

### 现状基线（2026-07-27）

**模块行数 Top 9（>400 行）**：

| 模块 | 行数 | 主要职责 |
|------|------|---------|
| `packaging/nuitka.py` | 1332 | 环境就绪 + 编译 + 验证 + stamp 缓存 |
| `builder.py` | 901 | 阶段编排 + pyc 预编译 + 源码同步 |
| `config.py` | 731 | dataclass + toml 解析 + 版本解析 |
| `packaging/installer.py` | 619 | NSIS + Linux 安装包 + zip |
| `packaging/wheels.py` | 609 | pip/uv 调用 + marker 过滤 + 缓存 |
| `packaging/loader.py` | 584 | C loader 源码 + 交叉编译 |
| `slim/base.py` | 526 | SlimSpec 基类 + 解压实现 |
| `slim/qt.py` | 443 | Qt 库精简规则 |
| `analyzer.py` | 400 | AST 依赖分析 |

**性能热点（凭代码审查推断，需基线测量确认）**：

1. `analyze_dependencies`：单线程 `for py in src_dir.rglob("*.py")` 顺序
   `ast.parse`，大项目（100+ 文件）耗时显著
2. `slim_unpack`：顺序解压多 wheel，PySide6 拆分 wheel（3 个）+ 大 wheel
   （2000+ 文件）场景耗时
3. `source_fingerprint`：每次遍历所有 .py 计算 mtime/size，os.walk 有 stat
   开销
4. `_precompile_pyc`：对 src 与 site-packages 分别调 `compileall` 子进程，
   subprocess 启动开销 ~50ms × N

## 10 项迭代任务

### 性能优化（iter-51 ~ iter-55）

- [x] **iter-51 性能基线建立**：添加 `pytest-benchmark` 依赖与基线测试套件，
  覆盖 AST 解析、wheel 解压、指纹计算、subprocess 启动等核心场景。后续优化
  的回归门禁（性能退化 > 10% 失败）
- [x] **iter-52 AST 依赖分析并行化**：`analyze_dependencies` 改
  `ProcessPoolExecutor`（CPU 密集 ast.parse），大项目（100+ 文件）显著提速
- [x] **iter-53 wheel 并行解压**：`slim_unpack` 改 `ThreadPoolExecutor`
  （I/O 密集 zipfile 解压），PySide6 多 wheel 场景显著提速
- [x] **iter-54 source_fingerprint 优化**：`os.walk` → `os.scandir` 递归，
  利用 `DirEntry.stat(follow_symlinks=False)` 减少 stat 调用
- [x] **iter-55 _precompile_pyc 批量化**：合并 src 与 site-packages 的
  compileall 调用为单次 subprocess，减少启动开销

### 项目结构优化（iter-56 ~ iter-60）

- [x] **iter-56 nuitka.py 拆分**：1332 行 → `nuitka_env.py`（环境就绪）/
  `nuitka_compile.py`（编译流程）/ `nuitka_verify.py`（验证与导入测试），
  `nuitka.py` 作 facade
- [x] **iter-57 builder.py 拆分**：901 行 → `pipeline.py`（阶段函数）/
  `pyc.py`（pyc 预编译）/ `sync.py`（源码同步），`builder.py` 作 facade
- [x] **iter-58 config.py 拆分**：731 行 → `models.py`（dataclass）/
  `parsing.py`（toml 解析）/ `versions.py`（版本解析），`config.py` 作 facade
- [x] **iter-59 wheels.py 拆分**：609 行 → `wheel_pip.py`（pip 调用）/
  `wheel_cache.py`（依赖缓存）/ `wheel_markers.py`（marker 过滤），
  `wheels.py` 作 facade
- [x] **iter-60 slim/base.py 拆分**：526 行 → `spec.py`（SlimSpec 基类与
  辅助）/ `unpack.py`（slim_unpack/_slim_extract），`base.py` 作 facade

## 验收标准

- 性能基线建立后，每次性能优化迭代须对比基线，退化 > 10% 失败
- 项目结构拆分保持公开 API 不变（`__all__` 与 import 路径兼容），所有现有
  测试不破坏
- 每次迭代全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
- 每次迭代覆盖率不下降

## 实施结果

全部 10 项迭代完成：

- 性能优化 5 项（iter-51~55）：建立基线 + AST 并行 + wheel 并行解压 +
  scandir 优化 + pyc 批量编译
- 结构优化 5 项（iter-56~60）：5 个大文件（1546/1059/887/709/526 行）拆分为
  15 个职责单一的模块 + 5 个 facade

最终门禁状态：
- ruff check：通过
- ruff format --check：通过
- pyrefly check：0 errors
- pytest：1010 passed
- coverage：97.16%（≥95% 门禁）
