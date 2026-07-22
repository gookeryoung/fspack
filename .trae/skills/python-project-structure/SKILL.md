---
name: "python-project-structure"
description: "Python 项目结构与骨架设计技能：src layout、pyproject.toml 元数据、依赖声明（PEP 631/735）、工具链配置拆分、包内部结构、测试/文档/CI 目录组织、项目类型差异（library/cli/gui/web）、版本管理与发布流程。当需要初始化项目、设计目录结构、配置构建系统、拆分工具链、组织测试/文档/CI、选择项目类型骨架、配置版本管理与发布流程时调用。"
---



# Python 项目结构与骨架设计

自包含的 Python 项目骨架设计指南：目录布局、pyproject.toml、依赖声明、工具链拆分、包/测试/文档/CI 组织、项目类型差异、版本与发布。所有示例遵循 `rule-11-python-standards.md`（类型注解、中文 docstring、`from __future__ import annotations`）；优先标准库与 PEP 621/735/660 等规范；构建系统用 hatchling，包管理用 uv，版本管理用 bump-my-version。

## 何时调用

- 需要初始化新 Python 项目或重构现有项目目录结构
- 需要选择 src layout vs flat layout
- 需要设计 pyproject.toml（元数据、依赖、构建系统）
- 需要拆分工具链配置到独立文件（ruff.toml/pytest.ini/.coveragerc/pyrefly.toml/.bumpversion.toml/uv.toml）
- 需要组织包内部结构（`__init__.py` / `py.typed` / `__main__.py` / `__all__`）
- 需要组织 tests/ 目录与 conftest.py 层级
- 需要配置 Sphinx 文档与 ReadTheDocs
- 需要设计 GitHub Actions CI/CD 结构
- 需要为不同项目类型（library/cli/gui/web）选择骨架
- 需要配置版本管理（bump-my-version）与发布流程（PyPI/GitHub Release）

## 项目布局：src layout 首选

src layout（`src/<package>/`）优于 flat layout（`<package>/` 直接在根目录）。

```
my_project/
├── src/
│   └── my_package/
│       ├── __init__.py        # 包入口，定义 __version__ 与 __all__
│       ├── __main__.py        # 可选：支持 python -m my_package
│       ├── py.typed           # PEP 561：标记包对外暴露类型注解
│       └── ...
├── tests/                     # 测试目录（与 src 平级）
├── docs/                      # Sphinx 文档
├── pyproject.toml             # 项目元数据（PEP 621）
├── ruff.toml                  # 工具链配置（独立文件）
├── pytest.ini
├── .coveragerc
├── pyrefly.toml
├── .bumpversion.toml
├── uv.toml
├── .pre-commit-config.yaml
├── .python-version            # pyenv / uv 用的 Python 版本
├── uv.lock                    # 锁定依赖（提交到 VCS）
├── Makefile                   # 常用命令快捷方式
├── README.md
├── LICENSE
└── .gitignore
```

要点：
- **src layout 强制安装后测试**：避免测试误用本地源码而非安装版本（flat layout 常见陷阱：测试时 `import my_package` 指向本地源码而非构建产物，掩盖打包错误）。
- **避免导入冲突**：根目录无 `my_package/`，开发环境不会意外 `import my_package` 指向源码而非安装包。
- **构建系统友好**：wheel 构建从 `src/` 抽取，与最终安装结构一致。
- **flat layout 仅用于零构建脚本**：单文件模块、教学示例可不 src layout；正式发布到 PyPI 的项目一律 src layout。
- `py.typed` 空文件标记 PEP 561，让下游项目使用时获得类型检查支持。

## pyproject.toml：项目元数据中枢

PEP 621 标准化项目元数据；`pyproject.toml` 仅承载元数据 + 构建系统 + 依赖声明，工具链配置拆到独立文件（见下节）。

### 完整骨架

```toml
[project]
name            = "my-package"               # PyPI 包名（连字符）
version         = "0.1.0"                    # 或 dynamic = ["version"] 用 hatch 动态读取
description     = "一句话项目描述。"
readme          = "README.md"
requires-python = ">=3.8"
license         = "MIT"
authors         = [{ name = "作者名", email = "author@example.com" }]
classifiers = [
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.8",
    "Programming Language :: Python :: 3.9",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: Software Development :: Libraries :: Python Modules",
]
dependencies = [
    "copier>=9.0.0",
    # 带环境标记的依赖（PEP 508）
    "PySide2>=5.15.2.1; python_version <= '3.10'",
    "PySide6>=6.5.0; python_version >= '3.11'",
]

[project.optional-dependencies]           # PEP 631：可选依赖分组（extras）
dev  = ["my-package[lint,test]", "prek>=0.4.5"]
docs = ["myst-parser>=3.0", "sphinx-rtd-theme>=2.0", "sphinx>=7.0"]
lint = ["pyrefly>=1.1.1", "ruff>=0.8.0"]
test = ["pytest-cov>=5.0.0", "pytest>=8.0.0"]

[project.scripts]                         # CLI 入口点（仅 cli 项目）
my-package = "my_package.cli:main"

[project.urls]                            # 项目链接（PyPI 页面展示）
Homepage = "https://github.com/user/my-package"
Repository = "https://github.com/user/my-package"
Changelog = "https://github.com/user/my-package/blob/main/CHANGELOG.md"

[build-system]                            # PEP 517：构建后端
build-backend = "hatchling.build"
requires      = ["hatchling"]

[tool.hatch.build.targets.wheel]
packages = ["src/my_package"]             # src layout 包路径

[tool.hatch.build.targets.wheel.force-include]
"src/my_package/py.typed" = "my_package/py.typed"   # 包含 py.typed 标记

[dependency-groups]                       # PEP 735：开发依赖分组（uv 专用）
dev = ["my-package[dev]"]
```

要点：
- **`name` 用连字符**：PyPI 包名约定（`my-package`），但 Python 导入用下划线（`my_package`）；hatchling 自动处理转换。
- **`version` 静态优先**：静态字符串 `"0.1.0"` 简单可靠；动态读取（`dynamic = ["version"]` + `[tool.hatch.version]`）适合多组件同步场景。
- **`classifiers` 完整列出支持的 Python 版本**：PyPI 检索与 pip 安装时校验；用 `Programming Language :: Python :: 3.X` 每个版本一行。
- **`dependencies` 用 PEP 508 标记**：`; python_version <= '3.10'`、`; sys_platform == 'win32'` 实现条件依赖。
- **`optional-dependencies` 自引用**：`"my-package[lint,test]"` 实现分组聚合（PEP 631）。
- **`[project.scripts]` 仅 cli 项目生成**：library/gui/web 不生成入口点。
- **`[build-system]` 用 hatchling**：速度快、配置简单、原生支持 src layout；其他选项（setuptools/flit/poetry）需额外配置。
- **`[dependency-groups]` 是 PEP 735 新规范**：与 `optional-dependencies` 并存，uv 原生支持；`optional-dependencies` 是 PyPI 安装时用（`pip install pkg[dev]`），`dependency-groups` 是开发时用（`uv sync --group dev`）。

### 动态版本（可选）

从 `__init__.py` 的 `__version__` 读取版本号，避免手动同步：

```toml
[project]
dynamic = ["version"]

[tool.hatch.version]
path = "src/my_package/__init__.py"
pattern = '__version__ = "(?P<version>[^"]+)"'
```

要点：
- 适合单一来源场景；coopie 自身用静态 `version` + bump-my-version 同步多文件，更可控。
- 动态版本与 bump-my-version 不兼容（bump 需要静态字符串可改写）。

## 工具链配置拆分

`pyproject.toml` 仅承载元数据；工具链配置拆到独立文件。**好处**：避免 `copier update` 等工具覆盖项目元数据时连带覆盖工具链配置；每个工具职责单一，便于审计。

### 配置文件职责

| 工具 | 配置文件 | 关键配置 | 必备项 |
|------|---------|---------|--------|
| ruff | `ruff.toml` | `line-length=120`、`target-version="py38"`、`[lint] select/ignore` | 顶层键（无 `[tool.ruff]` 前缀） |
| pyrefly | `pyrefly.toml` | `preset="strict"`、`python-version="3.8"`、`project-excludes` | 顶层键 |
| pytest | `pytest.ini` | `testpaths`、`markers`、`asyncio_default_fixture_loop_scope` | `[pytest]` 段 |
| coverage | `.coveragerc` | `branch=true`、`fail_under=95`、`source`、`concurrency=thread` | `[run]`/`[report]` 段 |
| bump-my-version | `.bumpversion.toml` | `current_version`、`[[tool.bumpversion.files]]` 列表 | **保留 `[tool.bumpversion]` 前缀**（强制要求） |
| uv | `uv.toml` | `required-version`、`[[index]]` 镜像源 | 顶层键（覆盖 `pyproject.toml [tool.uv]`） |
| pre-commit | `.pre-commit-config.yaml` | ruff `--fix` + trailing-whitespace + end-of-file-fixer | YAML 格式 |

### ruff.toml 示例

```toml
extend-exclude = ["template", "docs"]
line-length    = 120
target-version = "py38"

[lint]
ignore = [
    "E501",    # line too long (handled by formatter)
    "PLC0415", # import should be at top-level (intentional for lazy imports)
    "PLR0915", # too many statements (intentional for complex methods)
    "PLR2004", # magic value comparison
    "RUF001",  # ambiguous unicode characters in string
    "RUF002",  # ambiguous unicode characters in docstring
    "RUF003",  # ambiguous unicode characters in comment
    "RUF012",  # mutable class attributes (intentional for config)
    "SIM108",  # use ternary operator
    # UP045 建议 Optional[X] -> X | None，但 typer 在 3.8/3.9 用 get_type_hints 求值注解会抛 TypeError
    "UP045",
]
select = [
    "ARG", # flake8-unused-arguments
    "B",   # flake8-bugbear
    "C4",  # flake8-comprehensions
    "E",   # pycodestyle errors
    "F",   # Pyflakes
    "I",   # isort
    "PL",  # Pylint
    "PTH", # flake8-use-pathlib
    "RUF", # Ruff-specific rules
    "SIM", # flake8-simplify
    "UP",  # pyupgrade
    "W",   # pycodestyle warnings
]

[lint.per-file-ignores]
"**/tests/**" = ["ARG001", "ARG002"]   # 测试 fixture 未使用参数不报错
```

### pytest.ini 示例

```ini
[pytest]
addopts = -ra --strict-markers --strict-config
asyncio_default_fixture_loop_scope = function
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
    gui: marks tests requiring Qt/GUI (deselect with '-m "not gui"')
testpaths = tests
```

> 标记注册与 `--strict-markers` 详细说明见 `python-testing` SKILL「标记注册」章节。

### .coveragerc 示例

```ini
[run]
branch = true
concurrency = thread
omit = tests/*
source = my_package

[report]
fail_under = 95
exclude_lines =
    if TYPE_CHECKING:
    if __name__ == .__main__.:
    pragma: no cover
    raise NotImplementedError
show_missing = true
```

> 覆盖率排除规则与 `# pragma: no cover` 详细说明见 `python-testing` SKILL「覆盖率」章节。

### pyrefly.toml 示例

```toml
preset           = "strict"
project-excludes = [".venv/**", "template/**"]
project-includes = ["**/*.ipynb", "**/*.py*"]
python-version   = "3.8"
search-path      = ["."]
```

要点：
- **ruff.toml 顶层键**：无 `[tool.ruff]` 前缀（与 pyproject.toml 内嵌时的区别）。
- **pyrefly 必须 project 模式**：`uv run pyrefly check`（project 模式读取 `project-excludes`），不能用 `pyrefly check .`（per-file 模式忽略配置，template/ 下 Jinja 模板会被误检）。
- **.bumpversion.toml 强制 `[tool.bumpversion]` 前缀**：bump-my-version 解析要求，其他工具的独立配置文件无前缀。
- **uv.toml 覆盖 pyproject.toml `[tool.uv]`**：两个都存在时 uv.toml 优先；coopie 模板用 uv.toml 承载镜像源配置。
- **coverage `concurrency=thread`**：与 pytest-xdist 多进程并行不冲突；多线程代码覆盖率统计必需。
- **`fail_under` 不下调**：覆盖率门禁一旦放宽就难再收紧；新功能未达覆盖率应补测试而非降阈值。

## 包内部结构

### `__init__.py`：包入口

```python
"""my_package - 一句话项目描述."""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
```

要点：
- **`from __future__ import annotations` 必放第一行**（仅次于 docstring）：延迟注解求值，兼容 3.8/3.9 的 `X | Y` 语法。
- **`__all__` 显式声明导出符号**：位置仅次于 `__future__`；禁用 `from x import *`；避免命名空间污染。
- **`__version__` 字符串**：PEP 8 推荐；与 `pyproject.toml` 的 `version` 字段保持一致（bump-my-version 自动同步）。
- **`__init__.py` 不放业务代码**：仅导出公共 API 与版本号；业务逻辑放子模块。
- **包级 docstring**：Sphinx autodoc 自动读取作为包描述。

### `py.typed`：类型标记

空文件，标记 PEP 561。让下游项目 `import my_package` 时获得类型检查支持。

```toml
# pyproject.toml 中确保 py.typed 进入 wheel
[tool.hatch.build.targets.wheel.force-include]
"src/my_package/py.typed" = "my_package/py.typed"
```

要点：
- 文件必须空（或仅注释）；放 `src/<package>/py.typed`。
- 不配置 `force-include` 时 hatchling 默认包含 `py.typed`，但显式声明更可靠。
- 私有/internal 包可不加；发布到 PyPI 的公共包必须加。

### `__main__.py`：模块入口

支持 `python -m my_package` 调用：

```python
"""支持 `python -m my_package` 执行."""

from __future__ import annotations

from my_package.cli import main


if __name__ == "__main__":
    main()
```

要点：
- 仅 cli 项目需要；library/gui/web 不生成。
- `__main__.py` 仅做入口转发，业务逻辑在 `cli.py` 内。
- `if __name__ == "__main__":` 守卫必需，避免被 import 时执行。

### 模块划分

```
src/my_package/
├── __init__.py          # 包入口（__version__、__all__）
├── py.typed             # PEP 561 标记
├── cli.py               # CLI 入口（仅 cli 项目）
├── __main__.py          # python -m 入口（仅 cli 项目）
├── config.py            # 配置加载（Pydantic Settings / dataclass）
├── exceptions.py        # 自定义异常基类与子类
├── models.py            # 数据模型（dataclass / Pydantic BaseModel）
├── services/            # 业务逻辑层
│   ├── __init__.py
│   └── ...
├── utils.py             # ❌ 禁用：职责模糊，见 rule-11
└── helpers.py           # ❌ 禁用：同上
```

要点：
- **禁用 `utils.py`/`helpers.py`**（rule-11）：职责模糊易变成大杂烩；按功能命名（`path_utils` → `paths`、`string_utils` → `strings`）。
- **单一职责**：每模块一个明确职责；超过 500 行考虑拆分。
- **避免循环依赖**：用惰性导入（函数体内 `import` 并注释）打破循环；优先重构层级。
- **`exceptions.py` 集中定义**：自定义异常继承公共基类，按场景分类（rule-11 异常处理）。

## 测试目录组织

### 目录结构

```
tests/
├── __init__.py           # 让 tests 成为包（可选，但便于共享 fixture）
├── conftest.py           # 根级 fixture（所有测试可见）
├── unit/                 # 单元测试（快、隔离）
│   ├── __init__.py
│   ├── conftest.py       # 单元测试专用 fixture
│   └── test_models.py
├── integration/          # 集成测试（慢、跨模块，标 @pytest.mark.slow）
│   ├── __init__.py
│   └── test_workflow.py
├── gui/                  # GUI 测试（仅 GUI 项目，标 @pytest.mark.gui）
│   ├── __init__.py
│   └── test_main_window.py
└── fixtures/             # 测试数据（JSON/CSV/二进制）
    └── sample.json
```

### conftest.py 层级

```python
"""根级 conftest：所有测试共享的 fixture."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """临时项目目录（每个测试独立，自动清理）。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    return tmp_path


@pytest.fixture(scope="session")
def sample_data_path() -> Path:
    """测试数据路径（session 级共享，只读）。"""
    return Path(__file__).parent / "fixtures" / "sample.json"
```

要点：
- **`conftest.py` 自动发现**：pytest 自动加载同目录与父目录的 conftest，无需 import。
- **`tests/__init__.py` 可选**：加上后 tests 成为包，便于跨测试模块 import fixture；不加则 pytest 用 rootdir 模式发现。
- **`fixtures/` 目录放测试数据**：JSON/CSV/二进制文件，通过 `Path(__file__).parent / "fixtures"` 引用。
- **测试覆盖 `src/` 全部公共 API**：覆盖率 ≥ 95%（rule-11 测试要求）。

> fixture 模式、scope 选择、参数化、Mock 策略、GUI 测试（pytest-qt）等详细测试模式见 `python-testing` SKILL。

## 文档目录组织

### Sphinx 标准结构

```
docs/
├── conf.py               # Sphinx 配置
├── index.rst             # 入口页
├── api.rst               # API 自动生成
├── changelog.rst         # 变更日志
├── usage.rst             # 使用指南
├── _static/              # 静态资源（图片/CSS）
│   └── .gitkeep
└── _build/               # 构建输出（.gitignore）
```

### conf.py 关键配置

```python
"""Sphinx 配置."""

from __future__ import annotations

import sys
from pathlib import Path

# 确保 src/ 在 sys.path 中，autodoc 能导入包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

project = "my-package"
author = "作者名"
copyright = "2026, 作者名"

try:
    from my_package import __version__  # type: ignore[import-not-found]
    release = __version__
    version = __version__
except ImportError:
    release = "0.1.0"
    version = "0.1.0"

extensions = [
    "sphinx.ext.autodoc",       # 自动从 docstring 生成 API 文档
    "sphinx.ext.napoleon",      # Google/NumPy docstring 兼容
    "sphinx.ext.viewcode",      # 添加"[源码]"链接
    "sphinx.ext.intersphinx",   # 跨项目链接（如 Python 标准库）
    "myst_parser",              # Markdown 支持
]

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    "member-order": "bysource",
}
autodoc_type_hints = "description"
autodoc_typehints_format = "short"

napoleon_google_docstring = True
napoleon_numpy_docstring = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

language = "zh_CN"
master_doc = "index"
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
```

### ReadTheDocs 配置

```yaml
# .readthedocs.yaml
version: 2

build:
  os: ubuntu-24.04
  tools:
    python: "3.8"

python:
  install:
    - method: pip
      path: .
      extra_requirements:
        - docs

sphinx:
  configuration: docs/conf.py
  builder: html
  fail_on_warning: false
```

### index.rst 骨架

```rst
my-package
==========

一句话项目描述。

.. toctree::
   :maxdepth: 2
   :caption: 目录

   api
   changelog
   usage

简介
====

详细描述。

安装
====

.. code-block:: bash

   pip install my-package

或使用 uv_:

.. code-block:: bash

   uv add my-package

.. _uv: https://docs.astral.sh/uv/
```

要点：
- **`sys.path.insert` 加 src/**：autodoc 导入包必需；不加会 `ImportError`。
- **`autodoc_type_hints = "description"`**：类型注解显示在描述里而非签名里，签名更简洁。
- **`myst_parser` 支持 Markdown**：与 `*.rst` 并存；`source_suffix` 注册两种后缀。
- **`intersphinx_mapping`**：自动链接到 Python 标准库文档（`:func:`print`` → `print` 的文档页）。
- **`fail_on_warning: false`**：初期能容忍警告；正式发布可改为 `true` 提升质量。
- **`language = "zh_CN"`**：中文界面元素（搜索框、导航等）。

## CI/CD 结构

### GitHub Actions 标准 4 jobs

```
.github/
└── workflows/
    ├── ci.yml           # 主 CI：lint + test + render + docs
    └── release.yml      # 发布：GitHub Release + PyPI
```

### ci.yml 骨架

```yaml
name: CI

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main, develop]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint:
    name: Lint & Typecheck
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v5
      - name: Install uv
        uses: astral-sh/setup-uv@v8.3.2
        with:
          enable-cache: true
          cache-dependency-glob: "uv.lock"
      - name: Sync dependencies
        run: uv sync --frozen --extra lint
      - name: Ruff check
        run: uv run ruff check
      - name: Ruff format check
        run: uv run ruff format --check
      - name: Pyrefly check
        run: uv run pyrefly check

  test:
    name: Test
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v5
      - name: Install uv
        uses: astral-sh/setup-uv@v8.3.2
        with:
          enable-cache: true
          cache-dependency-glob: "uv.lock"
      - name: Sync dependencies
        run: uv sync --frozen --extra test
      - name: Run tests
        run: uv run pytest --cov

  docs:
    name: Build Docs
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v5
      - name: Install uv
        uses: astral-sh/setup-uv@v8.3.2
        with:
          enable-cache: true
          cache-dependency-glob: "uv.lock"
      - name: Sync dependencies
        run: uv sync --frozen --extra docs
      - name: Build Sphinx docs
        run: uv run sphinx-build -b html docs docs/_build/html
```

### release.yml 骨架（PyPI OIDC trusted publishing）

```yaml
name: Release

on:
  push:
    tags: ["v*"]

jobs:
  release:
    name: Release
    runs-on: ubuntu-latest
    timeout-minutes: 15
    environment: pypi
    permissions:
      id-token: write        # OIDC trusted publishing 必需
      contents: write        # 创建 GitHub Release
    steps:
      - uses: actions/checkout@v5
      - name: Install uv
        uses: astral-sh/setup-uv@v8.3.2
        with:
          enable-cache: true
      - name: Sync dependencies
        run: uv sync --frozen --extra dev
      - name: Build package
        run: uv build
      - name: Publish to PyPI
        run: uv publish
      - name: Create GitHub Release
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh release create "$GITHUB_REF_NAME" --generate-notes dist/*
```

要点：
- **4 jobs 并行**：lint/test/docs/render 独立并行，`fail-fast: false` 让所有 job 都跑完再判断。
- **`cache-dependency-glob: "uv.lock"`**：依赖文件不变时复用缓存，加速 CI。
- **`uv sync --frozen`**：严格按 lock 文件安装，CI 不更新依赖；本地用 `uv sync`（允许更新）。
- **`--extra lint`/`--extra test`/`--extra docs`**：按 job 安装对应 extras，减少安装体积。
- **`setup-uv` action 必需**：否则 `uv`/`uvx` 命令找不到（exit 127）。
- **PyPI 用 OIDC trusted publishing**：无需配置 API token；在 PyPI 项目设置中绑定 GitHub repo。
- **`permissions: id-token: write`**：OIDC 必需；`contents: write` 创建 Release。
- **`environment: pypi`**：GitHub Environment 保护规则（手动审批、限定分支）。
- **`gh release create --generate-notes`**：从 commit 自动生成 Release notes。
- **`concurrency.cancel-in-progress: false`**（release）：发布不可中断；CI 可中断重复运行。

## 项目类型差异

`copier.yml` 的 `project_type` 字段决定入口模板与依赖；4 种类型骨架差异：

### library（纯库）

```
src/my_package/
├── __init__.py
└── py.typed
```

- `dependencies = []`（无运行时依赖）
- 无 `[project.scripts]`
- `pyproject.toml` classifiers: `Topic :: Software Development :: Libraries :: Python Modules`

### cli（命令行工具）

```
src/my_package/
├── __init__.py
├── py.typed
├── cli.py            # Typer/Click 入口
└── __main__.py       # python -m 入口
```

- `[project.scripts]` 自动生成：`my-package = "my_package.cli:main"`
- `cli.py` 推荐 Typer（类型注解驱动，与 rule-11 类型要求契合）

### gui（PySide2/PySide6 桌面应用）

```
src/my_package/
├── __init__.py
├── py.typed
├── main.py           # QApplication 入口
├── main_window.py    # 主窗口
├── theme.py          # 设计令牌（颜色/尺寸）
├── config.py         # 应用配置
└── widgets/          # 自定义控件
    └── ...
```

- `dependencies`：`PySide2>=5.15.2.1; python_version <= '3.10'` + `PySide6>=6.5.0; python_version >= '3.11'`（双兼容）
- 调用 `python-gui-pyside` SKILL 获取设计系统与代码模板（rule-03 项目场景）
- 遵循 `rule-12-pyside-dev.md` 硬约束

### web（FastAPI 服务）

```
src/my_package/
├── __init__.py
├── py.typed
├── app.py            # FastAPI app 实例
├── routers/          # API 路由
│   └── ...
├── models.py         # Pydantic 请求/响应模型
└── services/         # 业务逻辑
    └── ...
```

- `dependencies`：`fastapi>=0.100.0` + `uvicorn[standard]>=0.20.0`
- 调用 `python-fastapi` SKILL（rule-03 项目场景）

要点：
- **4 种类型共享同一套工具链**（ruff/pytest/coverage/pyrefly/bumpversion/uv）。
- **`project_type` 仅影响 `src/` 入口文件与 `dependencies`**，不影响 tests/docs/CI 结构。
- **新增类型扩展**：在 `copier.yml` 加 `choices` 项，在 `template/src/` 加对应 `{% if project_type == 'xxx' %}` 文件。

## 版本管理

### bump-my-version 配置

`.bumpversion.toml`（**保留 `[tool.bumpversion]` 前缀**，bump-my-version 强制要求）：

```toml
[tool.bumpversion]
current_version = "0.1.0"
commit = true
exclude = ["template/*"]
message = "chore: 更新版本 {current_version} → {new_version}"
tag = true
tag_name = "v{new_version}"

[[tool.bumpversion.files]]
filename = "pyproject.toml"
regex = true
search = '(version\s+=\s+)"{current_version}"'
replace = '\1"{new_version}"'

[[tool.bumpversion.files]]
filename = "src/my_package/__init__.py"
search = '__version__ = "{current_version}"'
replace = '__version__ = "{new_version}"'

[[tool.bumpversion.files]]
filename = ".copier-answers.yml"
regex = true
search = '_commit: v{current_version}'
replace = '_commit: v{new_version}'
```

### bump 命令

```bash
# patch: 0.1.0 → 0.1.1（bug 修复）
uvx bump-my-version bump patch --tag

# minor: 0.1.0 → 0.2.0（新功能，向后兼容）
uvx bump-my-version bump minor --tag

# major: 0.1.0 → 1.0.0（破坏性变更）
uvx bump-my-version bump major --tag
```

要点：
- **`commit = true` + `tag = true`**：bump 时自动提交并打 tag，一步到位。
- **`exclude = ["template/*"]`**：copier 模板项目必需，避免 bump 误改模板内的 `{{ version }}` 占位符。
- **同步 3 处版本号**：`pyproject.toml`（元数据）、`src/<package>/__init__.py`（运行时 `__version__`）、`.copier-answers.yml`（copier 模板项目自身版本追溯）。
- **`regex = true` 配合 `search`/`replace`**：精确匹配 `version = "0.1.0"` 而非 `__version__ = "0.1.0"`；不用 regex 会误改首个匹配。
- **语义化版本（SemVer）**：`MAJOR.MINOR.PATCH`；0.x.x 阶段 MINOR 表示破坏性变更也可接受。
- **`--tag` 显式打 tag**：bump 命令默认不打 tag，需手动加；Makefile `make bump` 已封装。

## 构建与发布

### 构建命令

```bash
uv build                # 同时构建 wheel + sdist
# 产物：dist/my_package-0.1.0-py3-none-any.whl + dist/my_package-0.1.0.tar.gz
```

### 本地验证

```bash
# 构建后本地安装测试
uv build
uv pip install dist/my_package-0.1.0-py3-none-any.whl --reinstall
python -c "import my_package; print(my_package.__version__)"

# 检查 wheel 元数据
uv run python -m zipfile -l dist/my_package-0.1.0-py3-none-any.whl | findstr py.typed
# 应输出：src/my_package/py.typed（PEP 561 标记已包含）
```

### 发布到 PyPI

```bash
# 通过 CI release.yml 自动发布（推荐）
git tag v0.1.0
git push origin v0.1.0
# 触发 release workflow：自动 build + publish + GitHub Release

# 手动发布（应急）
uv publish             # 需配置 PyPI API token 或 OIDC
```

要点：
- **wheel 优先**：PEP 427 wheel 安装快（无需编译）、元数据完整；sdist 作为源码备份。
- **`py.typed` 必须进入 wheel**：`[tool.hatch.build.targets.wheel.force-include]` 显式声明。
- **OIDC trusted publishing 优于 API token**：无需管理 token 生命周期；在 PyPI 项目设置中绑定 GitHub repo 即可。
- **发布前必跑 CI**：所有 jobs 通过再打 tag；`git tag` 触发 release workflow。
- **`gh release create --generate-notes`**：从 commits 自动生成 Release notes，基于 PR/commit 标题。
- **回退已发布版本不可行**：PyPI 不允许重新上传同版本号；只能 yank（`uv run twine yank`）或发新版本。

## Makefile：常用命令快捷方式

```makefile
# Makefile - 项目快捷命令
# 运行 `make help` 查看所有可用命令

.PHONY: help sync build clean lint typecheck check test cov doc bump patch minor major push

help: ## 显示帮助信息
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z].*:.*##/ {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## 安装开发依赖
	uv sync --extra dev

build: ## 构建 Python 包
	uv build

clean: ## 清理构建产物与缓存
	rm -rf build/ dist/ wheels/ *.egg-info htmlcov/ .coverage .coverage.* coverage.xml docs/_build/ .tox/
	rm -rf .ruff_cache/ .pyrefly_cache/ .mypy_cache/

lint: ## 代码风格检查 (ruff)
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## 类型检查 (pyrefly)
	uv run pyrefly check

check: lint typecheck ## 运行全套门禁 (lint + typecheck)

test: ## 运行测试
	uv run pytest

cov: ## 运行测试并生成 HTML 覆盖率报告
	uv run pytest --cov --cov-report=html
	@echo "覆盖率报告：htmlcov/index.html"

doc: ## 构建 Sphinx 文档
	uv run sphinx-build -b html docs docs/_build/html

BUMP_PART := $(filter-out bump,$(MAKECMDGOALS))

bump: ## 版本号 bump (默认 patch，用法: make bump [minor|major])
	@uvx bump-my-version bump $(if $(BUMP_PART),$(firstword $(BUMP_PART)),patch) --tag

patch minor major:
	@:

push: ## 推送代码到所有远程仓库
	@uv run python -c "import subprocess as sp; [print(f'推送 {r}...',flush=True) or (sp.run(['git','push',r],check=True) and sp.run(['git','push',r,'--tags'],check=True)) for r in sp.check_output(['git','remote'],text=True).split()]"
```

要点：
- **`make help` 自动生成**：从 `## ` 注释提取命令说明；新增命令加 `## ` 注释即可。
- **`make check` 是 pre-commit 替代**：本地推送前一键跑 lint + typecheck。
- **`make bump` 支持 `make bump minor`/`make bump major`**：通过 `BUMP_PART` 变量传递参数。
- **`make push` 推送所有远程**：多远程仓库（GitHub + Gitee）一键推送，含 tags。
- **`clean` 不删 `.venv`/`uv.lock`**：仅删可重新生成的缓存与构建产物。

## 常见陷阱

1. **flat layout 误用**：`<package>/` 直接在根目录，测试时 `import package` 指向源码而非安装包，掩盖打包错误。一律用 src layout。
2. **`pyproject.toml` 混入工具链配置**：`[tool.ruff]`/`[tool.pytest]` 等内嵌配置在 `copier update` 时易被覆盖；拆到独立文件。
3. **`__init__.py` 放业务代码**：包入口被业务逻辑污染，import 副作用难追踪。仅放 `__version__`、`__all__` 与公共 API 重导出。
4. **`py.typed` 未配置 force-include**：wheel 不含 `py.typed`，下游项目无法获得类型检查支持。`[tool.hatch.build.targets.wheel.force-include]` 显式声明。
5. **`optional-dependencies` 与 `dependency-groups` 混淆**：前者是 PyPI 安装时用（`pip install pkg[dev]`），后者是开发时用（`uv sync --group dev`）；两者并存不冲突。
6. **`bump-my-version` 未排除模板目录**：copier 模板项目 bump 时误改 `template/` 内 `{{ version }}` 占位符；`.bumpversion.toml` 加 `exclude = ["template/*"]`。
7. **CI 未用 `setup-uv` action**：直接 `run: uv sync` 报 `uv: command not found`（exit 127）；必须先 `uses: astral-sh/setup-uv`。
8. **CI 未用 `--frozen`**：`uv sync` 默认允许更新 `uv.lock`，CI 中应 `--frozen` 严格按 lock 安装；lock 更新在本地进行。
9. **PyPI 发布用 API token**：token 易泄漏、需轮换；改用 OIDC trusted publishing（`permissions: id-token: write` + `environment: pypi`）。
10. **`fail_under` 下调**：覆盖率门禁一旦放宽就难再收紧；新功能未达覆盖率应补测试而非降阈值。
11. **`coverage` 未配置 `concurrency=thread`**：多线程代码覆盖率统计丢失；`.coveragerc` 加 `concurrency = thread`（与 pytest-xdist 多进程兼容）。
12. **`docs/conf.py` 未加 `sys.path.insert`**：autodoc `ImportError`；必须 `sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))`。
13. **`pyrefly check .` per-file 模式**：忽略配置中的 `project-excludes`，template/ 下 Jinja 模板被误检；必须 `uv run pyrefly check`（project 模式）。
14. **`uv.toml` 与 `pyproject.toml [tool.uv]` 同时存在**：uv.toml 覆盖 pyproject.toml 配置；选一处放置，避免分裂。
15. **`__main__.py` 缺少 `if __name__ == "__main__":` 守卫**：被 import 时执行 main()；必须守卫。


