"""构建阶段函数：runtime 准备 → 依赖分析 → 依赖下载 → 源码编译 → loader 生成.

从 :mod:`fspack.packaging.pipeline` 拆分而来，封装各构建阶段的执行逻辑。
``pipeline.py`` 保留 ``build``/``_execute_build`` 入口与 ``BuildContext`` 共享
上下文，本模块提供各阶段函数实现。

依赖 :mod:`fspack.packaging.pipeline` 提供 ``BuildContext``/``fspack_wheel_cache_dir``
/``default_icon_path``/``_DEFAULT_ICON``（顶层导入避免循环依赖：``pipeline`` 顶层
导入本模块，本模块不能顶层导入 ``pipeline``）。

阶段函数通过 ``BuildContext`` 聚合参数，避免重复传递 6-8 个参数。目标平台通过
``ctx.cfg.target`` 访问。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from fspack.config import (
    DEFAULT_SLIM_RULES,
    AppType,
    BuildConfig,
    BuildOptions,
    DependencyReport,
    EntryPoint,
    ProjectInfo,
    SlimRules,
    cache_root,
    nuitka_cache_dir,
    standalone_cache_dir,
    wheel_cache_dir,
)
from fspack.packaging.builtin import TkinterBundler
from fspack.packaging.entry import EntryWrapper
from fspack.packaging.icon import ensure_ico, find_favicon
from fspack.packaging.loader import compile_loader, generate_loader_source
from fspack.packaging.pyc import (
    _inject_win7_compat_dll,
    _needs_win7_compat_dll,
    _precompile_pyc,
    _trim_standalone_runtime,
    _trim_stdlib,
)
from fspack.packaging.runtime import (
    STANDALONE_RELEASE_TAG,
    download_embed,
    download_standalone,
    embed_dirname,
    extract_embed,
    extract_standalone,
)
from fspack.packaging.site_packages import normalize_pkg_name as _normalize_pkg_name
from fspack.packaging.wheels import download_wheels
from fspack.platform import Platform, detect_platform, wheel_platform_tags

if TYPE_CHECKING:
    # BuildTracker / StageRecorder 仅用于类型注解（``from __future__ import
    # annotations`` 使注解不在运行时求值），顶部不导入 fspack.progress 避免连锁
    # 触发 rich.progress/rich.table 加载（省 ~12ms），仅在实际构建时才加载。
    from fspack.progress import BuildTracker, StageRecorder

__all__ = [
    "BuildContext",
    "_analyze_binary_dependencies",
    "_analyze_dependencies",
    "_build_entry_loaders",
    "_compile_user_sources",
    "_dep_cache_load",
    "_dep_cache_path",
    "_dep_cache_save",
    "_download_dependencies",
    "_prepare_runtime",
    "_prepare_standalone_runtime",
    "_prepare_windows_runtime",
    "_resolve_project_icon",
    "_site_packages_has_deps",
    "_slim_runtime",
    "_strip_version_specifier",
    "default_icon_path",
    "fspack_wheel_cache_dir",
]

_logger = logging.getLogger(__name__)

# 默认 icon：打包在 fspack 包内，随 wheel 分发
# stages.py 在 src/fspack/packaging/pipeline/ 下，parent.parent.parent 即 src/fspack/
_DEFAULT_ICON = Path(__file__).parent.parent.parent / "assets" / "icons" / "app.ico"

# 多入口 loader 并行编译上限（iter-133）：subprocess 释放 GIL，线程足够并行。
# 4 上限平衡并行收益与 Windows 资源限制（mingw/gcc 子进程句柄/内存），
# 与 _MAX_COMPILE_WORKERS（nuitka 模块）保持一致。
_MAX_LOADER_WORKERS = 4


@dataclass(frozen=True)
class BuildContext:
    """构建流水线共享上下文，聚合阶段函数共用的构建配置与状态.

    避免 :func:`_prepare_runtime`/:func:`_analyze_dependencies`/
    :func:`_download_dependencies`/:func:`_compile_user_sources`/
    :func:`_build_entry_loaders` 等阶段函数重复接收 6-8 个参数。
    目标平台通过 :attr:`cfg.target` 访问，无需单独字段。
    """

    tracker: BuildTracker
    info: ProjectInfo
    cfg: BuildConfig
    opts: BuildOptions
    runtime_dir: Path


def default_icon_path() -> Path:
    """返回 fspack 自带的默认 icon 路径（``assets/icons/app.ico``）."""
    return _DEFAULT_ICON


def fspack_wheel_cache_dir() -> Path:
    """返回 fspack wheel 缓存目录（``FSPACK_CACHE_DIR`` 环境变量 > 默认 ``~/.fspack/cache/wheels``）."""
    return wheel_cache_dir()


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

    必须在 :func:`_compile_user_sources` 之后调用：``_precompile_pyc`` 构建期需
    ``python/bin/python3.X`` 跑 ``compileall``，构建完成后才能删。
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
    major, minor = ctx.info.py_version.split(".")[:2]
    python_bin = ctx.runtime_dir / "python" / "bin" / f"python{major}.{minor}"
    runtime_ready = python_bin.is_file()
    standalone_cache = standalone_cache_dir()
    tar_path: Path | None = None
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
    dll_marker = ctx.runtime_dir / f"{embed_dirname(ctx.info.py_version)}.dll"
    runtime_ready = dll_marker.is_file()
    zip_path: Path | None = None
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


def _analyze_dependencies(ctx: BuildContext, *, save_cache: bool = True) -> DependencyReport:
    """分析依赖（源码指纹缓存命中则跳过 AST 扫描）.

    ``save_cache=False`` 时跳过缓存写入（用于 ``--dry-run`` 模式，避免创建
    ``dist/.dep_cache.json`` 触发 dist 目录创建）。

    extras 依赖合并：``ctx.opts.extras`` 指定的 ``[project.optional-dependencies]``
    分组经 :func:`fspack.config.expand_extras` 展开后与 ``ctx.info.dependencies``
    合并，作为 ``declared`` 传入依赖分析。自引用 ``"my-pkg[extra]"`` 递归展开，
    第三方 ``"pkg[extra]"`` 原样保留交给 pip。缓存键含 declared，extras 变化时
    缓存自动失效。
    """
    project_dir = ctx.cfg.project_dir
    with ctx.tracker.stage("分析依赖") as st:
        # 源码指纹缓存：源码未变时跳过 AST 分析，重复构建加速 ~478ms
        from fspack.analyzer import source_fingerprint
        from fspack.config import expand_extras

        # 合并 base deps 与 enabled extras（展开自引用）
        expanded_deps = expand_extras(
            ctx.info.dependencies,
            ctx.info.optional_dependencies,
            ctx.opts.extras,
            ctx.info.name,
        )
        fingerprint = source_fingerprint(project_dir, ctx.info.data_dirs)
        report = _dep_cache_load(ctx.cfg.dist_dir, fingerprint, expanded_deps)
        if report is not None:
            st.hit_cache()
            ast_count = len(report.ast_third_party)
            st.set_detail(f"缓存命中，AST {ast_count} 个第三方")
        else:
            report = DependencyReport.from_src(project_dir, ctx.info.name, expanded_deps, ctx.info.data_dirs)
            if save_cache:
                _dep_cache_save(ctx.cfg.dist_dir, fingerprint, report)
            if report.missing:
                _logger.info("AST 发现未声明依赖: %s", ", ".join(report.missing))
            ast_count = len(report.ast_third_party)
            st.processed(ast_count)
            st.set_detail(f"AST {ast_count} 个第三方")
    return report


def _download_dependencies(ctx: BuildContext, site_packages: Path, report: DependencyReport) -> bool:
    """下载并解压第三方依赖 wheel 到 site-packages，返回是否补充了 tkinter.

    补充内置库 tkinter（embed python 缺失，AST 检测到使用时从 python-build-standalone 提取）。
    下载用包名优先 declared（PyPI 包名权威），declared 为空时回退 ast_third_party。
    """
    target = ctx.cfg.target
    # 补充内置库：embed python 缺失 tkinter（纯 Python 包 + _tkinter.pyd + Tcl/Tk 脚本），
    # 若 AST 检测到 tkinter 使用则从 python-build-standalone Windows 构建提取并补充到 runtime。
    # Linux standalone 已含全部 stdlib，无需补充。
    has_tkinter = False
    if TkinterBundler.is_needed(report.ast_stdlib, target):
        builtin_cache = cache_root()
        with ctx.tracker.stage("补充内置库") as st:
            TkinterBundler.ensure(ctx.runtime_dir, ctx.info.py_version, builtin_cache, stage=st)
            has_tkinter = True
            st.set_detail("tkinter")

    # 下载用包名：优先 declared（pyproject.toml 声明的 PyPI 包名，权威），
    # declared 为空时回退到 ast_third_party（AST 扫描的导入名，best effort）。
    # 原因：导入名 ≠ PyPI 包名时（如 orderedset → ordered-set），用导入名 pip download 会失败。
    # declared 非空时以声明为准，未声明的依赖通过 report.missing 日志提示用户补充。
    packages_to_download: tuple[str, ...] = report.declared if report.declared else report.ast_third_party

    if packages_to_download:
        if _site_packages_has_deps(site_packages, packages_to_download):
            with ctx.tracker.stage("下载依赖") as st:
                _logger.info("site-packages 已有依赖，跳过下载解压")
                st.skip(len(packages_to_download))
                st.set_detail("已存在跳过")
        else:
            wheel_cache = fspack_wheel_cache_dir()
            with ctx.tracker.stage("下载依赖") as st:
                wheels = download_wheels(
                    packages_to_download,
                    ctx.info.py_version,
                    ctx.cfg.mirror.pypi_index,
                    wheel_cache,
                    platform_tags=wheel_platform_tags(target),
                    stage=st,
                    extra_index_urls=ctx.info.extra_index_urls,
                    find_links=ctx.info.find_links,
                )
            with ctx.tracker.stage("解压 wheel(精简)") as st:
                unpack_wheels(
                    wheels,
                    site_packages,
                    report.ast_submodules,
                    ctx.opts.keep_modules,
                    slim_rules=ctx.info.slim_rules,
                    stage=st,
                )
    else:
        _logger.info("无第三方依赖，跳过 wheel 下载")
    return has_tkinter


def _compile_user_sources(ctx: BuildContext, src_dst: Path) -> None:
    """编译用户源码：Nuitka 编译（可选）+ 字节码预编译.

    Nuitka 编译模式：用 runtime python -c "sys.path.insert(0, <nuitka_cache>); ..." 调用
    nuitka --module 将 dist/src 下用户源码编译为 .pyd。
    用户源码以 .pyd 形式本机执行，速度提升 30-50%（参考 RimSort Nuitka 打包方案）。
    仅编译用户源码（src/），第三方依赖（site-packages/）保持 wheel 解压 + .pyc。
    交叉构建跳过（Nuitka 无法生成目标平台 .pyd）。
    nuitka 装到本地缓存 ~/.fspack/cache/nuitka/<py_version>/，不污染 dist/runtime；
    编译时用 -c 注入 sys.path 绕过 _pth 对 PYTHONPATH 的限制。
    stamp 命中跳过整个阶段（含 ensure_env 与 compile_src）。
    入口文件跳过编译与剥离：入口包装器用 ``runpy.run_module``/``run_path`` 调用
    用户代码，需 ``.py`` 存在才能被 ``find_spec`` 定位（``.pyd`` 无字节码无法被
    ``runpy`` 执行，``__pycache__`` 下的 ``.pyc`` 不在 ``FileFinder`` 搜索范围）。

    ``[tool.fspack] data-dirs`` 配置的数据资源目录树（``ctx.info.data_dirs``）
    与 ``web-static-dirs`` 配置的前端构建产物目录（``ctx.info.web_static_dirs``）
    传递给 ``_precompile_pyc``，其下 ``.py`` 不被剥离：这些目录视为完整资源原样
    保留（如 fspack 的 ``assets/templates/`` 含项目模板源码，下游 ``fsp doctor
    --test`` 复制后需 ``.py`` 存在才能构建；前端 ``dist/`` 内含 JS 工具脚本）。
    """
    target = ctx.cfg.target
    # 入口文件相对 src 的 POSIX 路径集合：Nuitka 编译与 pyc_strip 剥离均跳过这些文件
    entry_rels = frozenset(ep.entry_rel(ctx.info.src_dir) for ep in ctx.info.all_entries)
    if ctx.opts.nuitka and target is detect_platform():
        with ctx.tracker.stage("Nuitka 编译") as st:
            from fspack.packaging.nuitka import NuitkaCompiler

            nuitka_cache_root = nuitka_cache_dir()
            NuitkaCompiler.compile_with_stamp(
                src_dst,
                ctx.cfg.dist_dir,
                ctx.runtime_dir,
                ctx.info.py_version,
                target,
                ctx.cfg.mirror,
                nuitka_cache_root,
                stage=st,
                entry_rels=entry_rels,
                ccache=ctx.opts.ccache,
                nuitka_packages=ctx.opts.nuitka_packages,
            )

    # 预编译字节码：用 runtime 自身 python 编译 src + site-packages 为 .pyc，加速首次启动。
    # pyc_strip=True 时额外剥离非 __init__.py 源码（源码保护，保留包标识避免命名空间包问题）。
    # 交叉构建时（构建机平台 ≠ 目标平台）runtime python 无法执行，跳过预编译。
    # Nuitka 模式下 src 已编译为 .pyd，compileall 会跳过（找不到 .py 不生成 .pyc），
    # site-packages 仍按 pyc_optimize 编译，故本步保留不跳过。
    # data_dirs/web_static_dirs 解析为 dist/src 下的绝对路径，传递给 _precompile_pyc
    # 跳过其下 .py 剥离。
    if not ctx.opts.no_pyc and target is detect_platform():
        with ctx.tracker.stage("预编译字节码") as st:
            # data_dirs/web_static_dirs 配置为相对项目目录的 POSIX 路径，解析为 dist/src
            # 下的绝对路径：project_dir/<rel> → dist/src/<rel>（src_dst 即 dist/src）。
            # 仅解析存在的目录，避免传不存在的路径（无副作用但增加判断开销）。
            resolved_data_dirs = tuple(
                src_dst / Path(rel) for rel in ctx.info.data_dirs if (src_dst / Path(rel)).is_dir()
            )
            resolved_web_static_dirs = tuple(
                src_dst / Path(rel) for rel in ctx.info.web_static_dirs if (src_dst / Path(rel)).is_dir()
            )
            _precompile_pyc(
                ctx.cfg.dist_dir,
                ctx.runtime_dir,
                ctx.info.py_version,
                target,
                strip_py=ctx.opts.pyc_strip,
                stage=st,
                optimize=ctx.opts.pyc_optimize,
                entry_rels=entry_rels,
                data_dirs=resolved_data_dirs,
                web_static_dirs=resolved_web_static_dirs,
            )


def _build_entry_loaders(ctx: BuildContext, resolved_icon: Path | None, has_tkinter: bool) -> list[Path]:
    """为每个入口生成 C loader 与入口包装器，返回生成的 exe 路径列表.

    用 ``tempfile.TemporaryDirectory`` 作为 loader 编译工作目录，编译完成后自动清理，
    避免 ``dist/build/`` 残留 ``loader.c``/``icon.rc``/``icon.ico``/``icon.o`` 中间文件
    被打包进发行包。loader 缓存命中路径不创建工作目录，无副作用。

    **并行编译**（iter-133）：多入口场景用 :class:`ThreadPoolExecutor` 并行编译
    每个 entry loader（mingw/gcc/clang 子进程释放 GIL，线程足够并行）。
    ``max_workers = min(cpu_count, :data:`_MAX_LOADER_WORKERS`)`` 平衡并行收益与
    Windows 资源限制。共享 ``TemporaryDirectory``，每个入口分配独立子目录
    （``<tmp>/<entry_name>``）避免 ``loader.c``/``icon.rc``/``icon.o`` 文件冲突。

    **线程安全**：``exes``/``st.processed()`` 仅在主线程（``future.result()`` 迭代）
    聚合，无共享可变状态竞争。``compile_loader`` 内部 ``stage.hit_cache()``/
    ``stage.set_detail()`` 在 worker 线程调用，``StageRecorder._hits += 1`` 在 GIL 下
    最坏丢失一次计数（benign race，不影响正确性）。

    **异常传播**：worker 内 ``compile_loader`` 抛异常（如 ``LoaderError``）时
    ``future.result()`` 重抛，``with ThreadPoolExecutor`` 的 ``__exit__`` 调
    ``shutdown(wait=True)`` 等待在途任务后传播。
    """
    target = ctx.cfg.target
    exes: list[Path] = []
    with ctx.tracker.stage("生成 C loader") as st:
        source = generate_loader_source(ctx.info.py_xy, target)
        entries = ctx.info.all_entries
        # 单入口无需并行（线程池开销无收益）
        if len(entries) <= 1:
            with tempfile.TemporaryDirectory(prefix="fspack_loader_") as tmp:
                _build_one_loader(ctx, entries[0], source, Path(tmp), resolved_icon, has_tkinter, st)
                exes.append(_loader_exe_path(ctx, entries[0], target))
            st.processed(len(exes))
            return exes

        cpu = os.cpu_count() or 1
        max_workers = min(cpu, _MAX_LOADER_WORKERS)
        _logger.info("并行编译 %d 个 entry loader（max_workers=%d）", len(entries), max_workers)
        # 临时工作目录：编译完成（或异常）后自动清理，不污染 dist/
        # 共享 TemporaryDirectory，每入口独立子目录避免 loader.c/icon.rc/icon.o 冲突
        with tempfile.TemporaryDirectory(prefix="fspack_loader_") as tmp:
            build_dir = Path(tmp)

            def _build_one(ep: EntryPoint) -> Path:
                """单入口编译 worker：生成包装器 + .entry + 编译 loader，返回 exe 路径."""
                work_subdir = build_dir / ep.name
                work_subdir.mkdir(parents=True, exist_ok=True)
                _build_one_loader(ctx, ep, source, work_subdir, resolved_icon, has_tkinter, st)
                return _loader_exe_path(ctx, ep, target)

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [pool.submit(_build_one, ep) for ep in entries]
                # 按 submit 顺序取 result，保持 exes 顺序与 entries 一致
                # future.result() 重抛 worker 异常（如 LoaderError），由 with 块 __exit__
                # 的 shutdown(wait=True) 等待在途任务后传播
                for future in futures:
                    exes.append(future.result())
        st.processed(len(exes))
    return exes


def _build_one_loader(  # noqa: PLR0913
    ctx: BuildContext,
    ep: EntryPoint,
    source: str,
    work_dir: Path,
    resolved_icon: Path | None,
    has_tkinter: bool,
    stage: StageRecorder,
) -> None:
    """为单个入口生成包装器、``.entry`` 文件并编译 loader.

    抽取自 :func:`_build_entry_loaders` 供串行与并行路径复用。``work_dir`` 由调用方
    分配（并行模式下为 ``<tmp>/<entry_name>`` 子目录，避免多入口文件冲突）。
    """
    entry_rel = ep.entry_rel(ctx.info.src_dir)
    result = EntryWrapper.dotted_module_name(ctx.info.src_dir, ep.file)
    module_dotted = result[0] if result is not None else None
    pkg_root_rel = result[1] if result is not None else "."
    # 生成入口包装器：处理 sys.path、Qt 插件路径与包上下文（相对导入），
    # WEB 类型额外注入静态文件 serve 与自动开浏览器（open_browser 默认启用）。
    # open_browser = opts.open_browser（CLI/配置显式启用）或 WEB 类型自动启用；
    # 非 WEB 类型 opts.open_browser=True 时也启用（如 GUI 内嵌 WebView 场景）。
    wrapper_name = f"_entry_{ep.name}.py"
    wrapper_path = ctx.cfg.dist_dir / wrapper_name
    open_browser = ctx.opts.open_browser or ep.app_type is AppType.WEB
    # web_static_dirs 是相对项目目录的路径（如 "frontend"），copy_source 复制到
    # dist/src/ 下，wrapper 运行时以 _DIST_DIR 为基准解析，需加 "src/" 前缀
    # 使其指向 dist/src/frontend（前端构建产物的实际位置）。
    web_static_dirs = tuple(f"src/{d}" for d in ctx.info.web_static_dirs)
    wrapper_path.write_text(
        EntryWrapper.generate_wrapper_source(
            ep.name,
            module_dotted,
            entry_rel,
            pkg_root_rel,
            has_tkinter=has_tkinter,
            lazy_imports=ctx.opts.lazy_imports,
            web_static_dirs=web_static_dirs,
            open_browser=open_browser,
        ),
        encoding="utf-8",
    )
    # .entry 指向 wrapper（loader 读 .entry 路径运行）
    if ctx.info.entries:
        # 多入口模式：每个入口写 <name>.entry
        (ctx.cfg.dist_dir / f"{ep.name}.entry").write_text(wrapper_name, encoding="utf-8")
    else:
        # 单入口模式：写 .entry（向后兼容）
        (ctx.cfg.dist_dir / ".entry").write_text(wrapper_name, encoding="utf-8")
    exe = _loader_exe_path(ctx, ep, ctx.cfg.target)
    compile_loader(source, exe, ep.app_type, work_dir, ctx.cfg.target, icon=resolved_icon, stage=stage)


def _loader_exe_path(ctx: BuildContext, ep: EntryPoint, target: Platform) -> Path:
    """返回入口对应的 loader exe 路径（Windows 加 ``.exe`` 后缀）."""
    exe_name = f"{ep.name}.exe" if target is Platform.WINDOWS else ep.name
    return ctx.cfg.dist_dir / exe_name


def _analyze_binary_dependencies(ctx: BuildContext) -> int:
    """执行二进制依赖分析，剥离 dist 内无引用的 .dll/.so/.dylib.

    仅当 ``ctx.opts.analyze_deps=True`` 时调用。流程：

    1. :func:`analyze_binary_dependencies` 扫描 dist 下所有二进制，构建依赖图
    2. :func:`find_unused_binaries` 从入口 BFS，返回不可达二进制列表
    3. :func:`strip_unused_binaries` 删除未引用文件，累加节省字节数到 stage

    工具缺失（objdump/otool 未安装）时静默跳过，不阻断构建。
    节省字节数通过 :meth:`StageRecorder.add_saved_bytes` 写入 tracker，
    在 ``BuildTracker.summary()`` 中体现为"依赖分析"阶段"节省"列。
    """
    from fspack.packaging.dep_analyzer import (
        analyze_binary_dependencies,
        find_unused_binaries,
        strip_unused_binaries,
    )

    with ctx.tracker.stage("依赖分析") as st:
        graph = analyze_binary_dependencies(ctx.cfg.dist_dir, ctx.cfg.target, runtime_dir=ctx.runtime_dir)
        if not graph.binaries:
            st.set_detail("无二进制或工具缺失，跳过")
            return 0

        unused = find_unused_binaries(graph)
        if not unused:
            st.set_detail(f"扫描 {len(graph.binaries)} 个二进制，全部可达")
            return 0

        saved = strip_unused_binaries(unused)
        st.add_saved_bytes(saved)
        st.set_detail(f"剥离 {len(unused)} 个无引用二进制")
        _logger.info("依赖分析：剥离 %d 个无引用二进制，节省 %d bytes", len(unused), saved)
        return saved


def _dep_cache_path(dist_dir: Path) -> Path:
    """依赖分析缓存文件路径：``dist/.dep_cache.json``."""
    return dist_dir / ".dep_cache.json"


def _dep_cache_load(dist_dir: Path, fingerprint: str, declared: tuple[str, ...]) -> DependencyReport | None:
    """加载依赖分析缓存，指纹或声明依赖不匹配时返回 ``None``."""
    cache = _dep_cache_path(dist_dir)
    if not cache.is_file():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("fingerprint") != fingerprint or tuple(data.get("declared", [])) != declared:
        return None
    r = data["report"]
    return DependencyReport(
        declared=tuple(r["declared"]),
        ast_third_party=tuple(r["ast_third_party"]),
        ast_stdlib=tuple(r["ast_stdlib"]),
        ast_local=tuple(r["ast_local"]),
        ast_submodules={k: frozenset(v) for k, v in r["ast_submodules"].items()},
    )


def _dep_cache_save(dist_dir: Path, fingerprint: str, report: DependencyReport) -> None:
    """保存依赖分析缓存."""
    cache = _dep_cache_path(dist_dir)
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": fingerprint,
        "declared": list(report.declared),
        "report": {
            "declared": list(report.declared),
            "ast_third_party": list(report.ast_third_party),
            "ast_stdlib": list(report.ast_stdlib),
            "ast_local": list(report.ast_local),
            "ast_submodules": {k: sorted(v) for k, v in report.ast_submodules.items()},
        },
    }
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _resolve_project_icon(
    cli_icon: Path | None,
    project_icon: Path | None,
    project_dir: Path,
    work_dir: Path,
    target: Platform,
) -> Path | None:
    """按优先级解析最终 icon 路径，非 .ico 格式自动转换。

    优先级：``cli_icon`` > ``project_icon`` > 自动搜索 ``favicon.*`` > 默认 ``app.ico``。

    - Linux 目标：始终返回 ``None``（ELF 无图标资源概念）
    - 非 ``.ico`` 格式（``.png``/``.jpg`` 等）：调用 :func:`ensure_ico` 转换，
      转换失败（如 Pillow 未安装）回退到默认 ``app.ico``
    - 默认 ``app.ico`` 是 fspack 自带资源，必定存在，无需转换

    ``work_dir`` 为图片转换的临时目录（通常是 ``dist/build``）。
    """
    if target is Platform.LINUX:
        return None

    # 选定候选 icon：CLI > 项目配置 > favicon 自动搜索
    candidate = cli_icon
    if candidate is None:
        candidate = project_icon
    if candidate is None:
        candidate = find_favicon(project_dir)
        if candidate is not None:
            _logger.info("使用 favicon 作为 icon: %s", candidate)

    # 无任何候选 → 默认 icon（.ico，无需转换）
    if candidate is None:
        return _DEFAULT_ICON

    # 转换为 .ico（.ico 原样返回，其他格式用 Pillow 转换，失败回退默认）
    resolved = ensure_ico(candidate, work_dir)
    if resolved is not None:
        return resolved
    _logger.warning("icon 转换失败，回退到默认 icon: %s", _DEFAULT_ICON)
    return _DEFAULT_ICON


def _site_packages_has_deps(site_packages: Path, packages: Sequence[str]) -> bool:
    """检查 site-packages 是否已安装全部声明依赖。

    逐个检查 ``packages`` 中的包是否有对应的 ``*.dist-info`` 目录。
    仅当全部声明依赖均已安装时返回 True，可跳过下载+解压阶段
    （需 ``fspack c`` 清理后才会重新解压）。

    不能仅检查 ``any(*.dist-info)``：python-build-standalone 预装 pip
    （含 ``pip-*.dist-info``），embed python 也会预装 pip，导致无用户依赖时
    误判为已安装。必须按声明的包名逐一匹配。
    """
    if not site_packages.is_dir():
        return False
    # 收集 site-packages 中所有已安装包的规范化名（PEP 503）
    installed: set[str] = set()
    for d in site_packages.glob("*.dist-info"):
        if not d.is_dir():
            continue
        # dist-info 目录名格式: <name>-<version>.dist-info
        stem = d.name[: -len(".dist-info")]
        # 从右侧分离 version（最后一个 - 之后的部分）
        parts = stem.rsplit("-", 1)
        pkg_name = parts[0] if len(parts) == 2 else stem
        installed.add(_normalize_pkg_name(pkg_name))

    return all(_normalize_pkg_name(_strip_version_specifier(pkg)) in installed for pkg in packages)


def _strip_version_specifier(pkg: str) -> str:
    """从依赖字符串中剥离版本 specifier，返回纯包名。

    ``pygame>=2.5.0`` → ``pygame``；``requests`` → ``requests``。
    """
    return re.split(r"[<>=!~;\[]", pkg, maxsplit=1)[0].strip()


def unpack_wheels(  # noqa: PLR0913
    wheels: Sequence[Path],
    site_packages_dir: Path,
    submodule_usage: dict[str, frozenset[str]] | None = None,
    keep_modules: set[str] | None = None,
    *,
    slim_rules: SlimRules = DEFAULT_SLIM_RULES,
    stage: StageRecorder | None = None,
) -> int:
    """将给定 wheel 列表解包到 site-packages 目录，返回解包数量。

    当提供 ``submodule_usage`` 时按子模块分析选择性解压（精简打包），
    否则全量解压。``slim_rules`` 透传给 ``slim_unpack``，作为用户自定义
    glob 规则覆盖 spec 自动分类。
    """
    from fspack.slim import slim_unpack

    return slim_unpack(
        wheels,
        site_packages_dir,
        submodule_usage,
        keep_modules,
        slim_rules=slim_rules,
        stage=stage,
    )
