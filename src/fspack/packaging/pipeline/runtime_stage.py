"""runtime 准备阶段：下载/解压运行时、精简标准库、精简 standalone runtime.

四个平台分支函数 + 一个后处理精简函数（在源码编译完成后调用）：

- :func:`_prepare_runtime`：主入口，按目标平台分支
- :func:`_prepare_standalone_runtime`：Linux/macOS python-build-standalone 下载解压
- :func:`_prepare_windows_runtime`：Windows embed python 下载解压
- :func:`_slim_runtime`：编译后 strip 调试符号 + 删无用文件
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from fspack.config import standalone_cache_dir
from fspack.packaging.pyc import (
    _inject_win7_compat_dll,
    _needs_win7_compat_dll,
    _trim_standalone_runtime,
    _trim_stdlib,
)
from fspack.packaging.runtime import (
    STANDALONE_RELEASE_TAG,
    embed_dirname,
)
from fspack.packaging.runtime import (
    download_embed as _default_download_embed,
)
from fspack.packaging.runtime import (
    download_standalone as _default_download_standalone,
)
from fspack.packaging.runtime import (
    extract_embed as _default_extract_embed,
)
from fspack.packaging.runtime import (
    extract_standalone as _default_extract_standalone,
)
from fspack.platform import Platform

from .context import BuildContext

__all__ = [
    "_prepare_runtime",
    "_prepare_standalone_runtime",
    "_prepare_windows_runtime",
    "_slim_runtime",
]

_logger = logging.getLogger(__name__)

# 延迟持有 stages 模块引用，避免顶层循环 import（stages 顶层从本模块导入阶段函数）
_stages_mod_holder: list[Any] = [None]


def _S(fn_name: str, fallback_fn: Callable[..., Any]) -> Callable[..., Any]:
    """运行时从 :mod:`stages` 模块动态取 ``fn_name``，fallback 到默认实现.

    测试通过 ``monkeypatch.setattr("fspack.packaging.pipeline.stages.<name>", mock)``
    修改 stages 模块属性，此 dispatch 使阶段函数内部调用能感知到 patch。
    """
    mod = _stages_mod_holder[0]
    if mod is None:
        try:
            from fspack.packaging.pipeline import stages as _stages_mod

            mod = _stages_mod
            _stages_mod_holder[0] = mod
        except ImportError:
            return fallback_fn
    return getattr(mod, fn_name, fallback_fn)


def _prepare_runtime(ctx: BuildContext) -> Path:
    """下载/解压运行时、精简标准库，返回 site-packages 路径.

    分支：

    - Linux：下载 python-build-standalone tar.gz，解压到 ``runtime/python``
    - macOS：下载 python-build-standalone tar.gz（x86_64 或 arm64），解压到 ``runtime/python``
    - Windows：下载 embed python zip，解压到 ``runtime``

    runtime 已就绪（dll/python bin 存在）时跳过下载解压，两 stage 均 ``hit_cache``。
    """
    target = ctx.cfg.target
    if target is Platform.LINUX:
        site_packages = _prepare_standalone_runtime(ctx)
    elif target is Platform.MACOS:
        site_packages = _prepare_standalone_runtime(ctx, macos_arch=_detect_macos_arch())
    else:
        site_packages = _prepare_windows_runtime(ctx)
    site_packages.mkdir(parents=True, exist_ok=True)

    # Win7 兼容性：Python 3.9+ 官方不再支持 Win7，注入 api-ms-win-core-path-l1-1-0.dll
    # 使 embed python 3.9+ 在 Win7 SP1 / Server 2008 R2 SP1 上也能运行。
    # 仅 Windows 目标需要（Linux/macOS standalone 不存在此问题）。
    if target is Platform.WINDOWS and _needs_win7_compat_dll(ctx.info.py_version):
        _inject_win7_compat_dll(ctx.runtime_dir)

    # 标准库精简：剥离 standalone 中的 test/ensurepip/idlelib 等运行时无用模块。
    # Windows embed 标准库在 python3XX.zip 内（官方已精简），阶段内自动跳过。
    if not ctx.opts.no_stdlib_trim:
        with ctx.tracker.stage("精简标准库") as st:
            _trim_stdlib(ctx.runtime_dir, ctx.info.py_version, target, st)

    return site_packages


def _slim_runtime(ctx: BuildContext, has_tkinter: bool) -> None:
    """精简 standalone runtime 到运行时最小集（在 ``_compile_user_sources`` 之后调用）.

    剥离运行时无用的开发期文件，仅 Linux/macOS 目标生效（Windows embed 已精简且
    无调试符号，函数内自动跳过）。包含四类优化：

    - A. strip ``libpython3.X.so.1.0`` 调试符号（省 ~34MB）
    - B. 删 ``python/bin/python3.X`` 二进制（省 ~53MB，loader 用 dlopen 不需要它）
    - C. 删 ``python/include/`` 与 ``python/share/``（省 ~9MB）
    - D. 非 tkinter 项目剥离 Tcl/Tk 运行时（省 ~9MB）

    必须在 :func:`compile_stage._compile_user_sources` 之后调用：``_precompile_pyc``
    构建期需 ``python/bin/python3.X`` 跑 ``compileall``，构建完成后才能删。
    ``--no-slim-runtime`` 关闭此阶段（``BuildOptions.no_slim_runtime=True``）。

    Args:
        has_tkinter: 项目是否使用 tkinter（True 保留 Tcl/Tk，False 剥离）
    """
    target = ctx.cfg.target
    if ctx.opts.no_slim_runtime:
        _logger.info("no_slim_runtime=True，跳过 runtime 精简")
        return
    with ctx.tracker.stage("精简 runtime") as st:
        _trim_standalone_runtime(
            ctx.runtime_dir,
            ctx.info.py_version,
            target,
            st,
            has_tkinter=has_tkinter,
        )


def _detect_macos_arch() -> str:
    """检测 macOS 目标架构：host 为 macOS 时用本机架构，否则默认 x86_64（CI 常见）."""
    import platform as _platform

    machine = _platform.machine()
    return "arm64" if machine == "arm64" else "x86_64"


def _prepare_standalone_runtime(ctx: BuildContext, *, macos_arch: str | None = None) -> Path:
    """下载并解压 python-build-standalone 到 runtime_dir（Linux 与 macOS 目标）.

    Args:
        macos_arch: macOS 架构（``"x86_64"`` 或 ``"arm64"``），None 表示 Linux。
    """
    download_standalone = _S("download_standalone", _default_download_standalone)
    extract_standalone = _S("extract_standalone", _default_extract_standalone)
    major, minor = ctx.info.py_version.split(".")[:2]
    python_bin = ctx.runtime_dir / "python" / "bin" / f"python{major}.{minor}"
    runtime_ready = python_bin.is_file()
    standalone_cache = standalone_cache_dir()
    tar_path = None
    with ctx.tracker.stage("下载运行时") as st:
        if runtime_ready:
            st.hit_cache()
            st.set_detail("runtime 已就绪")
        else:
            tar_path = download_standalone(
                ctx.info.py_version,
                STANDALONE_RELEASE_TAG,
                standalone_cache,
                stage=st,
                macos_arch=macos_arch,
            )
            st.set_detail("python-build-standalone")
    with ctx.tracker.stage("解压运行时") as st:
        if runtime_ready:
            st.hit_cache()
            st.set_detail("runtime 已就绪")
        else:
            assert tar_path is not None
            extract_standalone(tar_path, ctx.runtime_dir)
            st.processed(1)
            st.set_detail("python-build-standalone")
    return ctx.cfg.dist_dir / "site-packages"


def _prepare_windows_runtime(ctx: BuildContext) -> Path:
    """下载并解压 embed python 到 runtime_dir（Windows 目标）."""
    download_embed = _S("download_embed", _default_download_embed)
    extract_embed = _S("extract_embed", _default_extract_embed)
    dll_marker = ctx.runtime_dir / f"{embed_dirname(ctx.info.py_version)}.dll"
    runtime_ready = dll_marker.is_file()
    zip_path = None
    with ctx.tracker.stage("下载运行时") as st:
        if runtime_ready:
            st.hit_cache()
            st.set_detail("runtime 已就绪")
        else:
            zip_path = download_embed(ctx.info.py_version, ctx.cfg.mirror, ctx.cfg.embed_cache_dir, stage=st)
            st.set_detail("embed python")
    with ctx.tracker.stage("解压运行时") as st:
        if runtime_ready:
            st.hit_cache()
            st.set_detail("runtime 已就绪")
        else:
            assert zip_path is not None
            extract_embed(zip_path, ctx.runtime_dir)
            st.processed(1)
            st.set_detail("embed python")
    return ctx.cfg.dist_dir / "site-packages"
