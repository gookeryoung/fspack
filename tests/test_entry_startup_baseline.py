"""入口包装器启动耗时基线（iter-144，req-49 L129-130）.

测量 :class:`EntryWrapper` 生成的 wrapper 在四种启动模式下的端到端启动
耗时（subprocess wall time），建立 lazy-import 与 ``--no-site`` 优化的
可量化基线。用真实 ``subprocess.run`` 启动 Python 解释器执行 wrapper，
所有耗时都是真实的（无 mock），但通过最小化 dist 结构（仅 1 个模拟
``numpy`` 包 + 1 个用户入口）控制绝对耗时，让优化收益可观测。

四个基线场景：

1. **默认启动**：``python _entry_app.py``（no-site 关闭 + lazy 关闭），
   ``numpy/__init__.py`` 的 ``time.sleep(0.05)`` 全量执行
2. **lazy-import 启用**：wrapper 注入 ``_LazyImportFinder``，
   ``import numpy`` 仅创建 :class:`LazyLoader` 包装的模块对象，
   ``__init__.py`` 不执行（app 不访问 numpy 属性），省 ~50ms
3. **no-site 启用**：``python -S _entry_app.py`` 模拟 ``--no-site``，
   跳过 ``site.py`` 加载（~10-20ms），但 ``import numpy`` 仍执行
4. **no-site + lazy 组合**：``python -S`` + lazy 同时启用，双重优化

对比关系：

- 默认 vs lazy：lazy 收益 = ``numpy/__init__.py`` 延迟执行（~50ms）
- 默认 vs no-site：no-site 收益 = ``site.py`` 不加载（~10-20ms）
- 默认 vs 组合：双重优化收益 ≈ lazy 收益 + no-site 收益

模拟 ``numpy/__init__.py`` 用 ``time.sleep(0.05)``：

- 真实 numpy ``__init__.py`` 启动耗时 ~80-150ms（C 扩展初始化 + 子模块
  导入），用 50ms 模拟保持 CI 时间合理，同时让 lazy 收益显著（>10x
  测量噪声）
- :class:`importlib.util.LazyLoader` 对纯 Python 模块（含 ``time.sleep``
  的 ``__init__.py``）有效，能真实延迟 ``__init__.py`` 执行

运行方式::

    # 仅运行本基线（slow marker 默认门禁不执行）
    uv run pytest tests/test_entry_startup_baseline.py -m slow --benchmark-only

    # 保存基线供后续对比
    uv run pytest tests/test_entry_startup_baseline.py -m slow --benchmark-only --benchmark-save=iter144

    # 优化后对比退化
    uv run python scripts/compare_benchmark.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.entry import EntryWrapper

# ---- 测试样本 ----

# 模拟 numpy __init__.py 耗时（秒）
# 真实 numpy __init__.py 启动 ~80-150ms，用 50ms 模拟保持 CI 时间合理
# 50ms 让 lazy 收益（省 __init__.py 执行）显著大于测量噪声（subprocess
# 启动抖动 ~5-10ms），lazy vs 默认 median 差应 ~50ms
_NUMPY_INIT_SLEEP = 0.05

# rounds 选择依据：
# - subprocess 启动抖动较大（OS 调度、文件系统缓存），需要足够轮数取 median
# - 默认/lazy/no-site/组合 均用 rounds=10：~50-80ms/轮，10 轮平衡稳定性与
#   CI 运行时间（4 基线 * 10 轮 * ~70ms = ~2.8s 总时间）
# - 与 iter-141 的 medium_cold rounds=15 相比略低，因每轮耗时更短
_ROUNDS = 10

# python -X importtime 输出行格式：
#   import time:      5123 |        1 |   numpy
# 第一列是 cumulative（含子导入），第二列是 self（仅本模块）
# 用 cumulative 验证 lazy 是否真延迟了 numpy __init__.py 执行
_IMPORTTIME_RE = re.compile(r"^import time:\s*\d+\s*\|\s*(\d+)\s*\|.*\bnumpy\b")


def _make_minimal_dist(
    dist_dir: Path,
    *,
    lazy_imports: tuple[str, ...] = (),
) -> Path:
    """构造最小 dist 目录：模拟 numpy + 用户入口 + 生成的 wrapper.

    目录结构::

        dist/
        ├── _entry_app.py          # EntryWrapper 生成的包装器
        ├── src/
        │   └── app.py             # 用户入口（import numpy; print）
        └── runtime/
            └── Lib/
                └── site-packages/
                    └── numpy/
                        └── __init__.py  # time.sleep(0.05) 模拟重量级 init

    Args:
        dist_dir: dist 根目录（tmp_path 下的子目录）.
        lazy_imports: 传给 :meth:`EntryWrapper.generate_wrapper_source` 的
            ``lazy_imports`` 参数。空元组时 wrapper 不注入
            ``_LazyImportFinder``，``import numpy`` 立即执行 ``__init__.py``；
            非空时注入 finder，``import numpy`` 仅创建 LazyLoader 包装的
            模块对象，``__init__.py`` 延迟到首次属性访问才执行。

    Returns:
        wrapper 文件路径 ``dist/_entry_app.py``.
    """
    # 模拟 numpy 包：__init__.py sleep 50ms 模拟重量级 init
    numpy_init = dist_dir / "runtime" / "Lib" / "site-packages" / "numpy" / "__init__.py"
    numpy_init.parent.mkdir(parents=True, exist_ok=True)
    numpy_init.write_text(
        f"import time\ntime.sleep({_NUMPY_INIT_SLEEP})\n",
        encoding="utf-8",
    )

    # 用户入口：import numpy 但不访问其属性
    # - 默认模式：import 触发 __init__.py 执行（sleep 50ms）
    # - lazy 模式：import 仅创建 LazyLoader 模块对象，__init__.py 不执行
    src_dir = dist_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "app.py").write_text(
        'import numpy\nprint("hello")\n',
        encoding="utf-8",
    )

    # 生成 wrapper：顶层模式（module_dotted=None），runpy.run_path 执行 app.py
    wrapper_source = EntryWrapper.generate_wrapper_source(
        entry_name="app",
        module_dotted=None,
        entry_rel="app.py",
        pkg_root_rel=".",
        has_tkinter=False,
        lazy_imports=lazy_imports,
    )
    wrapper_path = dist_dir / "_entry_app.py"
    wrapper_path.write_text(wrapper_source, encoding="utf-8")
    return wrapper_path


def _measure_wall_ms(wrapper_path: Path, *, no_site: bool) -> float:
    """启动 wrapper 并测量 wall time（毫秒）.

    用 :func:`time.perf_counter` 包住 :func:`subprocess.run`，返回端到端
    wall time（含解释器启动 + wrapper 执行 + numpy import + 退出）。
    功能验证由 :func:`_verify_importtime_lazy` 单独负责。
    """
    cmd: list[str] = [sys.executable]
    if no_site:
        cmd.append("-S")
    cmd.append(str(wrapper_path))
    start = time.perf_counter()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if result.returncode != 0 or "hello" not in result.stdout:
        raise AssertionError(
            f"wrapper 执行失败: returncode={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return elapsed_ms


def _verify_importtime_lazy(
    dist_dir: Path,
    wrapper_path: Path,
    *,
    lazy_enabled: bool,
) -> None:
    """用 ``python -X importtime`` 验证 lazy 是否真延迟了 numpy __init__.py.

    ``python -X importtime`` 在 stderr 输出每个 import 的 cumulative/self
    耗时（微秒）。解析 ``numpy`` 行的 cumulative：

    - lazy 关闭：cumulative 应 > 40000us（含 ``time.sleep(50ms)`` = 50000us）
    - lazy 启用：cumulative 应 < 10000us（仅 LazyLoader 创建，不执行
      ``__init__.py``）

    本函数仅作为功能验证辅助，不进入基线测试主流程（基线测 wall time）。

    Args:
        dist_dir: dist 根目录（未使用，保留以备扩展）.
        wrapper_path: wrapper 文件路径.
        lazy_enabled: 预期 lazy 是否启用.
    """
    cmd = [sys.executable, "-X", "importtime", str(wrapper_path)]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"python -X importtime 执行失败: returncode={result.returncode}\nstderr={result.stderr!r}")
    # 从 stderr 解析 numpy cumulative
    numpy_cumulative_us: int | None = None
    for line in result.stderr.splitlines():
        m = _IMPORTTIME_RE.match(line)
        if m:
            numpy_cumulative_us = int(m.group(1))
            break
    if numpy_cumulative_us is None:
        raise AssertionError(f"未在 importtime 输出中找到 numpy 行:\n{result.stderr}")
    if lazy_enabled:
        # lazy 启用：numpy __init__.py 不执行，cumulative 应 < 10000us
        # （LazyLoader 创建开销 ~100-500us，加上 sys.meta_path 查找）
        assert numpy_cumulative_us < 10000, (
            f"lazy 启用但 numpy cumulative={numpy_cumulative_us}us 应 < 10000us（__init__.py 不应执行）"
        )
    else:
        # lazy 关闭：numpy __init__.py 全量执行，cumulative 应 > 40000us
        # （time.sleep(50ms) = 50000us，加上 importlib 开销）
        assert numpy_cumulative_us > 40000, (
            f"lazy 关闭但 numpy cumulative={numpy_cumulative_us}us 应 > 40000us（__init__.py 应执行 sleep(0.05)）"
        )


# ---- 基线测试 ----


@pytest.mark.slow
class TestEntryStartupBaseline:
    """入口包装器启动耗时基线.

    测量 :class:`EntryWrapper` 生成的 wrapper 在四种启动模式下的端到端
    启动耗时（subprocess wall time），验证 lazy-import 与 ``--no-site``
    优化效果。用真实 ``subprocess.run`` 启动 Python 解释器，所有耗时都是
    真实的（无 mock），但通过最小化 dist 结构控制绝对耗时。

    对比关系：

    - 默认 vs lazy：lazy 收益 = ``numpy/__init__.py`` 延迟执行（~50ms）
    - 默认 vs no-site：no-site 收益 = ``site.py`` 不加载（~10-20ms）
    - 默认 vs 组合：双重优化收益 ≈ lazy 收益 + no-site 收益
    """

    def test_default_startup_baseline(
        self,
        benchmark: Any,
        tmp_path: Path,
    ) -> None:
        """默认启动基线：``python _entry_app.py``（no-site 关闭 + lazy 关闭）.

        ``numpy/__init__.py`` 的 ``time.sleep(0.05)`` 全量执行，wall time
        含：解释器启动 + site.py 加载 + wrapper 执行 + numpy import
        （50ms sleep）+ ``print("hello")``。预期 ~70-90ms。

        作为 lazy/no-site/组合 基线的对照参考，优化收益 = 默认 median -
        优化 median。

        ``rounds=10``：~70-90ms/轮，10 轮取 median 平衡 subprocess 抖动
        与 CI 运行时间。
        """
        dist_dir = tmp_path / "dist"
        wrapper_path = _make_minimal_dist(dist_dir, lazy_imports=())

        def _run() -> float:
            return _measure_wall_ms(wrapper_path, no_site=False)

        result = benchmark.pedantic(_run, rounds=_ROUNDS, iterations=1)
        # 功能正确性验证（基线测试也验证功能）
        assert result > 0
        # 默认模式 numpy __init__.py 应执行（cumulative > 40000us）
        _verify_importtime_lazy(dist_dir, wrapper_path, lazy_enabled=False)

    def test_lazy_import_startup_baseline(
        self,
        benchmark: Any,
        tmp_path: Path,
    ) -> None:
        """lazy-import 启用基线：wrapper 注入 ``_LazyImportFinder``.

        ``--lazy-import numpy`` 让 wrapper 注入 :class:`_LazyImportFinder`
        meta path finder，``import numpy`` 仅创建 :class:`LazyLoader` 包装
        的模块对象，``__init__.py`` 不执行（app 不访问 numpy 属性）。

        lazy 收益 = 默认 median - lazy median，应接近 ``numpy/__init__.py``
        的 ``time.sleep(0.05)`` 耗时（~50ms）。

        ``rounds=10``：~20-40ms/轮（比默认少 ~50ms），10 轮取 median 稳定。
        """
        dist_dir = tmp_path / "dist"
        wrapper_path = _make_minimal_dist(dist_dir, lazy_imports=("numpy",))

        def _run() -> float:
            return _measure_wall_ms(wrapper_path, no_site=False)

        result = benchmark.pedantic(_run, rounds=_ROUNDS, iterations=1)
        assert result > 0
        # lazy 启用模式 numpy __init__.py 不应执行（cumulative < 10000us）
        _verify_importtime_lazy(dist_dir, wrapper_path, lazy_enabled=True)

    def test_no_site_startup_baseline(
        self,
        benchmark: Any,
        tmp_path: Path,
    ) -> None:
        """no-site 启用基线：``python -S _entry_app.py`` 模拟 ``--no-site``.

        ``python -S`` 跳过 ``site.py`` 加载（~10-20ms），但 ``import numpy``
        仍执行 ``__init__.py``（sleep 50ms）。no-site 收益 = 默认 median -
        no-site median，应 ~10-20ms（site.py 加载耗时）。

        注：``python -S`` 也跳过 ``site-packages`` 路径设置，但 wrapper 自身
        会显式 ``sys.path.insert(0, _SITE_PACKAGES)``，所以 numpy 仍可 import。

        ``rounds=10``：~60-80ms/轮（比默认少 ~10-20ms），10 轮取 median 稳定。
        """
        dist_dir = tmp_path / "dist"
        wrapper_path = _make_minimal_dist(dist_dir, lazy_imports=())

        def _run() -> float:
            return _measure_wall_ms(wrapper_path, no_site=True)

        result = benchmark.pedantic(_run, rounds=_ROUNDS, iterations=1)
        assert result > 0
        # no-site 模式不影响 numpy __init__.py 执行，cumulative 仍 > 40000us
        _verify_importtime_lazy(dist_dir, wrapper_path, lazy_enabled=False)

    def test_no_site_lazy_combined_baseline(
        self,
        benchmark: Any,
        tmp_path: Path,
    ) -> None:
        """no-site + lazy 组合基线：``python -S`` + lazy 同时启用.

        双重优化：``python -S`` 跳过 ``site.py`` + lazy 延迟 ``numpy/__init__.py``。
        组合收益 ≈ no-site 收益 + lazy 收益，应接近默认 median - ~60-70ms
        （site.py ~10-20ms + numpy __init__ ~50ms）。

        ``rounds=10``：~10-30ms/轮（最快），10 轮取 median 稳定。
        """
        dist_dir = tmp_path / "dist"
        wrapper_path = _make_minimal_dist(dist_dir, lazy_imports=("numpy",))

        def _run() -> float:
            return _measure_wall_ms(wrapper_path, no_site=True)

        result = benchmark.pedantic(_run, rounds=_ROUNDS, iterations=1)
        assert result > 0
        # 组合模式 lazy 启用，numpy __init__.py 不应执行（cumulative < 10000us）
        _verify_importtime_lazy(dist_dir, wrapper_path, lazy_enabled=True)
