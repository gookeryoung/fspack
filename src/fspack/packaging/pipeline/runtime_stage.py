"""runtime 准备阶段：下载/解压运行时、Win7 兼容处理、精简标准库.

四个平台分支函数 + Win7 dll 替换 + 一个后处理精简函数（在源码编译完成后调用）：

- :func:`_prepare_runtime`：主入口，按目标平台分支
- :func:`_prepare_standalone_runtime`：Linux/macOS python-build-standalone 下载解压
- :func:`_prepare_windows_runtime`：Windows embed python 下载解压
- :func:`_replace_win7_dll`：Windows 3.12+ 官方 runtime 组件整套替换为 win7 重编译版
- :func:`_zip_stdlib`：Linux/macOS 标准库 zip 化（编译后、精简前）
- :func:`_slim_runtime`：编译后 strip 调试符号 + 删无用文件
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from fspack.config import standalone_cache_dir, win7_dll_cache_dir
from fspack.exceptions import EmbedError
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
from fspack.packaging.runtime import (
    zip_stdlib as _default_zip_stdlib,
)
from fspack.packaging.win7.dll import ensure_win7_dll, needs_win7_dll
from fspack.platform import Platform

from .context import BuildContext

if TYPE_CHECKING:
    from fspack.progress import StageRecorder

__all__ = [
    "_flatten_python_dir",
    "_prepare_runtime",
    "_prepare_standalone_runtime",
    "_prepare_windows_runtime",
    "_prepare_windows_t_runtime",
    "_slim_runtime",
    "_zip_stdlib",
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
    - Windows 标准版：下载 embed python zip，解压到 ``runtime``
    - Windows 自由线程版（``py_version`` 末尾 ``t``）：下载 python-build-standalone
      Windows freethreaded tarball，解压后扁平化 ``python/`` 子目录到 ``runtime`` 根
      （python.org 不提供 freethreaded embed zip）

    runtime 已就绪（dll/python bin 存在）时跳过下载解压，两 stage 均 ``hit_cache``。
    """
    target = ctx.cfg.target
    if target is Platform.LINUX:
        site_packages = _prepare_standalone_runtime(ctx)
    elif target is Platform.MACOS:
        site_packages = _prepare_standalone_runtime(ctx, macos_arch=_detect_macos_arch())
    elif target is Platform.WINDOWS and ctx.info.py_version.endswith("t"):
        site_packages = _prepare_windows_t_runtime(ctx)
    else:
        site_packages = _prepare_windows_runtime(ctx)
    site_packages.mkdir(parents=True, exist_ok=True)

    # Win7 兼容性：Python 3.9+ 官方不再支持 Win7，注入 api-ms-win-core-path-l1-1-0.dll
    # 使 embed python 3.9+ 在 Win7 SP1 / Server 2008 R2 SP1 上也能运行。
    # 仅 Windows 目标需要（Linux/macOS standalone 不存在此问题）。
    # 3.12+ 官方 python3XX.dll 另含 kernel32 的 Win8+ 静态导入，shim 无法解决，
    # 须整套替换为重编译版组件（dll+pyd+exe 同源，仅换 dll 会与官方 pyd ABI
    # 混搭不兼容；清单驱动下载 + 双重校验，见 win7_dll 模块）。
    # ``--no-win7-dll``：产物仅面向 Win8+/Win10+ 时跳过全部 Win7 兼容注入，
    # 避免网络受限环境因 GitHub 下载失败阻断构建（产物不支持 Win7）。
    if not ctx.opts.no_win7_dll:
        if target is Platform.WINDOWS and needs_win7_dll(ctx.info.py_version):
            with ctx.tracker.stage("Win7 组件替换") as st:
                _replace_win7_dll(ctx, st)
        if target is Platform.WINDOWS and _needs_win7_compat_dll(ctx.info.py_version):
            _inject_win7_compat_dll(ctx.runtime_dir)

    # 标准库精简：剥离运行时无用模块。
    # Windows 标准版 embed zip 走 zip 重写（保守档默认删 pydoc_data 等文档数据，
    # slim-stdlib=aggressive 再删 xml/email/http 等大块可选模块）；
    # Windows 自由线程版（standalone 路径，Lib/ 解压）与 Linux/macOS 按目录剥离。
    if not ctx.opts.no_stdlib_trim:
        with ctx.tracker.stage("精简标准库") as st:
            _trim_stdlib(
                ctx.runtime_dir, ctx.info.py_version, target, st, aggressive=ctx.opts.slim_stdlib == "aggressive"
            )

    return site_packages


def _zip_stdlib(ctx: BuildContext) -> None:
    """Linux/macOS 标准库 zip 化（在 ``_compile_user_sources`` 之后、``_slim_runtime`` 之前调用）.

    把 standalone 标准库 ``.py`` 编译为 ``.pyc`` 打包为 ``lib/pythonXY[t].zip``，
    CPython ``getpath`` 自动检测 zip 加入 ``sys.path``，省去数百个 stdlib 目录的
    ``stat`` 遍历，冷启动提速 30-80ms。详见 :func:`fspack.packaging.runtime.stdlib_zip.zip_stdlib`。

    时序约束：必须在 ``_precompile_pyc`` 之后（不冲突，stdlib 不在 src/site-packages
    编译范围）且 ``_trim_standalone_runtime`` 删除 python 二进制之前（compileall
    需要 ``python/bin/pythonX.Y[t]``）。``--no-stdlib-zip`` 关闭（``BuildOptions.no_stdlib_zip``）。
    """
    if ctx.opts.no_stdlib_zip:
        _logger.info("no_stdlib_zip=True，跳过标准库 zip 化")
        return
    zip_stdlib_dispatch = _S("zip_stdlib", _default_zip_stdlib)
    with ctx.tracker.stage("标准库 zip 化") as st:
        zip_stdlib_dispatch(ctx.runtime_dir, ctx.info.py_version, ctx.cfg.target, st)


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
    # free-threaded build 二进制名带 t 后缀（python3.13t）
    is_t = ctx.info.py_version.endswith("t")
    base = ctx.info.py_version[:-1] if is_t else ctx.info.py_version
    major, minor = base.split(".")[:2]
    suffix = "t" if is_t else ""
    python_bin = ctx.runtime_dir / "python" / "bin" / f"python{major}.{minor}{suffix}"
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
            if tar_path is None:
                # runtime 未就绪时 download_standalone 应返回路径或抛异常，
                # 此分支仅防御（assert 在 python -O 下会被剥离）
                raise EmbedError("下载运行时未返回 python-build-standalone tar.gz 路径")
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
            if zip_path is None:
                # runtime 未就绪时 download_embed 应返回路径或抛异常，
                # 此分支仅防御（assert 在 python -O 下会被剥离）
                raise EmbedError("下载运行时未返回 embed python zip 路径")
            extract_embed(zip_path, ctx.runtime_dir)
            st.processed(1)
            st.set_detail("embed python")
    return ctx.cfg.dist_dir / "site-packages"


def _flatten_python_dir(runtime_dir: Path) -> None:
    """将 ``runtime_dir/python/`` 子目录所有内容上移到 ``runtime_dir`` 根.

    python-build-standalone tarball 解压后顶层是 ``python/`` 子目录，含
    ``python.exe``/``python3XX.dll``/``Lib/``/``DLLs/`` 等。Windows loader
    与 pth 文件预期 DLL 在 ``runtime_dir`` 根（与 embed zip 结构一致），
    故需扁平化：把 ``python/*`` 全部移到 ``runtime_dir/``，再删空的 ``python/``。

    幂等：``python/`` 不存在时直接返回（缓存命中场景）。
    """
    python_subdir = runtime_dir / "python"
    if not python_subdir.is_dir():
        return
    for entry in python_subdir.iterdir():
        dest = runtime_dir / entry.name
        if dest.exists():
            # 同名条目已存在于 runtime_dir 根（如重复构建残留），先清理
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
            else:
                dest.unlink(missing_ok=True)
        shutil.move(str(entry), str(dest))
    # 删除空的 python/ 子目录（ignore_errors 容忍 Windows 上偶发的句柄占用）
    shutil.rmtree(python_subdir, ignore_errors=True)


def _prepare_windows_t_runtime(ctx: BuildContext) -> Path:
    """下载 python-build-standalone freethreaded Windows tarball 并扁平化到 runtime_dir.

    python.org 不提供 freethreaded embed zip（``python-3.X.Yt-embed-amd64.zip``
    不存在），Windows 自由线程版本必须改用 astral-sh python-build-standalone 的
    ``-freethreaded-install_only`` tarball。该 tarball 解压后顶层为 ``python/``
    子目录，扁平化后 DLL/exe/Lib 移到 ``runtime_dir`` 根，与 embed 结构一致，
    使下游 loader（``runtime\\python3XXt.dll``）与 pth 文件能直接定位。

    runtime 已就绪（``python3XXt.dll`` 存在）时跳过下载解压，两 stage 均
    ``hit_cache``。
    """
    download_standalone = _S("download_standalone", _default_download_standalone)
    extract_standalone = _S("extract_standalone", _default_extract_standalone)
    dll_marker = ctx.runtime_dir / f"{embed_dirname(ctx.info.py_version)}.dll"
    runtime_ready = dll_marker.is_file()
    tar_path = None
    with ctx.tracker.stage("下载运行时") as st:
        if runtime_ready:
            st.hit_cache()
            st.set_detail("runtime 已就绪")
        else:
            tar_path = download_standalone(
                ctx.info.py_version,
                STANDALONE_RELEASE_TAG,
                standalone_cache_dir(),
                stage=st,
                windows=True,
            )
            st.set_detail("python-build-standalone freethreaded")
    with ctx.tracker.stage("解压运行时") as st:
        if runtime_ready:
            st.hit_cache()
            st.set_detail("runtime 已就绪")
        else:
            if tar_path is None:
                # runtime 未就绪时 download_standalone 应返回路径或抛异常，
                # 此分支仅防御（assert 在 python -O 下会被剥离）
                raise EmbedError("下载运行时未返回 python-build-standalone tar.gz 路径")
            extract_standalone(tar_path, ctx.runtime_dir)
            _flatten_python_dir(ctx.runtime_dir)
            st.processed(1)
            st.set_detail("python-build-standalone freethreaded")
    return ctx.cfg.dist_dir / "site-packages"


def _replace_win7_dll(ctx: BuildContext, st: StageRecorder) -> None:
    """将 runtime 官方组件整套替换为 win7 重编译版（3.12+ 目标）.

    官方 dll 含 kernel32 的 Win8+ 静态导入，loader 在 Win7 上直接拒绝加载，
    shim 无法解决；且重编译版与官方 embed 工具链不同，仅换 dll 会与官方
    ``_ctypes.pyd`` 等组件 ABI 混搭不兼容（``import ctypes`` 即访问冲突）。
    此处在官方 embed 解压后按清单下载 win7 embed zip **全量提取覆盖** runtime，
    保证 dll/pyd/exe/运行时 DLL 同源。zip 缓存命中时仅本地提取 + 导入表校验。
    ``replace_invalid=True``：官方 dll 校验必然失败，静默替换而非报错。
    """
    ensure_win7_dll(
        ctx.info.py_version,
        win7_dll_cache_dir(),
        ctx.runtime_dir,
        stage=st,
        replace_invalid=True,
    )
    st.set_detail(f"win7 重编译版 {ctx.info.py_version}")
