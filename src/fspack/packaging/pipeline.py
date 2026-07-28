"""构建流水线编排入口：``build`` 主入口 + 阶段函数 re-export + 公共辅助.

本模块从 :mod:`fspack.builder` 抽离，``builder.py`` 通过 re-export 保持公开 API 不变。
按职责拆分到两个模块：

- :mod:`fspack.packaging.pipeline`（本模块）：``build``/``_execute_build``/``resolve_project_info``
  /``clean_dist``/``_print_build_plan`` 入口与编排，``_KEEP_NSI`` 常量
- :mod:`fspack.packaging.pipeline_stages`：阶段函数实现（``_prepare_runtime``/
  ``_analyze_dependencies``/``_download_dependencies``/``_compile_user_sources``/
  ``_build_entry_loaders``）+ ``BuildContext`` + 依赖缓存 + icon 解析 + wheel 解压

显式 ``import`` 运行时依赖（``write_pth``/``copy_source``/``compile_loader``/
``download_embed``/``extract_embed``/``download_standalone``/``extract_standalone``/
``download_wheels``）是为了兼容测试 ``monkeypatch.setattr("fspack.packaging.pipeline.<attr>", ...)``
路径解析：patch 设置的是模块对象的属性，``_execute_build`` 内的调用通过模块全局
名字解析取到 patch 后的值。

从 :mod:`fspack.packaging.pipeline_stages` re-export 阶段函数与 ``BuildContext``，
保持 ``fspack.packaging.pipeline.<fn>`` patch 路径兼容（测试通过本模块 patch 阶段函数）。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from fspack.config import (
    DEFAULT_LINUX_PY_VERSION,
    DEFAULT_PY_VERSION,
    BuildConfig,
    BuildOptions,
    DependencyReport,
    MirrorConfig,
    ProjectInfo,
    embed_cache_dir,
    resolve_py_version,
)
from fspack.console import console
from fspack.packaging.loader import compile_loader  # noqa: F401
from fspack.packaging.log_file import LogFormat, setup_log_file, teardown_log_file

# re-export 阶段函数与 BuildContext：保持 fspack.packaging.pipeline.<fn> patch 路径兼容
from fspack.packaging.pipeline_stages import (
    _DEFAULT_ICON,  # noqa: F401
    BuildContext,
    _analyze_binary_dependencies,
    _analyze_dependencies,
    _build_entry_loaders,
    _compile_user_sources,
    _dep_cache_load,  # noqa: F401
    _dep_cache_path,  # noqa: F401
    _dep_cache_save,  # noqa: F401
    _download_dependencies,
    _normalize_pkg_name,  # noqa: F401
    _prepare_runtime,
    _prepare_standalone_runtime,  # noqa: F401
    _prepare_windows_runtime,  # noqa: F401
    _resolve_project_icon,
    _site_packages_has_deps,  # noqa: F401
    _slim_runtime,
    _strip_version_specifier,  # noqa: F401
    default_icon_path,
    fspack_wheel_cache_dir,
    unpack_wheels,
)
from fspack.packaging.profile import ProfileContext, print_profile_report

# 显式导入运行时依赖：兼容测试 monkeypatch.setattr("fspack.packaging.pipeline.<func>", ...)
from fspack.packaging.runtime import (  # noqa: F401
    download_embed,
    download_standalone,
    extract_embed,
    extract_standalone,
    write_pth,
)
from fspack.packaging.sync import copy_source
from fspack.packaging.wheels import download_wheels  # noqa: F401
from fspack.platform import Platform, detect_platform
from fspack.progress import BuildTracker

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


def resolve_project_info(project_dir: Path, py_version: str | None, target: Platform) -> ProjectInfo:
    """解析项目信息并自动选择 Python 版本，返回带已解析版本的 ``ProjectInfo``.

    优先级见 :func:`resolve_py_version`：``--py-version`` > ``.python-version``
    > ``requires-python`` > 平台默认。用于 :func:`build` 与安装包编排共享版本
    解析逻辑，确保 ``no_build`` 模式下发行包文件名也使用正确的 Python 版本。
    """
    info = ProjectInfo.from_dir(project_dir, py_version)
    # Linux 与 macOS 均用 python-build-standalone，共享默认版本与版本表
    default_ver = DEFAULT_LINUX_PY_VERSION if target in (Platform.LINUX, Platform.MACOS) else DEFAULT_PY_VERSION
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
    profile: bool = False,
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

    ``profile=True`` 时启用耗时分析：用 ``tracemalloc`` 采集内存峰值，
    ``time.process_time()`` 采集 CPU 时间，构建结束后输出各阶段 wall time /
    占比 / 缓存命中 / 下载 / 节省等指标的表格，以及资源总览（wall/CPU/CPU 占比/
    内存峰值）。便于识别瓶颈阶段。
    """
    opts = options or BuildOptions()
    tracker = BuildTracker()
    project_dir = Path(project_dir).resolve()
    target = target or detect_platform()
    dist = dist_dir or project_dir / "dist"
    cache = embed_cache or embed_cache_dir()
    cfg = BuildConfig(project_dir=project_dir, dist_dir=dist, embed_cache_dir=cache, mirror=mirror, target=target)

    log_wrapper = setup_log_file(Path(log_file), log_format) if log_file is not None else None
    profile_ctx = ProfileContext() if profile else None
    try:
        if profile_ctx is not None:
            with profile_ctx:
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
        else:
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
    if profile_ctx is not None:
        report = profile_ctx.collect(tracker)
        print_profile_report(report)
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

    # 精简 standalone runtime：在 _precompile_pyc 之后（构建期 compileall 已用完 python3.X 二进制），
    # strip libpython 调试符号 + 删 python3.X 二进制 + 删 include/share + 非 tkinter 项目剥离 Tcl/Tk。
    # 仅 Linux/macOS 目标生效，Windows embed 已精简且无调试符号，函数内自动跳过。
    # --no-slim-runtime 关闭此阶段（BuildOptions.no_slim_runtime=True）。
    _slim_runtime(ctx, has_tkinter)

    # icon 优先级：CLI --icon > 项目 [tool.fspack] icon > 自动搜索 favicon.* > 默认 app.ico（仅 Windows）
    # Linux 目标无图标资源概念，统一传 None
    with tracker.stage("解析图标") as st:
        resolved_icon = _resolve_project_icon(opts.icon, info.icon, project_dir, cfg.dist_dir / "build", target)
        if resolved_icon is not None:
            st.set_detail(str(resolved_icon.name))

    exes = _build_entry_loaders(ctx, resolved_icon, has_tkinter)

    # 二进制依赖分析（可选）：解析 .dll/.so/.dylib 依赖树，剥离无引用文件。
    # 仅当 --analyze-deps 启用时执行，节省字节数写入 tracker 的"依赖分析"stage。
    if opts.analyze_deps:
        _analyze_binary_dependencies(ctx)

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
    if target is Platform.WINDOWS:
        loader_compiler = "mingw-w64"
    elif target is Platform.MACOS:
        loader_compiler = "clang"
    else:
        loader_compiler = "gcc"
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


# 显式导入 spinner：_execute_build 内使用，兼容测试 monkeypatch
from fspack.progress import spinner  # noqa: E402
