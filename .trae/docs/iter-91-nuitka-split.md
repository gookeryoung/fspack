# iter-91: nuitka_compile.py 与 nuitka_env.py 职责拆分

## 需求清单

- [x] 拆分 `nuitka_compile.py`：将产物剥离与构建目录清理职责拆到独立模块
- [x] 拆分 `nuitka_env.py`：将 standalone python 准备与 ccache 管理职责拆到独立模块
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

将两个过大的 nuitka mixin 模块按职责拆分为多个单一职责 mixin，降低单文件复杂度便于
维护与测试，同时保持 `NuitkaCompiler` facade 公开 API 完全不变。

## 改动文件清单

### 新增

- `src/fspack/packaging/nuitka_strip.py` — 产物剥离与构建目录清理 mixin
  - 从 `nuitka_compile.py` 迁入 `_strip_compiled_sources` / `_cleanup_build_dirs`
- `src/fspack/packaging/nuitka_standalone.py` — standalone python 准备 mixin
  - 从 `nuitka_env.py` 迁入 `_build_python_cache_dir` / `_build_python_exe` /
    `_ensure_build_python` / `_download_standalone_python` / `_extract_standalone_python`
- `src/fspack/packaging/nuitka_ccache.py` — ccache 管理 mixin
  - 从 `nuitka_env.py` 迁入 `_ensure_ccache` / `_download_and_extract_ccache`
    及模块级常量 `CCACHE_VERSION` / `_CCACHE_BASE` / `CCACHE_URLS`

### 修改

- `src/fspack/packaging/nuitka_compile.py`
  - 删除已迁移到 `nuitka_strip.py` 的方法
  - 类顶部补充 `_strip_compiled_sources` / `_cleanup_build_dirs` stub（仅供类型检查，
    运行时由 MRO 派发到 `NuitkaStrip`）
  - 更新 stub 文档说明来源 mixin（`_ensure_build_python` → `NuitkaStandalone`，
    `_ensure_ccache` → `NuitkaCcache`）
- `src/fspack/packaging/nuitka_env.py`
  - 删除已迁移到 `nuitka_standalone.py` / `nuitka_ccache.py` 的方法与常量
  - 移除 `_ensure_build_python` / `_ensure_ccache` stub（避免 MRO 顺序冲突覆盖真实实现）
  - 删除不再使用的导入（`contextlib` / `shutil` / `tarfile` / `zipfile` /
    `KNOWN_STANDALONE_VERSIONS`）
- `src/fspack/packaging/nuitka.py` — facade 类
  - 多继承列表从 3 个 mixin 扩展到 6 个：
    `NuitkaEnv, NuitkaStandalone, NuitkaCcache, NuitkaStrip, NuitkaCompile, NuitkaVerify`
  - 更新模块 docstring 与类 docstring 描述六个 mixin 职责
  - 显式 import 新增的三个 mixin 模块（保持 monkeypatch 路径解析兼容）
- `tests/test_nuitka.py`
  - `test_ensure_ccache_unsupported_platform_returns_none`：
    `monkeypatch.setattr("fspack.packaging.nuitka_env.CCACHE_URLS", {})` →
    `fspack.packaging.nuitka_ccache.CCACHE_URLS`
- `tests/test_offline_mode.py`
  - `test_nuitka_ensure_ccache_offline_skips_download`：
    `monkeypatch.setattr("fspack.packaging.nuitka_env.shutil.which", ...)` →
    `fspack.packaging.nuitka_ccache.shutil.which`

## 关键决策与依据

### MRO 顺序设计

`NuitkaCompiler(NuitkaEnv, NuitkaStandalone, NuitkaCcache, NuitkaStrip, NuitkaCompile, NuitkaVerify)`

关键约束：**真实实现 mixin 必须在 stub mixin 前面**，否则 stub（`raise NotImplementedError`）
会覆盖真实实现。

- `NuitkaStrip` 必须在 `NuitkaCompile` 前面：`NuitkaCompile` 类内有 `_strip_compiled_sources` /
  `_cleanup_build_dirs` stub 供类型检查，运行时由 `NuitkaStrip` 真实实现优先派发
- `NuitkaStandalone` / `NuitkaCcache` 必须在 `NuitkaCompile` 前面：`NuitkaCompile` 类内有
  `_ensure_build_python` / `_ensure_ccache` stub
- `NuitkaEnv` 不再提供 `_ensure_build_python` / `_ensure_ccache` stub（删除），避免与
  `NuitkaStandalone` / `NuitkaCcache` 真实实现冲突

### NuitkaVerify 仍无法 stub

`NuitkaVerify` 在 MRO 末尾（`NuitkaCompile` 后），`NuitkaStrip` 调用 `_verify_compiled_modules`
时若在 `NuitkaStrip` 类内放 stub 会覆盖 `NuitkaVerify` 真实实现。沿用原 `NuitkaCompile`
方案：调用处用 `# type: ignore[attr-defined]` 标注，不在 `NuitkaStrip` 内放 stub。

### 拆分边界

按"职责单一 + 边界清晰"原则拆分：

- `nuitka_strip.py`：编译后处理（产物剥离 + 构建目录清理），与编译流程解耦
- `nuitka_standalone.py`：获取编译用 Python 解释器（Windows python-build-standalone 下载）
- `nuitka_ccache.py`：获取 C 编译缓存工具（PATH 查找 + 下载）
- `nuitka_env.py`：环境就绪主流程（C 编译器检查 + nuitka 安装 + pip 可用性 + 编译环境变量）
- `nuitka_compile.py`：编译流程核心（单文件编译 + stamp 缓存 + 第三方包编译）

## 代码实现情况

### 模块规模变化

| 模块 | 拆分前 stmt | 拆分后 stmt | 变化 |
|------|------------|------------|------|
| nuitka_compile.py | ~410 | 231 | -44% |
| nuitka_env.py | ~360 | 108 | -70% |
| nuitka_strip.py | - | 52 | 新增 |
| nuitka_standalone.py | - | 78 | 新增 |
| nuitka_ccache.py | - | 86 | 新增 |

### 公开 API 不变

`NuitkaCompiler` facade 通过 MRO 派发，所有 `_ensure_build_python` / `_ensure_ccache` /
`_strip_compiled_sources` / `_cleanup_build_dirs` / `_download_standalone_python` /
`_extract_standalone_python` / `_download_and_extract_ccache` 等方法访问路径与签名
完全不变，测试仅修改两处 monkeypatch 模块路径。

## 整合优化情况

- 删除 `nuitka_env.py` 不再使用的导入（`contextlib` / `shutil` / `tarfile` / `zipfile`）
- 删除 `nuitka_env.py` 的 `_ensure_build_python` / `_ensure_ccache` stub（MRO 冲突源）
- 拆分后每个 mixin 模块职责单一，便于独立测试与复用

## 测试验证结果

- `uv run ruff check src tests` — All checks passed
- `uv run ruff format --check src tests` — 93 files already formatted
- `uv run pyrefly check` — 0 errors (5 suppressed, 7 warnings)
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95` —
  1418 passed, 1 skipped, 30 deselected, coverage 97.83%

新模块覆盖率：

| 模块 | 覆盖率 |
|------|--------|
| nuitka_compile.py | 100% |
| nuitka_env.py | 100% |
| nuitka_strip.py | 100% |
| nuitka_standalone.py | 98% |
| nuitka_ccache.py | 97% |

## 遗留事项

无。

## 下一轮计划

iter-92: 继续按 `req-47-feature-perf-polish.md` 推进剩余功能/性能完善项。
