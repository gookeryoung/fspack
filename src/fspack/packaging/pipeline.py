"""构建流水线编排：阶段函数（解析 → runtime → 依赖 → 源码 → loader）.

本模块从 :mod:`fspack.builder` 抽离，含构建阶段编排与依赖缓存逻辑。
``builder.py`` 通过 re-export 保持公开 API 不变。

依赖 :mod:`fspack.packaging.sync` 提供 ``copy_source``，
:mod:`fspack.packaging.pyc` 提供 ``_trim_stdlib``/``_inject_win7_compat_dll``/
``_needs_win7_compat_dll``/``_precompile_pyc``。
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from fspack.config import (
    DEFAULT_LINUX_PY_VERSION,
    DEFAULT_PY_VERSION,
    DEFAULT_SLIM_RULES,
    BuildConfig,
    BuildOptions,
    DependencyReport,
    MirrorConfig,
    ProjectInfo,
    SlimRules,
    cache_root,
    embed_cache_dir,
    nuitka_cache_dir,
    resolve_py_version,
    standalone_cache_dir,
    wheel_cache_dir,
)
from fspack.console import console
from fspack.packaging.builtin import TkinterBundler
from fspack.packaging.entry import EntryWrapper
from fspack.packaging.icon import ensure_ico, find_favicon
from fspack.packaging.loader import compile_loader, generate_loader_source
from fspack.packaging.log_file import LogFormat, setup_log_file, teardown_log_file
from fspack.packaging.pyc import (
    _inject_win7_compat_dll,
    _needs_win7_compat_dll,
    _precompile_pyc,
    _trim_stdlib,
)
from fspack.packaging.runtime import (
    STANDALONE_RELEASE_TAG,
    download_embed,
    download_standalone,
    embed_dirname,
    extract_embed,
    extract_standalone,
    write_pth,
)
from fspack.packaging.sync import copy_source
from fspack.packaging.wheels import download_wheels
from fspack.platform import Platform, detect_platform, wheel_platform_tags
from fspack.progress import BuildTracker, StageRecorder, spinner

__all__ = [
    "BuildContext",
    "build",
    "clean_dist",
    "default_icon_path",
    "fspack_wheel_cache_dir",
    "resolve_project_info",
    "unpack_wheels",
]

_logger = logging.getLogger(__name__)

# 默认 icon：打包在 fspack 包内，随 wheel 分发
# pipeline.py 在 src/fspack/packaging/ 下，parent.parent 即 src/fspack/
_DEFAULT_ICON = Path(__file__).parent.parent / "assets" / "icons" / "app.ico"


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


def resolve_project_info(project_dir: Path, py_version: str | None, target: Platform) -> ProjectInfo:
    """解析项目信息并自动选择 Python 版本，返回带已解析版本的 ``ProjectInfo``.

    优先级见 :func:`resolve_py_version`：``--py-version`` > ``.python-version``
    > ``requires-python`` > 平台默认。用于 :func:`build` 与安装包编排共享版本
    解析逻辑，确保 ``no_build`` 模式下发行包文件名也使用正确的 Python 版本。
    """
    info = ProjectInfo.from_dir(project_dir, py_version)
    default_ver = DEFAULT_LINUX_PY_VERSION if target is Platform.LINUX else DEFAULT_PY_VERSION
    resolved = resolve_py_version(project_dir, py_version, info.requires_python, default_ver, target)
    if resolved != info.py_version:
        _logger.info("自动选择 Python 版本: %s", resolved)
        info = replace(info, py_version=resolved)
    return info


def build(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    dist_dir: Path | None = None,
    embed_cache: Path | None = None,
    target: Platform | None = None,
    options: BuildOptions | None = None,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
    dry_run: bool = False,
    log_file: Path | None = None,
    log_format: LogFormat = LogFormat.TEXT,
) -> ProjectInfo:
    """执行完整构建流水线，返回项目信息。

    构建行为开关（``keep_modules``/``icon``/``no_stdlib_trim``/``no_pyc``/
    ``pyc_strip``/``pyc_optimize``/``no_site``/``nuitka``）封装在 ``options``
    中，详见 :class:`fspack.config.BuildOptions`。``options=None`` 等价于
    全部开关取默认值（启用精简与预编译，关闭 Nuitka）。

    icon 优先级：``options.icon`` > 项目 ``[tool.fspack] icon`` > 自动搜索
    ``favicon.*`` > 默认 ``assets/icons/app.ico``。非 ``.ico`` 格式（如
    ``.png``/``.jpg``）通过 Pillow 转换为 ``.ico``（需安装 ``fspack[image]``），
    转换失败回退到默认 icon。仅 Windows 目标生效，Linux 忽略（ELF 无图标资源概念）。

    ``options.nuitka=True`` 时启用 Nuitka 编译模式：用 ``python -m nuitka --module``
    将 ``dist/src`` 下用户源码编译为 ``.pyd``，运行时本机执行，速度提升 30-50%。
    默认关闭。Nuitka 模式下 ``pyc_optimize`` 与 ``pyc_strip`` 仍生效于
    site-packages（第三方依赖保持 .pyc），用户源码以 .pyd 替代 .pyc。
    交叉构建时（构建机平台 ≠ 目标平台）Nuitka 跳过（无法生成目标平台 .pyd）。

    ``extra_index_urls``/``find_links`` 为 CLI 指定的私有包源，与
    ``[tool.fspack] extra-index-urls``/``find-links`` 合并（CLI 追加在配置之后，
    去重保留首次出现），透传给 pip/uv 用于私有 PyPI 服务器或本地 wheel 目录下载。

    ``dry_run=True`` 时仅执行项目解析与依赖分析，打印打包计划后返回，
    不执行下载/编译/复制等任何写操作。用于打包前确认配置正确。

    ``log_file`` 指定时将构建日志写入文件（含时间戳、级别、logger 名、消息、
    异常栈），便于 CI 上传与问题排查。``log_format`` 控制 格式：``TEXT``（默认，
    人类可读）或 ``JSON``（结构化，便于 ELK/Loki 采集）。日志文件在构建开始时
    创建、结束时自动关闭，即使构建异常也会正确清理（``try/finally``）。
    """
    opts = options or BuildOptions()
    tracker = BuildTracker()
    project_dir = Path(project_dir).resolve()
    target = target or detect_platform()
    dist = dist_dir or project_dir / "dist"
    cache = embed_cache or embed_cache_dir()
    cfg = BuildConfig(project_dir=project_dir, dist_dir=dist, embed_cache_dir=cache, mirror=mirror, target=target)

    log_wrapper = setup_log_file(Path(log_file), log_format) if log_file is not None else None
    try:
        info = _execute_build(
            tracker,
            project_dir,
            py_version,
            target,
            cfg,
            opts,
            extra_index_urls,
            find_links,
            dry_run,
        )
    finally:
        teardown_log_file(log_wrapper)
    return info


def _execute_build(  # noqa: PLR0913
    tracker: BuildTracker,
    project_dir: Path,
    py_version: str | None,
    target: Platform,
    cfg: BuildConfig,
    opts: BuildOptions,
    extra_index_urls: Sequence[str],
    find_links: Sequence[str],
    dry_run: bool,
) -> ProjectInfo:
    """执行构建流水线主体（不含日志文件 setup/teardown）.

    由 :func:`build` 调用，分离日志文件生命周期管理（``try/finally``）与
    构建逻辑，便于阅读与维护。
    """
    with tracker.stage("解析项目") as st:
        info = resolve_project_info(project_dir, py_version, target)
        # 合并 CLI 私有包源到 info（CLI 追加在配置之后，去重保留首次出现）
        merged_extra = tuple(dict.fromkeys((*info.extra_index_urls, *extra_index_urls)))
        merged_links = tuple(dict.fromkeys((*info.find_links, *find_links)))
        if merged_extra != info.extra_index_urls or merged_links != info.find_links:
            info = replace(info, extra_index_urls=merged_extra, find_links=merged_links)
        _logger.info("项目: %s %s (%s) 目标: %s", info.name, info.version, info.app_type.value, target.value)
        st.set_detail(f"{info.name} {info.version} ({info.app_type.value})")

    runtime_dir = cfg.dist_dir / "runtime"
    ctx = BuildContext(tracker=tracker, info=info, cfg=cfg, opts=opts, runtime_dir=runtime_dir)

    # dry-run 模式：仅解析项目 + 分析依赖，打印计划后返回
    if dry_run:
        report = _analyze_dependencies(ctx, save_cache=False)
        _print_build_plan(ctx, report)
        return info

    site_packages = _prepare_runtime(ctx)
    report = _analyze_dependencies(ctx)
    has_tkinter = _download_dependencies(ctx, site_packages, report)

    if target is Platform.WINDOWS:
        # tkinter 补充到 runtime/Lib/tkinter/，需将 Lib 加入 _pth 使其可被 import
        # （_pth 默认只含 Lib\site-packages，不含 Lib 本身）
        extra_pth_paths = ("Lib",) if has_tkinter else ()
        write_pth(cfg.dist_dir, info.py_version, extra_paths=extra_pth_paths, enable_site=not opts.no_site)

    with tracker.stage("复制源码") as st:
        src_dst = cfg.dist_dir / "src"
        with spinner(f"复制 {info.name} 源码"):
            copy_source(project_dir, src_dst, extra_excludes=info.exclude_dirs)

    _compile_user_sources(ctx, src_dst)

    # icon 优先级：CLI --icon > 项目 [tool.fspack] icon > 自动搜索 favicon.* > 默认 app.ico（仅 Windows）
    # Linux 目标无图标资源概念，统一传 None
    with tracker.stage("解析图标") as st:
        resolved_icon = _resolve_project_icon(opts.icon, info.icon, project_dir, cfg.dist_dir / "build", target)
        if resolved_icon is not None:
            st.set_detail(str(resolved_icon.name))

    exes = _build_entry_loaders(ctx, resolved_icon, has_tkinter)

    console.rich.print(tracker.summary())
    if not opts.no_size_report:
        from fspack.packaging.size_report import print_size_report

        print_size_report(cfg.dist_dir)
    if len(exes) == 1:
        console.success(f"构建完成: {exes[0]}")
    else:
        console.success(f"构建完成: {len(exes)} 个入口")
        for exe in exes:
            console.rich.print(f"  - {exe}")
    return info


def _prepare_runtime(ctx: BuildContext) -> Path:
    """下载/解压运行时、精简标准库，返回 site-packages 路径.

    分支：

    - Linux：下载 python-build-standalone tar.gz，解压到 ``runtime/python``
    - Windows：下载 embed python zip，解压到 ``runtime``

    runtime 已就绪（dll/python bin 存在）时跳过下载解压，两 stage 均 ``hit_cache``。
    """
    target = ctx.cfg.target
    if target is Platform.LINUX:
        site_packages = _prepare_linux_runtime(ctx)
    else:
        site_packages = _prepare_windows_runtime(ctx)
    site_packages.mkdir(parents=True, exist_ok=True)

    # Win7 兼容性：Python 3.9+ 官方不再支持 Win7，注入 api-ms-win-core-path-l1-1-0.dll
    # 使 embed python 3.9+ 在 Win7 SP1 / Server 2008 R2 SP1 上也能运行。
    # 仅 Windows 目标需要（Linux standalone 不存在此问题）。
    if target is Platform.WINDOWS and _needs_win7_compat_dll(ctx.info.py_version):
        _inject_win7_compat_dll(ctx.runtime_dir)

    # MinGW 运行时 DLL：loader.exe 与 Nuitka 编译的 .pyd 动态链接 libgcc_s_seh-1.dll
    # / libwinpthread-1.dll / libstdc++-6.dll。这些 DLL 不随 Windows 分发，需注入到
    # dist/runtime/ 使 Python 加载 .pyd 时能找到（DLL 搜索路径含 runtime/，由
    # loader.exe 的 SetDllDirectoryW 设置）。仅 Windows 目标需要（Linux 用系统 glibc）。
    if target is Platform.WINDOWS:
        from fspack.packaging.loader import inject_mingw_runtime_dlls

        inject_mingw_runtime_dlls(ctx.runtime_dir)

    # 标准库精简：剥离 Linux standalone 中 test/ensurepip/idlelib 等运行时无用模块。
    # Windows embed 标准库在 python3XX.zip 内（官方已精简），阶段内自动跳过。
    if not ctx.opts.no_stdlib_trim:
        with ctx.tracker.stage("精简标准库") as st:
            _trim_stdlib(ctx.runtime_dir, ctx.info.py_version, target, st)

    return site_packages


def _prepare_linux_runtime(ctx: BuildContext) -> Path:
    """下载并解压 python-build-standalone 到 runtime_dir（Linux 目标）."""
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
            tar_path = download_standalone(ctx.info.py_version, STANDALONE_RELEASE_TAG, standalone_cache, stage=st)
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
    return ctx.runtime_dir / "python" / "lib" / f"python{major}.{minor}" / "site-packages"


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
    return ctx.runtime_dir / "Lib" / "site-packages"


def _analyze_dependencies(ctx: BuildContext, *, save_cache: bool = True) -> DependencyReport:
    """分析依赖（源码指纹缓存命中则跳过 AST 扫描）.

    ``save_cache=False`` 时跳过缓存写入（用于 ``--dry-run`` 模式，避免创建
    ``dist/.dep_cache.json`` 触发 dist 目录创建）。
    """
    project_dir = ctx.cfg.project_dir
    with ctx.tracker.stage("分析依赖") as st:
        # 源码指纹缓存：源码未变时跳过 AST 分析，重复构建加速 ~478ms
        from fspack.analyzer import source_fingerprint

        fingerprint = source_fingerprint(project_dir)
        report = _dep_cache_load(ctx.cfg.dist_dir, fingerprint, ctx.info.dependencies)
        if report is not None:
            st.hit_cache()
            ast_count = len(report.ast_third_party)
            st.set_detail(f"缓存命中，AST {ast_count} 个第三方")
        else:
            report = DependencyReport.from_src(project_dir, ctx.info.name, ctx.info.dependencies)
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
    if not ctx.opts.no_pyc and target is detect_platform():
        with ctx.tracker.stage("预编译字节码") as st:
            _precompile_pyc(
                ctx.cfg.dist_dir,
                ctx.runtime_dir,
                ctx.info.py_version,
                target,
                strip_py=ctx.opts.pyc_strip,
                stage=st,
                optimize=ctx.opts.pyc_optimize,
                entry_rels=entry_rels,
            )


def _build_entry_loaders(ctx: BuildContext, resolved_icon: Path | None, has_tkinter: bool) -> list[Path]:
    """为每个入口生成 C loader 与入口包装器，返回生成的 exe 路径列表.

    用 ``tempfile.TemporaryDirectory`` 作为 loader 编译工作目录，编译完成后自动清理，
    避免 ``dist/build/`` 残留 ``loader.c``/``icon.rc``/``icon.ico``/``icon.o`` 中间文件
    被打包进发行包。loader 缓存命中路径不创建工作目录，无副作用。
    """
    target = ctx.cfg.target
    exes: list[Path] = []
    with ctx.tracker.stage("生成 C loader") as st:
        source = generate_loader_source(ctx.info.py_xy, target)
        # 临时工作目录：编译完成（或异常）后自动清理，不污染 dist/
        with tempfile.TemporaryDirectory(prefix="fspack_loader_") as tmp:
            build_dir = Path(tmp)
            for ep in ctx.info.all_entries:
                entry_rel = ep.entry_rel(ctx.info.src_dir)
                result = EntryWrapper.dotted_module_name(ctx.info.src_dir, ep.file)
                module_dotted = result[0] if result is not None else None
                pkg_root_rel = result[1] if result is not None else "."
                # 生成入口包装器：处理 sys.path、Qt 插件路径与包上下文（相对导入）
                wrapper_name = f"_entry_{ep.name}.py"
                wrapper_path = ctx.cfg.dist_dir / wrapper_name
                wrapper_path.write_text(
                    EntryWrapper.generate_wrapper_source(
                        ep.name, module_dotted, entry_rel, pkg_root_rel, has_tkinter=has_tkinter
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
                exe_name = f"{ep.name}.exe" if target is Platform.WINDOWS else ep.name
                exe = ctx.cfg.dist_dir / exe_name
                compile_loader(source, exe, ep.app_type, build_dir, target, icon=resolved_icon, stage=st)
                exes.append(exe)
        st.processed(len(exes))
    return exes


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


def _normalize_pkg_name(name: str) -> str:
    """按 PEP 503 规范化包名：连续的 ``-_.`` 替换为单 ``-``，转小写。

    使 ``ordered_set``/``ordered-set``/``Ordered.Set`` 均映射到 ``ordered-set``，
    便于跨命名风格匹配 dist-info 目录。
    """
    return re.sub(r"[-_.]+", "-", name).lower()


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


# 清理 dist 时保留的 NSIS 脚本文件名（便于改代码后重新打包分发）
_KEEP_NSI = "installer.nsi"


def clean_dist(project: Path) -> None:
    """清理项目下的 dist 目录，保留 ``installer.nsi`` 便于重新打包分发.

    ``fsp c`` 的实现：``installer.nsi`` 是 :func:`fspack.packaging.installer.generate_nsis_script`
    生成的 NSIS 脚本，保留它可在仅改 NSIS 指令（如快捷方式、注册表）后直接 ``fsp p --no-build``
    重新编译安装包，无需重新跑完整 build 流程。
    """
    import shutil

    dist = Path(project) / "dist"
    if not dist.is_dir():
        _logger.info("无 dist 目录可清理: %s", dist)
        return
    nsi_path = dist / _KEEP_NSI
    nsi_content: str | None = None
    if nsi_path.is_file():
        nsi_content = nsi_path.read_text(encoding="utf-8")
        _logger.info("保留 NSIS 脚本: %s", nsi_path)
    shutil.rmtree(dist)
    dist.mkdir(parents=True, exist_ok=True)
    if nsi_content is not None:
        nsi_path.write_text(nsi_content, encoding="utf-8")
    _logger.info("已清理: %s", dist)


def _print_build_plan(ctx: BuildContext, report: DependencyReport) -> None:
    """打印打包计划（``--dry-run`` 模式），不执行任何写操作.

    输出内容：

    - 项目基本信息：名称、版本、入口类型、入口数
    - 目标平台与 Python 版本
    - runtime 来源：Windows=embed python / Linux=python-build-standalone
    - loader 编译器：Windows=mingw-w64 / Linux=gcc
    - 缓存目录路径
    - 依赖分析：声明依赖数、AST 发现第三方数、未声明依赖（missing）数
    - 镜像源配置
    - 构建选项摘要（Nuitka/ccache/pyc_strip/no_site 等）
    """
    from rich.table import Table

    info = ctx.info
    target = ctx.cfg.target

    console.step("打包计划（dry-run，不执行实际构建）")

    # 基本信息
    basic_table = Table(title="项目信息", show_lines=False)
    basic_table.add_column("字段", style="cyan", no_wrap=True)
    basic_table.add_column("值", style="white")
    basic_table.add_row("项目名", info.name)
    basic_table.add_row("版本", info.version)
    basic_table.add_row("应用类型", info.app_type.value)
    entries = info.all_entries
    if len(entries) > 1:
        basic_table.add_row("入口数", f"{len(entries)} (多入口)")
        for ep in entries:
            basic_table.add_row(f"  - {ep.name}", f"{ep.module}.py ({ep.app_type.value})")
    else:
        basic_table.add_row("入口", f"{info.entry_module}.py")
    basic_table.add_row("目标平台", target.value)
    basic_table.add_row("Python 版本", info.py_version)
    runtime_source = "embed python" if target is Platform.WINDOWS else "python-build-standalone"
    basic_table.add_row("runtime 来源", runtime_source)
    loader_compiler = "mingw-w64" if target is Platform.WINDOWS else "gcc"
    basic_table.add_row("loader 编译器", loader_compiler)
    basic_table.add_row("缓存目录", str(ctx.cfg.embed_cache_dir.parent))
    basic_table.add_row("镜像源", f"{ctx.cfg.mirror.name} ({ctx.cfg.mirror.pypi_index})")
    console.rich.print(basic_table)

    # 依赖分析
    console.rich.print()
    dep_table = Table(title="依赖分析", show_lines=False)
    dep_table.add_column("类别", style="cyan", no_wrap=True)
    dep_table.add_column("数量", justify="right")
    dep_table.add_column("详情")
    dep_table.add_row("声明依赖", str(len(info.dependencies)), ", ".join(info.dependencies) or "(无)")
    dep_table.add_row(
        "AST 第三方",
        str(len(report.ast_third_party)),
        ", ".join(sorted(report.ast_third_party)) or "(无)",
    )
    dep_table.add_row(
        "未声明 (missing)",
        str(len(report.missing)),
        ", ".join(report.missing) if report.missing else "(无)",
    )
    dep_table.add_row("AST 标准库", str(len(report.ast_stdlib)), "(已识别)")
    dep_table.add_row("AST 本地模块", str(len(report.ast_local)), "(已识别)")
    console.rich.print(dep_table)

    # 私有包源
    if info.extra_index_urls or info.find_links:
        console.rich.print()
        src_table = Table(title="私有包源", show_lines=False)
        src_table.add_column("类型", style="cyan", no_wrap=True)
        src_table.add_column("路径")
        for url in info.extra_index_urls:
            src_table.add_row("extra-index-url", url)
        for link in info.find_links:
            src_table.add_row("find-links", link)
        console.rich.print(src_table)

    # 构建选项
    console.rich.print()
    opts_table = Table(title="构建选项", show_lines=False)
    opts_table.add_column("选项", style="cyan", no_wrap=True)
    opts_table.add_column("值", justify="center")
    opts_table.add_row("Nuitka 编译", "启用" if ctx.opts.nuitka else "关闭")
    if ctx.opts.nuitka:
        opts_table.add_row("ccache", "启用" if ctx.opts.ccache else "关闭")
        if ctx.opts.nuitka_packages:
            opts_table.add_row("nuitka-packages", ", ".join(ctx.opts.nuitka_packages))
    opts_table.add_row("字节码预编译", "关闭" if ctx.opts.no_pyc else "启用")
    opts_table.add_row("pyc 优化级别", str(ctx.opts.pyc_optimize))
    opts_table.add_row("剥离 .py (pyc_strip)", "启用" if ctx.opts.pyc_strip else "关闭")
    opts_table.add_row("标准库精简", "关闭" if ctx.opts.no_stdlib_trim else "启用")
    opts_table.add_row("禁用 site.py", "是" if ctx.opts.no_site else "否")
    if ctx.opts.keep_modules:
        opts_table.add_row("显式保留模块", ", ".join(sorted(ctx.opts.keep_modules)))
    console.rich.print(opts_table)

    # 汇总
    console.rich.print()
    wheel_count = len(info.dependencies)
    console.success(
        f"打包计划就绪：{info.name} {info.version} → {target.value} / Python {info.py_version} / "
        f"{len(entries)} 入口 / {wheel_count} 声明依赖"
    )
    console.rich.print("[dim]以上为 dry-run 预览，未执行任何下载/编译/复制。去掉 --dry-run 执行实际构建。[/]")
