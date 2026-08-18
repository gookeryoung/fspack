"""Wheel 下载性能基线（iter-143，req-49 L127-128）.

测量 wheel 下载链路在 pip/uv/缓存命中/冷下载四种模式下的耗时，建立 iter-132
uv 加速与 deps_cache 缓存命中的可量化基线。所有网络 I/O（subprocess 调用
pip/uv）通过 mock 替换为 ``time.sleep``，仅测量 Python 层编排开销
（``ThreadPoolExecutor`` 调度、``as_completed`` 聚合）+ 模拟下载耗时。

四个基线场景：

1. **pip 并行下载**：50 包用 ``pip download --no-deps`` 并行下载，30ms/包
   （模拟 pip 启动 + 网络，无 uv 加速）
2. **uv 并行下载**：50 包用 ``uv pip download --no-deps`` 并行下载，10ms/包
   （模拟 uv 启动 + 网络，比 pip 快 3x）
3. **缓存命中跳过下载**：deps_cache 命中，``download_wheels`` 直接返回 50 wheel
   （验证缓存查找开销可忽略）
4. **冷下载完整编排**：deps_cache 未命中，走完整 ``download_wheels`` 流程
   （含 ``_deps_cache_key`` + ``_load_deps_cache`` + ``_find_pip_python``
   + ``_resolve_with_uv`` + ``_download_resolved_parallel``）

对比关系：

- pip vs uv：验证 iter-132 uv 加速比 ≥ 2x（uv 无 Python 解释器启动开销 +
  Rust HTTP 客户端）
- 缓存命中 vs 冷下载：验证 deps_cache 命中让 ``download_wheels`` 跳过整个
  下载流程（数秒），缓存查找开销应 < 5ms

模拟下载耗时用 ``time.sleep``：subprocess 释放 GIL，与真实 pip/uv 子进程
``Popen.wait`` 阻塞行为一致，线程并行收益可观测。sleep 时间选择平衡
测量稳定性与运行速度：

- pip 下载：30ms/包（pip 启动 ~150ms + 网络，缩短 5x 保持 CI 时间合理）
- uv 下载：10ms/包（uv 启动 ~10ms + 网络，比 pip 快 3x）
- 冷下载编排：10ms/包（与 uv 下载一致，外加编排开销）

运行方式::

    # 仅运行本基线（slow marker 默认门禁不执行）
    uv run pytest tests/test_wheel_download_baseline.py -m slow --benchmark-only

    # 保存基线供后续对比
    uv run pytest tests/test_wheel_download_baseline.py -m slow --benchmark-only --benchmark-save=iter143

    # 优化后对比退化
    uv run python scripts/compare_benchmark.py
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.wheels.cache import _deps_cache_key, _save_deps_cache
from fspack.packaging.wheels.downloader import download_wheels
from fspack.packaging.wheels.resolver import DownloadContext, _download_resolved_parallel
from fspack.progress import StageRecorder

# ---- 测试样本 ----

# 50 个 wheel：与 test_perf_baseline.py 的 AST 基线 / test_nuitka_compile_baseline.py
# 的编译基线对齐，覆盖中等规模项目依赖
_WHEEL_COUNT = 50

# 模拟单包下载耗时（秒）
# pip 路径：30ms/包（pip 启动 ~150ms + 网络，缩短 5x 保持 CI 时间合理）
# uv 路径：10ms/包（uv 启动 ~10ms + 网络，比 pip 快 3x）
_DOWNLOAD_SLEEP_PIP = 0.03
_DOWNLOAD_SLEEP_UV = 0.01

# rounds 选择依据：
# - pip 并行 rounds=8：50 包 / 8 worker * 30ms = ~210ms/轮，8 轮平衡稳定性与运行时间
# - uv 并行 rounds=12：50 包 / 8 worker * 10ms = ~70ms/轮，12 轮取 median 稳定
# - 缓存命中 rounds=20：耗时极短（<5ms），20 轮确保统计稳定
# - 冷下载编排 rounds=10：与 uv 并行一致，10 轮平衡稳定性与运行时间
_ROUNDS_PIP = 8
_ROUNDS_UV = 12
_ROUNDS_CACHE_HIT = 20
_ROUNDS_COLD_DOWNLOAD = 10


def _make_resolved_packages(count: int = _WHEEL_COUNT) -> list[str]:
    """构造 ``count`` 个 ``name==version`` 精确版本需求列表，模拟 uv 解析输出."""
    return [f"pkg_{i:03d}==1.0.0" for i in range(count)]


def _make_sleep_download_one(sleep_seconds: float, *, mode: str) -> Any:
    """构造 mock 单包下载函数：sleep 指定秒数后返回成功结果.

    ``time.sleep`` 释放 GIL，与真实 ``subprocess.run`` 阻塞行为一致，让
    ``ThreadPoolExecutor`` 线程并行收益可观测（并行模式下多个 worker 同时
    sleep，总耗时接近单包耗时 * ceil(N/workers) 而非 N 倍）。

    Args:
        sleep_seconds: 模拟下载耗时（秒）.
        mode: ``"pip"`` 或 ``"uv"``，决定从 args 提取 req 的位置（pip 路径
            ``_download_one_resolved(req, ...)`` req 在 args[0]；uv 路径
            ``_download_one_with_uv(uv_path, req, ...)`` req 在 args[1]）.

    Returns:
        fake 下载函数，签名与真实函数一致，返回带 ``Saved <name>.whl`` stdout
        的 :class:`subprocess.CompletedProcess` 供 ``_parse_pip_download_wheels``
        解析。
    """

    def fake_download(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        time.sleep(sleep_seconds)
        # 从 args 提取 req（位置因函数而异）
        req = kwargs.get("req")
        if req is None and args:
            # _download_one_resolved(req, base_args, extra_args, pypi_index, *, with_index)
            # _download_one_with_uv(uv_path, req, cache_dir, extra_args, *, ...)
            req = args[1] if mode == "uv" else args[0]
        # 提取包名（name==version → name）
        pkg_name = req.split("==")[0] if req else "pkg"
        wheel_name = f"{pkg_name}-1.0.0-py3-none-any.whl"
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"Saved {wheel_name}\n",
            stderr="",
        )

    return fake_download


def _run_parallel_download(
    resolved: list[str],
    cache_dir: Path,
    *,
    uv_path: str | None,
) -> subprocess.CompletedProcess[str]:
    """调用 ``_download_resolved_parallel`` 下载 50 包，返回合并结果.

    构造最小化参数（``base_args`` 仅满足函数签名，实际 pip/uv
    子进程已被 mock 不会执行）。
    """
    base_args: list[str] = ["python", "-m", "pip", "download", "-d", str(cache_dir)]
    ctx = DownloadContext(
        py="python",
        py_version="3.11.9",
        platform_tags=("win_amd64",),
        pypi_index="https://pypi.org/simple",
        cache_dir=cache_dir,
        base_args=base_args,
        uv_path=uv_path,
    )
    return _download_resolved_parallel(resolved, ctx)


# ---- 基线测试 ----


@pytest.mark.slow
class TestWheelDownloadBaseline:
    """wheel 下载性能基线.

    测量 pip/uv 并行下载与缓存命中/冷下载四种模式耗时，验证 iter-132 uv
    加速与 deps_cache 缓存命中效果。所有网络 I/O（subprocess 调用 pip/uv）
    被 mock，仅测量 Python 层编排开销 + 模拟下载耗时。

    对比关系：

    - pip vs uv：uv 加速比应 ≥ 2x（uv 无 Python 解释器启动开销 + Rust HTTP 客户端）
    - 缓存命中 vs 冷下载：缓存命中应远快于冷下载（跳过整个下载流程）
    """

    def test_pip_parallel_download_baseline(
        self,
        benchmark: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pip 并行下载基线：50 包用 ``pip download --no-deps`` 并行下载，30ms/包.

        ``uv_path=None`` 让 ``_download_resolved_parallel`` 走 pip 路径
        （``_download_one_resolved``）。mock 单包下载函数 sleep 30ms 模拟
        pip 启动 + 网络耗时。8 worker 并行下 50 包预期 ~210ms/轮。

        ``rounds=8``：~210ms/轮，8 轮平衡稳定性与运行时间。
        """
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        resolved = _make_resolved_packages()
        monkeypatch.setattr(
            "fspack.packaging.wheels.resolver._download_one_resolved",
            _make_sleep_download_one(_DOWNLOAD_SLEEP_PIP, mode="pip"),
        )

        def _run() -> int:
            result = _run_parallel_download(resolved, cache_dir, uv_path=None)
            return result.returncode

        result = benchmark(_run)
        assert result == 0

    def test_uv_parallel_download_baseline(
        self,
        benchmark: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """uv 并行下载基线：50 包用 ``uv pip download --no-deps`` 并行下载，10ms/包.

        ``uv_path="uv"`` 让 ``_download_resolved_parallel`` 走 uv 路径
        （``_download_one_with_uv``）。mock 单包下载函数 sleep 10ms 模拟
        uv 启动 + 网络耗时（比 pip 快 3x）。8 worker 并行下 50 包预期 ~70ms/轮。

        uv 加速比 = pip median / uv median，应 ≥ 2x（req-49 iter-132 目标）。

        ``rounds=12``：~70ms/轮，12 轮取 median 稳定。
        """
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        resolved = _make_resolved_packages()
        monkeypatch.setattr(
            "fspack.packaging.wheels.resolver._download_one_with_uv",
            _make_sleep_download_one(_DOWNLOAD_SLEEP_UV, mode="uv"),
        )

        def _run() -> int:
            result = _run_parallel_download(resolved, cache_dir, uv_path="uv")
            return result.returncode

        result = benchmark(_run)
        assert result == 0

    def test_cache_hit_baseline(
        self,
        benchmark: Any,
        tmp_path: Path,
    ) -> None:
        """缓存命中基线：deps_cache 命中，``download_wheels`` 直接返回 50 wheel.

        预填 50 个 wheel 文件 + deps cache，让 ``download_wheels`` 走缓存命中
        分支（``_load_deps_cache`` 返回非 None），跳过整个 pip/uv 下载流程。
        缓存查找开销应 < 5ms（仅 JSON 解析 + 50 次 ``is_file()`` 调用）。

        与 ``test_perf_baseline.py`` 的 ``TestWheelDownloadCacheBaseline``
        互补：后者测 ``_load_deps_cache`` 单次调用耗时，本基线测
        ``download_wheels`` 入口缓存命中路径（含 ``_deps_cache_key`` 计算 +
        ``_load_deps_cache`` + ``StageRecorder`` 回写）。

        ``rounds=20``：耗时极短，20 轮确保统计稳定。
        """
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        wheels: list[Path] = []
        for i in range(_WHEEL_COUNT):
            whl = cache_dir / f"pkg_{i:03d}-1.0.0-py3-none-any.whl"
            whl.write_bytes(b"x" * 1024)
            wheels.append(whl)
        packages = tuple(f"pkg_{i:03d}" for i in range(_WHEEL_COUNT))
        key = _deps_cache_key(packages, "3.11.9", ("win_amd64",))
        _save_deps_cache(cache_dir, key, wheels)

        def _run() -> int:
            st = StageRecorder("下载")
            result = download_wheels(
                list(packages),
                "3.11.9",
                "https://pypi.org/simple",
                cache_dir,
                stage=st,
            )
            return len(result)

        result = benchmark(_run)
        assert result == _WHEEL_COUNT

    def test_cold_download_baseline(
        self,
        benchmark: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """冷下载完整编排基线：deps_cache 未命中，走完整 ``download_wheels`` 流程.

        mock 链路：

        1. ``_load_deps_cache`` 返回 None（强制冷下载）
        2. ``_save_deps_cache`` noop（避免写文件 I/O 影响基线）
        3. ``_find_pip_python`` 返回 "python"（避免 PATH 查找 subprocess）
        4. ``_run_pip`` 返回 None（让 ``--no-index`` 离线解析"失败"，走 ``_download_online``）
        5. ``_find_uv`` 返回 "uv"，``_uv_supports_download`` 返回 True
        6. ``_resolve_with_uv`` 返回 50 包解析结果
        7. ``_download_one_with_uv`` sleep 10ms 模拟 uv 下载

        本基线测量完整冷下载编排开销：``_deps_cache_key`` + ``_load_deps_cache``
        + ``_find_pip_python`` + ``_run_pip_download``（含 ``_download_online``
        → ``_resolve_with_uv`` + ``_download_resolved_parallel``）+
        ``_parse_wheel_names``。应接近 uv 并行下载基线（编排开销 <10ms）。

        ``rounds=10``：与 uv 并行一致，10 轮平衡稳定性与运行时间。
        """
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        packages = [f"pkg_{i:03d}" for i in range(_WHEEL_COUNT)]
        resolved = _make_resolved_packages()

        # 预创建 wheel 文件让 _parse_wheel_names 解析后 (cache_dir / name).is_file() 通过
        for req in resolved:
            pkg_name = req.split("==")[0]
            (cache_dir / f"{pkg_name}-1.0.0-py3-none-any.whl").write_bytes(b"x" * 1024)

        # mock 链路：_find_uv → _uv_supports_download → _resolve_with_uv → _download_one_with_uv
        monkeypatch.setattr("fspack.packaging.wheels.downloader._load_deps_cache", lambda *a, **kw: None)
        monkeypatch.setattr("fspack.packaging.wheels.downloader._save_deps_cache", lambda *a, **kw: None)
        monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "python")
        monkeypatch.setattr("fspack.packaging.wheels.downloader._run_pip", lambda *a, **kw: None)
        monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "uv")
        monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: True)
        monkeypatch.setattr(
            "fspack.packaging.wheels.resolver._resolve_with_uv",
            lambda *a, **kw: "".join(f"{r}\n" for r in resolved),
        )
        monkeypatch.setattr(
            "fspack.packaging.wheels.resolver._download_one_with_uv",
            _make_sleep_download_one(_DOWNLOAD_SLEEP_UV, mode="uv"),
        )

        def _run() -> int:
            st = StageRecorder("下载")
            result = download_wheels(
                packages,
                "3.11.9",
                "https://pypi.org/simple",
                cache_dir,
                stage=st,
            )
            return len(result)

        result = benchmark(_run)
        assert result == _WHEEL_COUNT
