"""``build()`` 端到端编排性能基线（iter-141，req-49 L122-124）.

测量 :func:`fspack.packaging.pipeline.build` 完整流水线编排耗时，作为打包
速度端到端基线。所有重活（runtime 下载、wheel 下载、源码编译、loader 编译）
通过 ``monkeypatch`` 替换为 noop，仅测量阶段编排 + ``BuildTracker`` +
``ProjectInfo`` 解析 + ``console`` 渲染等开销。此基线作为 iter-142~144
（Nuitka 编译/wheel 下载/启动时间）优化的对照参考：若各专项优化让此基线
退化 > 25% 说明编排层引入了无谓开销。

冷/热缓存差异主要在 :meth:`ProjectInfo.from_dir` 的 ``lru_cache`` 命中情况：

- **冷缓存**：每轮 ``setup`` 调 :func:`clear_project_cache`，``from_dir`` 重新
  解析 ``pyproject.toml`` + 入口 AST 扫描 + ``app_type`` 推断
- **热缓存**：预热一次后 ``from_dir`` 命中 ``lru_cache``，仅 ``stat`` + dict 查询

中项目（10 入口、20 依赖）反映多入口编排开销：``all_entries`` 元组构建与
``_build_entry_loaders`` 分发逻辑（loader 编译本身被 mock，实际编译开销见
iter-142 Nuitka 编译基线）。

运行方式::

    # 仅运行本基线（slow marker 默认门禁不执行）
    uv run pytest tests/test_build_perf_baseline.py -m slow --benchmark-only

    # 保存基线供后续对比
    uv run pytest tests/test_build_perf_baseline.py -m slow --benchmark-only --benchmark-save=iter141

    # 优化后对比退化
    uv run python scripts/compare_benchmark.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack.config import (
    BuildOptions,
    DependencyReport,
    ProjectInfo,
    clear_project_cache,
)

# ---- mock pipeline 阶段函数为 noop ----
# 与 test_profile.py / test_log_file.py 一致，patch fspack.packaging.pipeline.<fn>
# 让 build() 仅测量编排开销，不实际下载/编译/写文件。


def _empty_report() -> DependencyReport:
    """构造空 :class:`DependencyReport` 用于 mock."""
    return DependencyReport(
        declared=(),
        ast_third_party=(),
        ast_stdlib=(),
        ast_local=(),
        ast_submodules={},
    )


@pytest.fixture
def _mock_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """mock pipeline 写操作避免实际下载/编译.

    与 :func:`tests.test_profile.test_build_with_profile_outputs_report` 一致，
    patch 7 个阶段函数为 noop。``_slim_runtime`` 在 Windows 目标下自动跳过
    （函数内 ``_trim_standalone_runtime`` 检测到 Windows 直接 return），
    ``_resolve_project_icon`` 在无 favicon 时直接返回默认 icon（轻量），
    无需额外 mock。
    """
    monkeypatch.setattr(
        "fspack.packaging.pipeline._prepare_runtime",
        lambda ctx: ctx.cfg.dist_dir / "site-packages",
    )
    monkeypatch.setattr("fspack.packaging.pipeline.executor._analyze_dependencies", lambda ctx, **kw: _empty_report())
    monkeypatch.setattr("fspack.packaging.pipeline.executor._download_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr("fspack.packaging.pipeline.executor.write_pth", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor.copy_source", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor._compile_user_sources", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor._build_entry_loaders", lambda *a, **kw: [])


# ---- 小项目样本：1 入口、3 依赖 ----

_SMALL_PYPROJECT = """\
[project]
name = "smallapp"
version = "1.0.0"
requires-python = ">=3.8"
dependencies = ["numpy", "requests", "PySide2"]

[tool.fspack]
pyc_strip = true
no_site = true
"""

_SMALL_ENTRY = '''\
"""小项目入口."""
from __future__ import annotations

import os
import sys

import numpy as np
import requests
from PySide2.QtWidgets import QApplication


def main() -> None:
    """主入口."""
    print("hello")


if __name__ == "__main__":
    main()
'''


@pytest.fixture
def small_project(tmp_path: Path) -> Path:
    """构造小项目样本（1 入口、3 依赖）."""
    proj = tmp_path / "small"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(_SMALL_PYPROJECT, encoding="utf-8")
    (proj / "smallapp.py").write_text(_SMALL_ENTRY, encoding="utf-8")
    return proj


# ---- 中项目样本：10 入口、20 依赖 ----

# 20 个依赖（覆盖 scientific / gui / web / io / db 领域，便于后续 iter-142/143
# 按领域分组测 Nuitka 编译与 wheel 下载基线）。声明用 PyPI 包名；入口源码
# 仅引用其中合法 Python 标识符的子集（避免 PyPI 名含 ``-`` 等非法字符）。
_MEDIUM_DEPS = [
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "Pillow",
    "requests",
    "urllib3",
    "aiohttp",
    "httpx",
    "websocket-client",
    "PySide2",
    "PySide6",
    "PyQt5",
    "PyQt6",
    "tkinter8",
    "sqlalchemy",
    "psycopg2-binary",
    "redis",
    "pymongo",
    "pyyaml",
]

# 入口源码可安全 ``import`` 的子集（PyPI 名含 ``-`` 或与模块名不一致的剔除）
_IMPORTABLE_DEPS = [d for d in _MEDIUM_DEPS if d.replace("_", "").isalnum() and d not in {"Pillow", "pyyaml"}]


def _medium_pyproject() -> str:
    """生成中项目 ``pyproject.toml``（10 入口、20 依赖）."""
    deps = ", ".join(f'"{d}"' for d in _MEDIUM_DEPS)
    entries_lines = "\n".join(f'app{i} = "app{i}:main"' for i in range(10))
    return f"""\
[project]
name = "mediumapp"
version = "2.0.0"
requires-python = ">=3.9"
dependencies = [{deps}]

[project.scripts]
{entries_lines}

[tool.fspack]
pyc_strip = true
no_site = true
"""


def _medium_entry_source(idx: int) -> str:
    """生成中项目第 ``idx`` 个入口源码（含 stdlib + 部分 third-party import）.

    每 5 个入口引用一组不同的依赖组合，让 AST 扫描有实际内容可解析。
    """
    stdlib_lines = "\n".join(
        [
            "import os",
            "import sys",
            "import json",
            "import logging",
            "from pathlib import Path",
        ]
    )
    third_party = [
        _IMPORTABLE_DEPS[idx % len(_IMPORTABLE_DEPS)],
        _IMPORTABLE_DEPS[(idx + 7) % len(_IMPORTABLE_DEPS)],
    ]
    third_lines = "\n".join(f"import {m}" for m in third_party)
    return f'''\
"""中项目入口 {idx}."""
from __future__ import annotations

{stdlib_lines}

{third_lines}


def main() -> None:
    """主入口 {idx}."""
    print("hello from app{idx}")


if __name__ == "__main__":
    main()
'''


@pytest.fixture
def medium_project(tmp_path: Path) -> Path:
    """构造中项目样本（10 入口、20 依赖）."""
    proj = tmp_path / "medium"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(_medium_pyproject(), encoding="utf-8")
    for i in range(10):
        (proj / f"app{i}.py").write_text(_medium_entry_source(i), encoding="utf-8")
    return proj


# ---- 基线测试 ----

# rounds 选择依据：
# - 含文件 I/O（pyproject 解析、入口 AST 扫描），抖动较大
# - 小项目 rounds=20：与 test_perf_baseline.py 的 ProjectInfo 冷解析基线一致
# - 中项目 rounds=15：10 入口 AST 扫描开销大，15 轮平衡稳定性与运行时间
_ROUNDS_SMALL = 20
_ROUNDS_MEDIUM = 15


@pytest.mark.slow
class TestBuildPerfBaseline:
    """``build()`` 端到端编排性能基线.

    测量 ``build()`` 全流程编排耗时（mock 掉下载/编译等重活），作为打包
    速度的端到端参考点。冷/热缓存差异体现 ``ProjectInfo.from_dir`` 的
    ``lru_cache`` 收益，多入口场景体现 ``all_entries`` 元组构建与
    ``_build_entry_loaders`` 分发开销。

    使用 ``BuildOptions(no_sbom=True, no_size_report=True)`` 跳过 SBOM 生成
    与 size report 扫描：两者会写入/扫描 dist 目录，引入 I/O 噪声且跨轮次
    累积文件触发 ``_handle_dist_incomplete`` 半成品告警。
    """

    def test_small_project_cold_cache_baseline(
        self,
        benchmark: Any,
        small_project: Path,
        _mock_pipeline: None,
    ) -> None:
        """小项目冷缓存构建基线：每轮清空 ``ProjectInfo`` 缓存后单次 ``build``.

        冷缓存场景反映首次构建耗时（``pyproject`` 解析 + 入口 AST 扫描 +
        ``app_type`` 推断 + 阶段编排）。``rounds=20``：含文件 I/O，抖动大，
        20 轮取 median 稳定，避免 CI 跨运行对比误报退化。
        """
        from fspack.config import get_mirror
        from fspack.console import console
        from fspack.packaging.pipeline import build
        from fspack.platform import Platform

        opts = BuildOptions(no_sbom=True, no_size_report=True)
        kwargs: dict[str, object] = {
            "mirror": get_mirror("huawei"),
            "py_version": "3.11.9",
            "target": Platform.WINDOWS,
            "options": opts,
        }

        def _setup() -> tuple[tuple[Path], dict[str, object]]:
            clear_project_cache()
            return (small_project,), kwargs

        with console.rich.capture():
            result = benchmark.pedantic(
                build,
                setup=_setup,
                rounds=_ROUNDS_SMALL,
                iterations=1,
            )
        # 功能正确性验证（基线测试也验证功能，避免基线退化）
        assert result.name == "smallapp"
        assert result.version == "1.0.0"
        assert result.dependencies == ("numpy", "requests", "PySide2")
        assert result.app_type.value == "gui"  # PySide2 import 触发 GUI 推断

    def test_small_project_warm_cache_baseline(
        self,
        benchmark: Any,
        small_project: Path,
        _mock_pipeline: None,
    ) -> None:
        """小项目热缓存构建基线：预热后 ``ProjectInfo.from_dir`` 命中 ``lru_cache``.

        热缓存场景反映重复构建耗时（缓存查找 + 阶段编排）。应显著快于冷缓存
        （省去 ``tomllib`` 解析与 AST 扫描）。退化 > 25% 失败。
        """
        from fspack.config import get_mirror
        from fspack.console import console
        from fspack.packaging.pipeline import build
        from fspack.platform import Platform

        # 预热一次填充缓存
        ProjectInfo.from_dir(small_project)
        opts = BuildOptions(no_sbom=True, no_size_report=True)
        kwargs: dict[str, object] = {
            "mirror": get_mirror("huawei"),
            "py_version": "3.11.9",
            "target": Platform.WINDOWS,
            "options": opts,
        }

        def _setup() -> tuple[tuple[Path], dict[str, object]]:
            return (small_project,), kwargs

        with console.rich.capture():
            result = benchmark.pedantic(
                build,
                setup=_setup,
                rounds=_ROUNDS_SMALL,
                iterations=1,
            )
        assert result.name == "smallapp"
        assert result.dependencies == ("numpy", "requests", "PySide2")

    def test_medium_project_cold_cache_baseline(
        self,
        benchmark: Any,
        medium_project: Path,
        _mock_pipeline: None,
    ) -> None:
        """中项目冷缓存构建基线：10 入口、20 依赖.

        多入口场景反映 ``all_entries`` 解析与 ``_build_entry_loaders`` 编排
        开销（loader 编译本身被 mock）。冷缓存含 10 个入口 AST 扫描，预期
        显著慢于小项目冷缓存。``rounds=15``：10 入口 AST 扫描开销大，15 轮
        平衡稳定性与运行时间。
        """
        from fspack.config import get_mirror
        from fspack.console import console
        from fspack.packaging.pipeline import build
        from fspack.platform import Platform

        opts = BuildOptions(no_sbom=True, no_size_report=True)
        kwargs: dict[str, object] = {
            "mirror": get_mirror("huawei"),
            "py_version": "3.11.9",
            "target": Platform.WINDOWS,
            "options": opts,
        }

        def _setup() -> tuple[tuple[Path], dict[str, object]]:
            clear_project_cache()
            return (medium_project,), kwargs

        with console.rich.capture():
            result = benchmark.pedantic(
                build,
                setup=_setup,
                rounds=_ROUNDS_MEDIUM,
                iterations=1,
            )
        # 功能正确性验证
        assert result.name == "mediumapp"
        assert result.version == "2.0.0"
        assert len(result.all_entries) == 10
        assert len(result.dependencies) == 20

    def test_medium_project_warm_cache_baseline(
        self,
        benchmark: Any,
        medium_project: Path,
        _mock_pipeline: None,
    ) -> None:
        """中项目热缓存构建基线：10 入口、20 依赖，预热后命中 ``lru_cache``.

        热缓存下中项目应接近小项目热缓存（缓存查找 O(1)，阶段编排开销
        与入口数无关——loader 编译被 mock）。如显著慢则说明 ``all_entries``
        或 ``entries`` 元组在 ``build()`` 内被反复重建。
        """
        from fspack.config import get_mirror
        from fspack.console import console
        from fspack.packaging.pipeline import build
        from fspack.platform import Platform

        ProjectInfo.from_dir(medium_project)
        opts = BuildOptions(no_sbom=True, no_size_report=True)
        kwargs: dict[str, object] = {
            "mirror": get_mirror("huawei"),
            "py_version": "3.11.9",
            "target": Platform.WINDOWS,
            "options": opts,
        }

        def _setup() -> tuple[tuple[Path], dict[str, object]]:
            return (medium_project,), kwargs

        with console.rich.capture():
            result = benchmark.pedantic(
                build,
                setup=_setup,
                rounds=_ROUNDS_MEDIUM,
                iterations=1,
            )
        assert result.name == "mediumapp"
        assert len(result.all_entries) == 10
        assert len(result.dependencies) == 20
