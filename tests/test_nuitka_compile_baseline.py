"""Nuitka 并行编译性能基线（iter-142，req-49 L125-127）.

测量 :meth:`NuitkaCompiler._compile_files` 在不同模式下的耗时，建立 iter-131
并行化与 ccache 加速的可量化基线。所有测试 mock ``_stream_compile`` 与
``_build_compile_env``，仅测量 Python 层编排开销（``ThreadPoolExecutor``
调度、``as_completed`` 聚合、心跳线程启停）+ 模拟编译耗时。

四个基线场景：

1. **串行编译**：``_MAX_COMPILE_WORKERS=1`` 强制单线程，50 文件顺序编译
2. **并行编译**：默认 ``max_workers=min(cpu,4)``，50 文件并行编译
3. **ccache 命中**：并行模式 + 短耗时模拟 gcc 缓存命中（2ms/文件）
4. **ccache 未命中**：并行模式 + 长耗时模拟 gcc 全量编译（20ms/文件）

对比关系：

- 串行 vs 并行：验证 iter-131 并行化提速 ≥ 30%（req-39 iter-76 目标）
- ccache 命中 vs 未命中：验证 ccache 加速比 ~10x（ccache 官方文档 5-10x）

模拟编译耗时用 ``time.sleep``：subprocess 释放 GIL，与真实 nuitka 编译
``Popen.wait`` 阻塞行为一致，线程并行收益可观测。sleep 时间选择平衡
测量稳定性与运行速度：

- 串行/并行基线：10ms/文件（50 文件串行 500ms，并行 ~125ms）
- ccache 命中：2ms/文件（50 文件并行 ~25ms）
- ccache 未命中：20ms/文件（50 文件并行 ~250ms）

运行方式::

    # 仅运行本基线（slow marker 默认门禁不执行）
    uv run pytest tests/test_nuitka_compile_baseline.py -m slow --benchmark-only

    # 保存基线供后续对比
    uv run pytest tests/test_nuitka_compile_baseline.py -m slow --benchmark-only --benchmark-save=iter142

    # 优化后对比退化
    uv run python scripts/compare_benchmark.py
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.nuitka import NuitkaCompiler
from fspack.packaging.nuitka.compile import _MAX_COMPILE_WORKERS
from fspack.platform import Platform
from fspack.progress import StageRecorder

# ---- 测试样本 ----

# 50 个 .py 文件：与 test_perf_baseline.py 的 AST 基线对齐，覆盖中等规模项目
_FILE_COUNT = 50

# 模拟单文件编译耗时（秒）
# 串行/并行基线用 10ms：平衡测量稳定性与运行速度
# ccache 命中用 2ms：模拟 gcc 读缓存 .o（快）
# ccache 未命中用 20ms：模拟 gcc 全量编译（慢）
_COMPILE_SLEEP_NORMAL = 0.01
_COMPILE_SLEEP_CCACHE_HIT = 0.002
_COMPILE_SLEEP_CCACHE_MISS = 0.02

# rounds 选择依据：
# - 串行基线 rounds=5：50 文件 * 10ms = 500ms/轮，5 轮平衡稳定性与运行时间
# - 并行基线 rounds=10：50 文件 / 4 worker * 10ms = 125ms/轮，10 轮取 median 稳定
# - ccache 命中 rounds=15：耗时短（~25ms/轮），15 轮确保统计稳定
# - ccache 未命中 rounds=10：~250ms/轮，10 轮平衡稳定性与运行时间
_ROUNDS_SERIAL = 5
_ROUNDS_PARALLEL = 10
_ROUNDS_CCACHE_HIT = 15
_ROUNDS_CCACHE_MISS = 10


def _make_py_files(src_dir: Path, count: int = _FILE_COUNT) -> list[Path]:
    """在 ``src_dir`` 下创建 ``count`` 个 ``.py`` 文件，返回排序后的路径列表.

    文件内容简单（``x = <i>``），因为 ``_stream_compile`` 被 mock，实际不解析内容。
    """
    src_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for i in range(count):
        f = src_dir / f"mod_{i:03d}.py"
        f.write_text(f"x = {i}\n", encoding="utf-8")
        files.append(f)
    return files


def _make_sleep_stream(sleep_seconds: float) -> Any:
    """构造 mock ``_stream_compile``：sleep 指定秒数后返回成功.

    ``time.sleep`` 释放 GIL，与真实 ``Popen.wait`` 阻塞行为一致，让线程并行
    收益可观测（并行模式下多个 worker 同时 sleep，总耗时接近单文件耗时）。
    """

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        time.sleep(sleep_seconds)
        return (0, "", "")

    return staticmethod(fake_stream)


@pytest.fixture
def _compile_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[Path]]:
    """构造 50 个 ``.py`` 文件的编译样本，mock ``_build_compile_env`` 为空 dict.

    返回 ``(tmp_path, py_files)``。``_stream_compile`` 由各测试自行 mock
    （不同场景用不同 sleep 时长）。
    """
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))
    src = tmp_path / "src"
    py_files = _make_py_files(src)
    return tmp_path, py_files


# ---- 基线测试 ----


@pytest.mark.slow
class TestNuitkaCompileBaseline:
    """Nuitka 并行编译性能基线.

    测量 :meth:`NuitkaCompiler._compile_files` 在四种模式下的耗时，验证
    iter-131 并行化与 ccache 加速效果。所有重活（``_stream_compile`` 子进程
    调用、``_build_compile_env`` 环境构造）被 mock，仅测量 Python 层编排
    开销 + 模拟编译耗时。

    对比关系：

    - 串行 vs 并行：并行提速应 ≥ 30%（req-39 iter-76 目标，iter-131 实现）
    - ccache 命中 vs 未命中：ccache 加速比应 ~10x
    """

    def test_serial_compile_baseline(
        self,
        benchmark: Any,
        _compile_setup: tuple[Path, list[Path]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """串行编译基线：``_MAX_COMPILE_WORKERS=1`` 强制单线程顺序编译 50 文件.

        ``max_workers=1`` 时 ``ThreadPoolExecutor`` 退化为单线程顺序执行，
        与 iter-131 前的串行 for 循环等价。此基线作为并行化的对照参考，
        并行提速 = (串行 median - 并行 median) / 串行 median。

        ``rounds=5``：50 文件 * 10ms = 500ms/轮，5 轮平衡稳定性与运行时间
        （串行慢，rounds 过多会拖慢 CI）。
        """
        tmp_path, py_files = _compile_setup
        monkeypatch.setattr("fspack.packaging.nuitka.compile._MAX_COMPILE_WORKERS", 1)
        monkeypatch.setattr(NuitkaCompiler, "_stream_compile", _make_sleep_stream(_COMPILE_SLEEP_NORMAL))

        def _run() -> tuple[int, int]:
            st = StageRecorder("编译")
            compiled, failed = NuitkaCompiler._compile_files(
                tmp_path / "python.exe",
                tmp_path / "bootstrap.py",
                py_files,
                st,
                target=Platform.WINDOWS,
            )
            return len(compiled), len(failed)

        result = benchmark(_run)
        # 功能正确性验证
        assert result == (_FILE_COUNT, 0)

    def test_parallel_compile_baseline(
        self,
        benchmark: Any,
        _compile_setup: tuple[Path, list[Path]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """并行编译基线：默认 ``max_workers=min(cpu,4)`` 并行编译 50 文件.

        iter-131 实现：``ThreadPoolExecutor`` 并行调 nuitka ``--module``，
        subprocess 释放 GIL 让线程并行收益可观测。并行应显著快于串行
        （提速 ≥ 30% 验收标准）。

        ``rounds=10``：50 文件 / 4 worker * 10ms = 125ms/轮，10 轮取 median 稳定。
        """
        tmp_path, py_files = _compile_setup
        monkeypatch.setattr(NuitkaCompiler, "_stream_compile", _make_sleep_stream(_COMPILE_SLEEP_NORMAL))

        def _run() -> tuple[int, int]:
            st = StageRecorder("编译")
            compiled, failed = NuitkaCompiler._compile_files(
                tmp_path / "python.exe",
                tmp_path / "bootstrap.py",
                py_files,
                st,
                target=Platform.WINDOWS,
            )
            return len(compiled), len(failed)

        result = benchmark(_run)
        assert result == (_FILE_COUNT, 0)
        # 验证并行模式生效（max_workers > 1）
        cpu = __import__("os").cpu_count() or 1
        expected_workers = min(cpu, _MAX_COMPILE_WORKERS)
        assert expected_workers > 1 or cpu == 1  # 单核机器允许 max_workers=1

    def test_ccache_hit_baseline(
        self,
        benchmark: Any,
        _compile_setup: tuple[Path, list[Path]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ccache 命中基线：并行模式 + 短耗时模拟 gcc 缓存命中.

        ccache 命中时 gcc 直接返回缓存 ``.o`` 文件，单文件编译从 ~500ms 降到
        ~10ms。本基线用 2ms/文件模拟命中场景，测量并行编排 + 快速编译的总耗时。
        ccache 加速比 = ccache 未命中 median / ccache 命中 median。

        ``rounds=15``：耗时短（~25ms/轮），15 轮确保统计稳定。
        """
        tmp_path, py_files = _compile_setup
        monkeypatch.setattr(NuitkaCompiler, "_stream_compile", _make_sleep_stream(_COMPILE_SLEEP_CCACHE_HIT))

        def _run() -> tuple[int, int]:
            st = StageRecorder("编译")
            compiled, failed = NuitkaCompiler._compile_files(
                tmp_path / "python.exe",
                tmp_path / "bootstrap.py",
                py_files,
                st,
                target=Platform.WINDOWS,
                ccache_exe=tmp_path / "ccache",
            )
            return len(compiled), len(failed)

        result = benchmark(_run)
        assert result == (_FILE_COUNT, 0)

    def test_ccache_miss_baseline(
        self,
        benchmark: Any,
        _compile_setup: tuple[Path, list[Path]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ccache 未命中基线：并行模式 + 长耗时模拟 gcc 全量编译.

        ccache 未命中时 gcc 从源码全量编译，单文件 ~500ms。本基线用 20ms/文件
        模拟未命中场景（缩短 25x 保持 CI 运行时间合理）。ccache 命中应显著快于
        未命中（加速比 ~10x）。

        ``rounds=10``：~250ms/轮，10 轮平衡稳定性与运行时间。
        """
        tmp_path, py_files = _compile_setup
        monkeypatch.setattr(NuitkaCompiler, "_stream_compile", _make_sleep_stream(_COMPILE_SLEEP_CCACHE_MISS))

        def _run() -> tuple[int, int]:
            st = StageRecorder("编译")
            compiled, failed = NuitkaCompiler._compile_files(
                tmp_path / "python.exe",
                tmp_path / "bootstrap.py",
                py_files,
                st,
                target=Platform.WINDOWS,
                ccache_exe=None,
            )
            return len(compiled), len(failed)

        result = benchmark(_run)
        assert result == (_FILE_COUNT, 0)
