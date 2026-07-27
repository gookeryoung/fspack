"""性能基线测试：核心场景耗时基准，供 iter-52+ 性能优化迭代对比验证.

用 ``pytest-benchmark`` 测量核心场景耗时，建立可复现基线。后续性能优化迭代
（iter-52 AST 并行化、iter-53 wheel 并行解压、iter-54 fingerprint 优化等）
须对比此基线验证性能提升，退化 > 10% 失败。

运行方式：

    # 仅运行基线测试（不运行普通测试）
    uv run pytest tests/test_perf_baseline.py --benchmark-only

    # 查看基线统计（min/median/mean/stddev）
    uv run pytest tests/test_perf_baseline.py --benchmark-only --benchmark-columns=median,min,mean,stddev

    # 与基线对比（先保存基线，再对比）
    uv run pytest tests/test_perf_baseline.py --benchmark-only --benchmark-save=baseline
    # ... 优化后 ...
    uv run pytest tests/test_perf_baseline.py --benchmark-only --benchmark-compare=baseline

标 ``slow`` marker，默认门禁不执行（基线测试用于开发期对比，不阻断 CI）。
基线测试本身用 ``--benchmark-disable`` 时退化为普通测试（仅验证功能正确性）。
"""

from __future__ import annotations

import ast
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.analyzer import (
    analyze_dependencies,
    collect_imports_and_submodules,
    source_fingerprint,
)
from fspack.slim import classify_entry, slim_unpack

# ---- 测试样本 ----

# AST 分析样本：模拟中等规模项目（50 个模块文件，每个含 10+ import）
_SAMPLE_SRC_TEMPLATE = '''\
"""模块 {name}：模拟中等规模项目的源码文件."""
from __future__ import annotations

import os
import sys
import json
import logging
from pathlib import Path
from typing import Any, Optional

import numpy as np
import requests
from PySide2.QtWidgets import QApplication, QMainWindow
from PySide2.QtCore import Qt, QTimer
from PySide2.QtGui import QIcon


def func_{name}(x: int) -> int:
    """示例函数."""
    return x * 2


class Cls_{name}:
    """示例类."""

    def __init__(self, value: int) -> None:
        self.value = value

    def process(self) -> list[int]:
        return [self.value] * 10
'''

# wheel 解压样本：模拟 PySide6 拆分 wheel（200 个条目，混合各类文件）
_WHEEL_ENTRIES: dict[str, bytes] = {
    "PySide6/__init__.py": b"",
    "PySide6/QtCore.pyd": b"x" * 1024,
    "PySide6/QtGui.pyd": b"x" * 1024,
    "PySide6/QtWidgets.pyd": b"x" * 1024,
    "PySide6/Qt6Core.dll": b"x" * 4096,
    "PySide6/Qt6Gui.dll": b"x" * 4096,
    "PySide6/Qt6Widgets.dll": b"x" * 4096,
    "PySide6/plugins/platforms/qwindows.dll": b"x" * 2048,
    "PySide6/plugins/imageformats/qjpeg.dll": b"x" * 1024,
    "PySide6/translations/qt_ar.qm": b"x" * 512,  # 应被剥离
    "PySide6/include/pyside.h": b"x" * 256,  # 应被剥离
    "PySide6/designer.exe": b"x" * 512,  # 应被剥离
    "PySide6/Qt3DCore.pyd": b"x" * 1024,  # 闭包外应被剥离
    "PySide6/Qt6Charts.dll": b"x" * 4096,  # 闭包外应被剥离
    "pyside6-6.11.1.dist-info/METADATA": b"meta",
    "pyside6-6.11.1.dist-info/RECORD": b"record",  # 应被剥离
    "pyside6-6.11.1.dist-info/WHEEL": b"wheel",  # 应被剥离
}


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    """构造 50 个 .py 文件的样本项目（用于 AST 分析基线）."""
    src = tmp_path / "src" / "myproj"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    for i in range(50):
        (src / f"mod_{i:03d}.py").write_text(_SAMPLE_SRC_TEMPLATE.format(name=i), encoding="utf-8")
    return tmp_path / "src"


@pytest.fixture
def sample_wheel(tmp_path: Path) -> Path:
    """构造 PySide6 拆分 wheel 样本（用于解压基线）."""
    whl = tmp_path / "wh" / "pyside6-6.11.1-cp310-abi3-win_amd64.whl"
    whl.parent.mkdir()
    with zipfile.ZipFile(whl, "w") as zf:
        for name, content in _WHEEL_ENTRIES.items():
            zf.writestr(name, content)
    return whl


# ---- 基线测试 ----


@pytest.mark.slow
class TestAstBaseline:
    """AST 依赖分析性能基线."""

    def test_collect_imports_and_submodules_baseline(self, benchmark: Any, sample_project: Path) -> None:
        """单文件 AST 收集基线：collect_imports_and_submodules 单次调用耗时.

        优化目标（iter-52）：多文件并行解析后此基线作为单文件参考，并行收益
        通过 analyze_dependencies_baseline 测量。
        """
        sample_py = sample_project / "myproj" / "mod_000.py"
        tree = ast.parse(sample_py.read_text(encoding="utf-8"))
        result = benchmark(collect_imports_and_submodules, tree)
        # 功能正确性验证（基准测试也验证功能，避免基线退化）
        assert "os" in result[0]
        assert "PySide2" in result[1]
        assert "QtWidgets" in result[1]["PySide2"]

    def test_analyze_dependencies_baseline(self, benchmark: Any, sample_project: Path) -> None:
        """多文件依赖分析基线：50 个 .py 文件全量 AST 解析耗时.

        优化目标（iter-52）：ProcessPoolExecutor 并行解析，预期提速 2-4x
        （CPU 密集 ast.parse，50 文件足够并行收益）。
        """
        result = benchmark(analyze_dependencies, sample_project, "myproj", ())
        # 功能正确性验证
        assert "os" in result.ast_stdlib
        assert "PySide2" in result.ast_third_party
        assert "numpy" in result.ast_third_party
        assert "QtWidgets" in result.ast_submodules.get("PySide2", frozenset())


@pytest.mark.slow
class TestSlimBaseline:
    """wheel 精简解压性能基线."""

    def test_classify_entry_baseline(self, benchmark: Any) -> None:
        """SlimSpec 条目分类基线：classify_entry 单次调用耗时.

        优化目标：分类逻辑本身是 O(1) 字符串操作，基线作为参考点验证
        spec 注册表分发开销可忽略。
        """
        result = benchmark(classify_entry, "PySide6/QtCore.pyd", "PySide6", {"Core"})
        assert result == ("submodule", "Core")

    def test_slim_unpack_baseline(self, benchmark: Any, tmp_path: Path, sample_wheel: Path) -> None:
        """wheel 精简解压基线：单 wheel 按需解压耗时.

        优化目标（iter-53）：多 wheel 并行解压，PySide6 拆分 wheel 场景
        （3 个 wheel）预期提速 2-3x。
        """

        def _unpack() -> int:
            dest = tmp_path / f"sp_{_unpack.counter}"  # type: ignore[attr-defined]
            dest.mkdir()
            count = slim_unpack([sample_wheel], dest, {"PySide6": frozenset({"Core", "Gui", "Widgets"})})
            shutil.rmtree(dest, ignore_errors=True)
            return count

        _unpack.counter = 0  # type: ignore[attr-defined]

        def _run() -> int:
            _unpack.counter += 1  # type: ignore[attr-defined]
            return _unpack()

        result = benchmark(_run)
        assert result == 1


@pytest.mark.slow
class TestFingerprintBaseline:
    """源码指纹计算性能基线."""

    def test_source_fingerprint_baseline(self, benchmark: Any, sample_project: Path) -> None:
        """source_fingerprint 基线：50 个 .py 文件 mtime/size 哈希耗时.

        优化目标（iter-54）：os.walk → os.scandir 递归 + DirEntry.stat，
        预期提速 1.5-2x（减少 stat 系统调用）。
        """
        result = benchmark(source_fingerprint, sample_project)
        # 功能正确性：相同源码两次计算结果一致
        assert result == source_fingerprint(sample_project)
        assert len(result) == 64  # SHA-256 hex
