# 迭代 27：拆分 builder 到 packaging/wheels.py

## 迭代目标

builder.py 880 行职责混杂，按职责拆分：把 wheel 下载相关代码（~400 行）
迁到 `packaging/wheels.py`，builder.py 只留 `build()` 主流程编排。

经分析，builder 不适合基类抽象（build 流程只有一个实现，无多态需求；
运行时下载 vs wheel 下载差异大，强提"通用下载器基类"会空洞），只做模块拆分。

## 需求清单

- [x] 创建 packaging/wheels.py，迁入 download_wheels 及辅助函数
- [x] 精简 builder.py（880→284 行），保留 build() 编排逻辑
- [x] 更新 packaging/__init__.py 导出 download_wheels
- [x] 拆分 test_builder.py：wheel 相关测试迁到 test_wheels.py（86 个测试）
- [x] 修改 monkeypatch 路径 fspack.builder.* → fspack.packaging.wheels.*
- [x] 门禁通过（538 passed，覆盖率 97.93%）

## 改动文件清单

### 新增

- `src/fspack/packaging/wheels.py`（445 行）：wheel 下载与依赖解析。
  - 常量：`_PIP_PYTHON_NAMES`/`_UV_RESOLVED_LINE_RE`/`_MISSING_PKG_RE`/
    `_MARKER_PY_VER_RE`/`_PIP_WHEEL_LINE_RE`
  - 公共函数：`download_wheels`
  - 私有函数：`_find_pip_python`/`_find_uv`/`_resolve_with_uv`/
    `_download_online`/`_filter_by_python_version`/`_eval_python_version_marker`/
    `_eval_single_marker`/`_parse_missing_packages`/`_build_sdist_wheels`/
    `_deps_cache_key`/`_load_deps_cache`/`_save_deps_cache`/`_stream_subprocess`/
    `_run_pip`/`_parse_pip_download_wheels`
  - `__all__` 仅导出 `download_wheels`；不依赖 fspack.builder，无循环导入
- `tests/test_wheels.py`（86 个测试）：wheel 下载相关测试 + 辅助类
  （`_Completed`/`_FakePipe`/`_FakePopen`/`_patch_os_read_for`/`_patch_stderr_buffer`）

### 修改

- `src/fspack/builder.py`（880→284 行）：
  - 删除迁走的函数与常量
  - 新增 `from fspack.packaging.wheels import download_wheels`（build() 调用）
  - `__all__` 保留 `download_wheels`（re-export 供测试 monkeypatch
    `fspack.builder.download_wheels` 路径）
  - 清理 8 个不再使用的 import
- `src/fspack/packaging/__init__.py`：新增导出 `download_wheels`
- `tests/test_builder.py`：删除 86 个迁走测试，保留 18 个 build/copy_source/
  unpack_wheels/site_packages_has_deps 测试；清理 import

## 关键决策与依据

### 1. 不引入基类抽象，只做模块拆分

builder.py 的 `build()` 是线性编排，全项目只有一个实现，无多态需求。
模板方法模式需要"一骨架 + 多子类变体"（如 slim 的 SlimSpec + 多 Spec 子类），
builder 没有这种场景。"运行时下载" vs "wheel 下载"差异巨大（单文件 URL 已知
urllib 下载 vs 多包 pip/uv 依赖树解析 subprocess 调用），强提"通用下载器基类"
会空洞且子类臃肿，违反 rule-01「三处相似才提取，不过早抽象」。

### 2. wheels.py 不依赖 builder，可在 __init__ 导出

wheels.py 只依赖 exceptions/progress/wheel_cache，不依赖 builder，无循环导入。
与 installer（依赖 builder，不能在 __init__ 导出）不同，wheels 可安全导出。

### 3. builder.py 保留 download_wheels re-export

`from fspack.packaging.wheels import download_wheels` 后，`download_wheels`
绑定在 builder 模块命名空间。test_builder.py 中 build 编排测试 monkeypatch
`fspack.builder.download_wheels` 拦截 build() 内部调用，该路径仍生效。
这与 packaging 迁移时 `fspack.builder.download_embed` 模式一致。

### 4. mirror/net/wheel_cache 不整合

三者职责完全不同（mirror 是配置查表 36 行、net 是 SSL 工具 34 行、
wheel_cache 是文件名解析 64 行）、体量小、无共性基类可提。强行整合只会
变成"utils 杂物间"，违反单一职责。维持独立。

## 代码实现情况

### packaging/wheels.py 结构

```python
"""Wheel 下载与依赖解析：pip/uv 调用、缓存管理、sdist 回退."""

__all__ = ["download_wheels"]

# 常量
_PIP_PYTHON_NAMES = ...
_UV_RESOLVED_LINE_RE = ...
_MISSING_PKG_RE = ...
_MARKER_PY_VER_RE = ...
_PIP_WHEEL_LINE_RE = ...

# 公共函数
def download_wheels(...): ...

# 私有辅助
def _find_pip_python(): ...
def _find_uv(): ...
def _resolve_with_uv(...): ...
def _download_online(...): ...
def _filter_by_python_version(...): ...
def _eval_python_version_marker(...): ...
def _eval_single_marker(...): ...
def _parse_missing_packages(...): ...
def _build_sdist_wheels(...): ...
def _deps_cache_key(...): ...
def _load_deps_cache(...): ...
def _save_deps_cache(...): ...
def _stream_subprocess(...): ...
def _run_pip(...): ...
def _parse_pip_download_wheels(...): ...
```

### builder.py 精简后结构

```python
__all__ = ["DEFAULT_PY_VERSION", "build", "copy_source", "default_icon_path",
           "download_wheels", "unpack_wheels"]

_DEFAULT_ICON = ...
_EXCLUDE = ...

def default_icon_path(): ...
def build(...): ...  # 主流程编排，~170 行
def copy_source(...): ...
def _site_packages_has_deps(...): ...
def unpack_wheels(...): ...  # 委托 slim
```

## 测试验证结果

- ruff check src tests：通过
- ruff format --check src tests：52 文件已格式化
- pyrefly check：0 错误
- pytest -m "not slow" --cov=fspack --cov-fail-under=95：
  - 538 passed, 20 deselected
  - 覆盖率 97.93%
  - packaging/wheels.py 99%（270 stmts, 1 miss）
  - builder.py 93%（icon 处理与 Linux 分支等既有未覆盖路径）

## 整合优化情况

- builder.py 从 880 行降到 284 行，职责单一（仅 build 编排）
- wheel 下载逻辑集中到 packaging/wheels.py，与 runtime/loader/installer 并列
- packaging 包现含 4 个子模块：runtime/loader/installer/wheels
- 测试按模块分离：test_builder（18 编排测试）+ test_wheels（86 下载测试）

## 遗留事项

无。拆分任务闭环。

## 下一轮计划

无。
