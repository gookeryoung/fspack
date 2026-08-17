"""构建流水线编排实现：``build`` 主入口 + ``_execute_build`` 主体.

从 :mod:`fspack.packaging.pipeline` facade 迁入（facade 仅保留 re-export）。
本模块沿用 facade 时期的顶部轻量化约定：``fspack.console`` /
``fspack.progress`` / ``fspack.packaging.profile`` 全部为函数内延迟导入
（``BuildTracker``/``ProfileContext`` 类型注解仅在 TYPE_CHECKING 块内），
守护测试 ``test_builder_import_does_not_load_console`` /
``test_builder_import_does_not_load_progress`` /
``test_pipeline_module_no_top_level_heavy_imports``。

显式 ``import`` 运行时依赖（``write_pth``/``copy_source``/阶段函数等）是为了
兼容测试 ``monkeypatch.setattr("fspack.packaging.pipeline.executor.<attr>", ...)``
路径解析：patch 设置的是本模块对象的属性，``_execute_build`` 内的调用通过
本模块全局名字解析取到 patch 后的值。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from fspack.config import (
    DEFAULT_LINUX_PY_VERSION,
    DEFAULT_PY_VERSION,
    BuildConfig,
    BuildOptions,
    MirrorConfig,
    ProjectInfo,
    embed_cache_dir,
    resolve_py_version,
)
from fspack.packaging.log_file import LogFormat, setup_log_file, teardown_log_file
from fspack.packaging.pipeline.dist_helpers import (
    _handle_dist_incomplete,
    _remove_build_failure,
    _remove_build_ok,
    _save_build_failure,
    _save_build_ok,
)
from fspack.packaging.pipeline.stages import (
    BuildContext,
    _analyze_binary_dependencies,
    _analyze_dependencies,
    _build_entry_loaders,
    _build_frontend,
    _compile_user_sources,
    _detect_frontends,
    _download_dependencies,
    _frontend_prune_map,
    _prepare_runtime,
    _resolve_project_icon,
    _slim_runtime,
)
from fspack.packaging.runtime import write_pth
from fspack.packaging.sync import copy_source
from fspack.platform import Platform, detect_platform

if TYPE_CHECKING:
    # BuildTracker 仅用于 _execute_build 签名类型注解（``from __future__ import
    # annotations`` 使注解不在运行时求值），顶部不导入 fspack.progress 避免连锁
    # 触发 rich.progress/rich.table 加载（省 ~12ms）。build() 内实例化时才 import。
    # ProfileContext 仅用于 build() 内 ``profile_ctx`` 局部变量类型注解。
    # 顶部不导入 fspack.packaging.profile 避免连锁触发 fspack.console
    # （~17ms）+ rich.table 加载，build() 内启用 profile 时才 import。
    from fspack.packaging.profile import ProfileContext
    from fspack.progress import BuildTracker

# _print_build_plan 延迟加载：避免顶层加载 fspack.console 模块（rich 控制台），
# import fspack.builder 时不应触发 console / profile 模块加载。
# 通过 __getattr__ 惰性解析 + build() 内 dry-run 分支首次使用时绑定全局。
_PRINT_BUILD_PLAN_NAME = "_print_build_plan"

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
    auto_clean: bool = False,
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

    ``options.nuitka=True`` 时启用 Nuitka 编译模式：用 ``python -m nuitka --mode=module``
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

    ``auto_clean=True`` 时构建前自动清理 dist 残留（含 ``.build_failed`` 标记），
    避免上次中断/失败的构建产物干扰。构建异常时写入 ``dist/.build_failed`` JSON
    记录失败阶段与错误信息，下次 ``fsp b`` 检测到时提示用户。``fsp c`` 清理时
    保留 ``.build_failed`` 便于用户排查。
    """
    opts = options or BuildOptions()
    # 延迟导入：BuildTracker 实例化触发 fspack.progress 加载（含 rich.progress/
    # rich.table ~12ms）。仅在真正构建时加载，避免 import fspack.builder 热路径触发。
    from fspack.progress import BuildTracker

    tracker = BuildTracker()
    project_dir = Path(project_dir).resolve()
    target = target or detect_platform()
    dist = dist_dir or project_dir / "dist"
    cache = embed_cache or embed_cache_dir()
    cfg = BuildConfig(
        project_dir=project_dir,
        dist_dir=dist,
        embed_cache_dir=cache,
        mirror=mirror,
        target=target,
    )

    # dist 半成品检测：dist 已存在且含构建产物但缺少 stamp 文件，或存在
    # .build_failed 标记时，按 auto_clean 决定自动清理或告警。
    _handle_dist_incomplete(dist, auto_clean)

    log_wrapper = setup_log_file(Path(log_file), log_format) if log_file is not None else None
    # 延迟导入：profile 模块顶部 from fspack.console import console 会连锁触发
    # rich.console/rich.logging/rich.theme 加载（~17ms）。仅在启用 profile 时加载。
    profile_ctx: ProfileContext | None = None
    if profile:
        from fspack.packaging.profile import ProfileContext

        profile_ctx = ProfileContext()
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
    except BaseException as exc:
        # 构建异常时写入 .build_failed 供下次 fsp b 检测并提示用户。
        # 用 BaseException 而非 Exception：KeyboardInterrupt（Ctrl+C）与
        # SystemExit 不属于 Exception 子类，同样写入失败标记，覆盖
        # 「中断退出但 dist 残留半成品」的检测盲区
        _save_build_failure(dist, tracker, exc)
        raise
    else:
        # 构建成功：清除可能残留的 .build_failed 标记，写入 .build_ok 完成标记
        # （no_pyc/交叉构建不产出编译 stamp，靠 .build_ok 避免二次构建误判半成品）
        _remove_build_failure(dist)
        _save_build_ok(dist)
    finally:
        teardown_log_file(log_wrapper)
    if profile_ctx is not None:
        # print_profile_report 与 ProfileContext 同模块，此时已加载，import 仅 dict 查询
        from fspack.packaging.profile import print_profile_report

        report = profile_ctx.collect(tracker)
        print_profile_report(report)
    return info


def _execute_build(  # noqa: PLR0912, PLR0913
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

    注意：本函数内部调用的阶段函数与辅助（``_prepare_runtime``、
    ``write_pth``、``copy_source`` 等）都通过本模块**全局名字**解析，因此
    测试 monkeypatch ``fspack.packaging.pipeline.executor.*`` 路径时能生效。
    切勿改为从子模块直接导入绑定到局部名字，否则 patch 不生效。
    """
    with tracker.stage("解析项目") as st:
        info = resolve_project_info(project_dir, py_version, target)
        # 合并 CLI 私有包源到 info（CLI 追加在配置之后，去重保留首次出现）
        merged_extra = tuple(dict.fromkeys((*info.extra_index_urls, *extra_index_urls)))
        merged_links = tuple(dict.fromkeys((*info.find_links, *find_links)))
        if merged_extra != info.extra_index_urls or merged_links != info.find_links:
            info = replace(info, extra_index_urls=merged_extra, find_links=merged_links)
        _logger.info("项目: %s %s (%s) 目标: %s", info.name, info.version, info.app_type.value, target.value)
        detail = f"{info.name} {info.version} ({info.app_type.value})"
        if opts.extras:
            extras_str = ", ".join(sorted(opts.extras))
            detail += f" | extras: {extras_str}"
            _logger.info("启用 extras: %s", extras_str)
        st.set_detail(detail)

    runtime_dir = cfg.dist_dir / "runtime"
    ctx = BuildContext(tracker=tracker, info=info, cfg=cfg, opts=opts, runtime_dir=runtime_dir)

    # dry-run 模式：仅解析项目 + 分析依赖，打印计划后返回
    if dry_run:
        # 首次使用时才加载 plan_printer（触发 fspack.console rich 导入）
        from fspack.packaging.pipeline.plan_printer import _print_build_plan as _pbp

        globals()[_PRINT_BUILD_PLAN_NAME] = _pbp
        report = _analyze_dependencies(ctx, save_cache=False)
        _pbp(ctx, report)
        return info

    # 真实构建开始：删除旧的 .build_ok 完成标记（与 .build_failed 的
    # 成功后删除点对齐），保证本次构建中断/失败时不残留"成功完成"标记
    _remove_build_ok(cfg.dist_dir)

    # 前端阶段：识别 web 结构（web-static-dirs 配置或 package.json 结构扫描），
    # 产物缺失时在复制源码前就地构建——否则打出的应用会在终端用户机器上
    # 尝试安装前端依赖（无 node 环境，必然失败）。无前端项目时整段跳过。
    _frontends = _detect_frontends(project_dir, info.web_static_dirs)
    if _frontends:
        with tracker.stage("构建前端") as st:
            st.set_detail(_build_frontend(_frontends))

    site_packages = _prepare_runtime(ctx)
    report = _analyze_dependencies(ctx)
    has_tkinter = _download_dependencies(ctx, site_packages, report)

    if target is Platform.WINDOWS:
        # tkinter 补充到 runtime/Lib/tkinter/，需将 Lib 加入 _pth 使其可 import
        # （_pth 默认含 ..\site-packages 与 ..\src，不含 Lib 本身）
        extra_pth_paths = ("Lib",) if has_tkinter else ()
        write_pth(cfg.dist_dir, info.py_version, extra_paths=extra_pth_paths, enable_site=not opts.no_site)

    # .pth 文件优化：no_site=True 时 site.py 不加载，.pth 文件不会被
    # 处理，保留它们仅占空间且可能误导。剥离 site-packages 下所有 .pth 文件，
    # 典型节省数 KB 到数十 KB（pywin32_postinstall.pth、distutils-precedence.pth 等）。
    # no_site=False 时保留 .pth 文件，site.py 启动时处理（如设置 sys.path）。
    if opts.no_site and site_packages.is_dir():
        for _pth_file in site_packages.glob("*.pth"):
            try:
                _pth_file.unlink()
            except OSError as e:
                # Windows 杀软临时占用等场景：跳过该文件继续，不阻断构建
                _logger.warning("删除 .pth 失败: %s（%s）", _pth_file, e)

    with tracker.stage("复制源码") as st:
        # 延迟导入：spinner 触发 fspack.progress 加载（含 rich.progress ~12ms）。
        # 仅在实际复制源码时加载，避免 import fspack.builder 热路径触发。
        from fspack.progress import spinner

        src_dst = cfg.dist_dir / "src"
        with spinner(f"复制 {info.name} 源码"):
            copy_source(
                project_dir,
                src_dst,
                extra_excludes=info.exclude_dirs,
                data_dirs=info.data_dirs,
                web_static_dirs=info.web_static_dirs,
                frontend_prune=_frontend_prune_map(_frontends),
            )

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

    # Win7 门禁（硬）：loader exe 由 fspack 内置 C 源码 + mingw 编译，导入表
    # 引入 Win8+ API 属 fspack 回归，违规即阻断构建，不允许带病出包。
    if target is Platform.WINDOWS:
        from fspack.packaging.win7_scan import enforce_win7_loaders

        with tracker.stage("Win7 loader 校验") as st:
            enforce_win7_loaders(exes)
            st.set_detail(f"{len(exes)} 个 exe 通过")

    # 二进制依赖分析（可选）：解析 .dll/.so/.dylib 依赖树，剥离无引用文件。
    # 仅当 --analyze-deps 启用时执行，节省字节数写入 tracker 的"依赖分析"stage。
    if opts.analyze_deps:
        _analyze_binary_dependencies(ctx)

    # SBOM 与 manifest 并行生成：两者都只读扫描 dist 目录，无写入冲突。
    # 用 ThreadPoolExecutor 并行 submit，主线程在各 stage 内等待对应 future。
    # stage 记录保持串行（避免 tracker._records 顺序不稳定）。单个启用时
    # 退化为串行调用，避免线程池启动开销。size_report 在 summary 之后串行
    # 执行（控制台输出顺序敏感，不能与 summary 并行）。
    sbom_enabled = not opts.no_sbom
    manifest_enabled = not opts.no_manifest
    sbom_future = manifest_future = None
    _post_build_pool = None
    if sbom_enabled and manifest_enabled:
        from concurrent.futures import ThreadPoolExecutor

        from fspack.packaging.manifest import generate_manifest
        from fspack.packaging.sbom import generate_sbom

        _post_build_pool = ThreadPoolExecutor(max_workers=2)
        sbom_future = _post_build_pool.submit(generate_sbom, cfg.dist_dir, info)
        manifest_future = _post_build_pool.submit(generate_manifest, cfg.dist_dir, info)

    try:
        # SBOM 生成（默认启用，--no-sbom 关闭）：扫描 dist 下 site-packages 的
        # *.dist-info 提取依赖元信息，生成 SPDX 2.3 兼容 JSON 到 dist/release/。
        # 放在所有构建阶段之后、summary 之前，使 SBOM stage 出现在汇总表中。
        # 扫描失败不阻断构建（warning 后继续），SBOM 仅为审计辅助产物。
        if sbom_enabled:
            from fspack.packaging.sbom import generate_sbom

            with tracker.stage("生成 SBOM") as st:
                try:
                    sbom_path = sbom_future.result() if sbom_future is not None else generate_sbom(cfg.dist_dir, info)
                    st.processed(1)
                    st.set_detail(sbom_path.name)
                except Exception as e:
                    # 放宽为 Exception：SBOM 仅为审计辅助产物，任何生成失败
                    # （含非 OSError 的解析/序列化异常）降级为 warning 不阻断构建
                    _logger.warning("SBOM 生成失败，跳过: %s", e)
                    st.set_detail("生成失败")

        # manifest 产物清单生成（默认启用，--no-manifest 关闭）：扫描 dist 下
        # 所有文件按分类记录大小/SHA256，生成 JSON 到 dist/release/。版本间
        # 可通过 ``fsp manifest diff`` 对比差异。失败不阻断构建。
        if manifest_enabled:
            from fspack.packaging.manifest import generate_manifest

            with tracker.stage("生成产物清单") as st:
                try:
                    manifest_path = (
                        manifest_future.result()
                        if manifest_future is not None
                        else generate_manifest(cfg.dist_dir, info)
                    )
                    st.processed(1)
                    st.set_detail(manifest_path.name)
                except Exception as e:
                    # 放宽为 Exception：与 SBOM 同口径，任何生成失败降级告警
                    _logger.warning("manifest 生成失败，跳过: %s", e)
                    st.set_detail("生成失败")
    finally:
        if _post_build_pool is not None:
            _post_build_pool.shutdown(wait=True)

    # Win7 兼容扫描（软门禁，默认启用，--no-win7-scan 关闭）：dist 下全部
    # .dll/.pyd/.exe 导入表检查，第三方依赖与 Nuitka 产物违规无法自动修复，
    # 不阻断构建，生成文本报告到 dist/release/win7-compat-report.txt。
    # 仅 Windows 目标（Linux/macOS 产物不运行于 Win7）。
    if target is Platform.WINDOWS and not opts.no_win7_scan:
        from fspack.packaging.win7_scan import scan_dist_win7, write_win7_report

        with tracker.stage("Win7 兼容扫描") as st:
            report = scan_dist_win7(cfg.dist_dir)
            report_path = write_win7_report(report, cfg.dist_dir)
            st.processed(report.scanned)
            if report.violations:
                st.set_detail(f"{len(report.violations)} 个文件违规，见 {report_path.name}")
                _logger.warning(
                    "Win7 兼容扫描发现 %d 个违规文件（不阻断构建），详见 %s",
                    len(report.violations),
                    report_path,
                )
            else:
                st.set_detail(f"{report.scanned} 个文件通过")

    # 延迟导入：console 触发 fspack.console 加载（含 rich.console/rich.logging/
    # rich.theme ~17ms）。仅在构建完成输出 summary 时加载。注意 _execute_build
    # 内 spinner（L277）已连锁加载 fspack.console，此 import 为显式自包含。
    from fspack.console import console

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


def __getattr__(name: str):
    """模块级惰性属性：``_print_build_plan`` 仅在首次访问时导入 plan_printer."""
    if name == _PRINT_BUILD_PLAN_NAME:
        from fspack.packaging.pipeline.plan_printer import _print_build_plan

        globals()[_PRINT_BUILD_PLAN_NAME] = _print_build_plan
        return _print_build_plan
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
