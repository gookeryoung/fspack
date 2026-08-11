"""构建流水线编排入口：``build`` 主入口 + 阶段函数 re-export + 公共辅助.

本模块从 :mod:`fspack.builder` 抽离，``builder.py`` 通过 re-export 保持公开 API 不变。
按职责拆分到两个模块：

- :mod:`fspack.packaging.pipeline`（本模块）：``build``/``_execute_build``/``resolve_project_info``
  /``clean_dist``/``_print_build_plan`` 入口与编排，``_KEEP_NSI`` 常量
- :mod:`fspack.packaging.pipeline.stages`：阶段函数实现（``_prepare_runtime``/
  ``_analyze_dependencies``/``_download_dependencies``/``_compile_user_sources``/
  ``_build_entry_loaders``）+ ``BuildContext`` + 依赖缓存 + icon 解析 + wheel 解压

显式 ``import`` 运行时依赖（``write_pth``/``copy_source``/``compile_loader``/
``download_embed``/``extract_embed``/``download_standalone``/``extract_standalone``/
``download_wheels``）是为了兼容测试 ``monkeypatch.setattr("fspack.packaging.pipeline.<attr>", ...)``
路径解析：patch 设置的是模块对象的属性，``_execute_build`` 内的调用通过模块全局
名字解析取到 patch 后的值。

从 :mod:`fspack.packaging.pipeline.stages` re-export 阶段函数与 ``BuildContext``，
保持 ``fspack.packaging.pipeline.<fn>`` patch 路径兼容（测试通过本模块 patch 阶段函数）。
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
    DependencyReport,
    MirrorConfig,
    ProjectInfo,
    embed_cache_dir,
    resolve_py_version,
)
from fspack.packaging.loader import compile_loader  # noqa: F401
from fspack.packaging.log_file import LogFormat, setup_log_file, teardown_log_file

# re-export 阶段函数与 BuildContext：保持 fspack.packaging.pipeline.<fn> patch 路径兼容
from fspack.packaging.pipeline.stages import (
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

if TYPE_CHECKING:
    # BuildTracker 仅用于 _execute_build 签名类型注解（``from __future__ import
    # annotations`` 使注解不在运行时求值），顶部不导入 fspack.progress 避免连锁
    # 触发 rich.progress/rich.table 加载（省 ~12ms）。build() 内实例化时才 import。
    # ProfileContext 仅用于 build() 内 ``profile_ctx`` 局部变量类型注解。
    # 顶部不导入 fspack.packaging.profile 避免连锁触发 fspack.console
    # （~17ms）+ rich.table 加载，build() 内启用 profile 时才 import。
    from fspack.packaging.profile import ProfileContext
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
    cfg = BuildConfig(project_dir=project_dir, dist_dir=dist, embed_cache_dir=cache, mirror=mirror, target=target)

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
    except Exception as exc:
        # 构建异常时写入 .build_failed 供下次 fsp b 检测并提示用户
        _save_build_failure(dist, tracker, exc)
        raise
    else:
        # 构建成功：清除可能残留的 .build_failed 标记
        _remove_build_failure(dist)
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
        report = _analyze_dependencies(ctx, save_cache=False)
        _print_build_plan(ctx, report)
        return info

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
            _pth_file.unlink()

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

    # 二进制依赖分析（可选）：解析 .dll/.so/.dylib 依赖树，剥离无引用文件。
    # 仅当 --analyze-deps 启用时执行，节省字节数写入 tracker 的"依赖分析"stage。
    if opts.analyze_deps:
        _analyze_binary_dependencies(ctx)

    # SBOM 生成（默认启用，--no-sbom 关闭）：扫描 dist 下 site-packages 的
    # *.dist-info 提取依赖元信息，生成 SPDX 2.3 兼容 JSON 到 dist/release/。
    # 放在所有构建阶段之后、summary 之前，使 SBOM stage 出现在汇总表中。
    # 扫描失败不阻断构建（warning 后继续），SBOM 仅为审计辅助产物。
    if not opts.no_sbom:
        from fspack.packaging.sbom import generate_sbom

        with tracker.stage("生成 SBOM") as st:
            try:
                sbom_path = generate_sbom(cfg.dist_dir, info)
                st.processed(1)
                st.set_detail(sbom_path.name)
            except OSError as e:
                _logger.warning("SBOM 生成失败，跳过: %s", e)
                st.set_detail("生成失败")

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


# 清理 dist 时保留的 NSIS 脚本文件名（便于改代码后重新打包分发）
_KEEP_NSI = "installer.nsi"

# 构建失败标记文件：构建异常时写入，下次 fsp b 检测到时提示用户
_BUILD_FAILED = ".build_failed"

# 编译阶段产出的 stamp 文件名：存在即说明上次构建至少完成到编译阶段
_PYC_STAMP = ".pyc_stamp"
_NUITKA_STAMP = ".nuitka_compile_stamp"


def _has_dist_artifacts(dist_dir: Path) -> bool:
    """dist 目录是否含构建产物（子目录或 .exe，排除 NSI/诊断文件）."""
    return any(
        p.name not in (_KEEP_NSI, _BUILD_FAILED) and (p.is_dir() or p.suffix == ".exe") for p in dist_dir.iterdir()
    )


def _has_build_stamps(dist_dir: Path) -> bool:
    """dist 目录是否含编译 stamp 文件（说明上次构建至少完成到编译阶段）."""
    return (dist_dir / _PYC_STAMP).is_file() or (dist_dir / _NUITKA_STAMP).is_file()


def _handle_dist_incomplete(dist_dir: Path, auto_clean: bool) -> None:
    """检测 dist 半成品并按 auto_clean 决定自动清理或告警.

    iter-140 引入：替代 iter-128 的 ``_warn_dist_incomplete``，扩展支持
    ``.build_failed`` 标记检测与 ``--auto-clean`` 自动清理。

    检测条件（任一即视为半成品）：

    - dist 含构建产物但缺少编译 stamp 文件（中断/失败的构建残留）
    - dist 含 ``.build_failed`` 标记（上次构建异常退出）

    ``auto_clean=True`` 时调用 :func:`clean_dist` 清空 dist（不保留诊断文件，
    全新开始）。``auto_clean=False`` 时仅告警，提示用户 ``fsp c`` 或
    ``fsp b --auto-clean``。

    ``.build_failed`` 存在时额外输出失败阶段与错误信息，便于用户定位问题。
    """
    if not dist_dir.is_dir():
        return

    failed_info = _load_build_failure(dist_dir)
    has_artifacts = _has_dist_artifacts(dist_dir)
    has_stamps = _has_build_stamps(dist_dir)

    if failed_info:
        from fspack.console import console

        stage = failed_info.get("stage", "未知")
        error = failed_info.get("error", "")
        timestamp = failed_info.get("timestamp", "")
        console.warn(f"上次构建失败（{timestamp}）：阶段 [{stage}]")
        if error:
            console.rich.print(f"  错误: {error}")

    is_incomplete = (has_artifacts and not has_stamps) or failed_info is not None
    if not is_incomplete:
        return

    if auto_clean:
        _logger.info("auto-clean: 清理 dist 残留: %s", dist_dir)
        _clean_dist_dir(dist_dir, keep_diagnostics=False)
    else:
        _logger.warning(
            "dist 目录含上次构建的残留: %s，建议执行 `fsp c` 清理或 `fsp b --auto-clean` 自动清理后重新构建。",
            dist_dir,
        )


def _save_build_failure(dist_dir: Path, tracker: BuildTracker, exc: Exception) -> None:
    """构建异常时写入 ``dist/.build_failed`` JSON 记录失败信息.

    iter-140 引入：供下次 ``fsp b`` 检测并提示用户。记录内容：

    - ``stage``：失败时最后完成的阶段名（从 ``tracker.records`` 取末尾）
    - ``error``：异常类型与消息（截断到 500 字符避免文件过大）
    - ``timestamp``：ISO 格式时间戳

    dist 目录不存在时跳过（构建可能在创建 dist 前失败）。写入失败 best-effort
    （OSError 不阻断异常传播）。
    """
    import json
    from datetime import datetime

    from fspack._util.fsutil import atomic_write_text

    if not dist_dir.is_dir():
        return

    records = tracker.records
    stage = records[-1].name if records else "未知"
    error_msg = f"{type(exc).__name__}: {exc}"
    if len(error_msg) > 500:
        error_msg = error_msg[:497] + "..."

    data = {
        "stage": stage,
        "error": error_msg,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        atomic_write_text(dist_dir / _BUILD_FAILED, json.dumps(data, ensure_ascii=False, indent=2))
    except OSError as e:
        _logger.warning("写入 .build_failed 失败: %s", e)


def _load_build_failure(dist_dir: Path) -> dict[str, str] | None:
    """读取 ``dist/.build_failed`` JSON，返回失败信息 dict.

    文件不存在或解析失败返回 None（不阻断构建流程）。读取 → 解析 → 根 dict
    校验的公共骨架委托 :func:`fspack._util.jsoncache.load_json_dict`
    （``delete_on_corrupt=False``：诊断文件不删除）；值统一转 ``str`` 为本函数外壳。
    """
    from fspack._util.jsoncache import load_json_dict

    path = dist_dir / _BUILD_FAILED
    data = load_json_dict(path, delete_on_corrupt=False, logger=_logger)
    if data is None:
        return None
    return {k: str(v) for k, v in data.items()}


def _remove_build_failure(dist_dir: Path) -> None:
    """构建成功后删除 ``.build_failed`` 标记（如存在）."""
    path = dist_dir / _BUILD_FAILED
    if path.is_file():
        try:
            path.unlink()
        except OSError as e:
            _logger.warning("删除 .build_failed 失败: %s", e)


def _clean_dist_dir(dist_dir: Path, *, keep_diagnostics: bool) -> None:
    """清空 dist 目录，按 keep_diagnostics 决定是否保留诊断文件.

    :param keep_diagnostics: True 时保留 ``installer.nsi`` 与 ``.build_failed``
        （供 ``fsp c`` 使用，用户排查后保留诊断信息）；False 时全清（供
        ``--auto-clean`` 使用，全新开始构建）。
    """
    import shutil

    if not dist_dir.is_dir():
        return

    keep_names: list[str] = [_KEEP_NSI]
    if keep_diagnostics:
        keep_names.append(_BUILD_FAILED)

    preserved: dict[str, str] = {}
    for name in keep_names:
        path = dist_dir / name
        if path.is_file():
            try:
                preserved[name] = path.read_text(encoding="utf-8")
                _logger.info("保留: %s", path)
            except OSError:
                pass

    shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    for name, content in preserved.items():
        try:
            (dist_dir / name).write_text(content, encoding="utf-8")
        except OSError as e:
            _logger.warning("恢复 %s 失败: %s", name, e)
    _logger.info("已清理: %s", dist_dir)


def clean_dist(project: Path) -> None:
    """清理项目下的 dist 目录，保留 ``installer.nsi`` 与 ``.build_failed``.

    ``fsp c`` 的实现（iter-140 扩展）：

    - ``installer.nsi``：NSIS 脚本，保留便于改代码后 ``fsp p --no-build`` 重打包
    - ``.build_failed``：失败诊断标记，保留便于用户排查上次构建失败原因

    全清场景（``fsp b --auto-clean``）调用 :func:`_clean_dist_dir` 并传
    ``keep_diagnostics=False``。
    """
    dist = Path(project) / "dist"
    if not dist.is_dir():
        _logger.info("无 dist 目录可清理: %s", dist)
        return
    _clean_dist_dir(dist, keep_diagnostics=True)


def _print_build_plan(ctx: BuildContext, report: DependencyReport) -> None:  # noqa: PLR0912
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
    # 延迟导入：dry-run 路径不经过 _execute_build 的 spinner 加载，需独立加载
    # fspack.console（含 rich.console/rich.logging/rich.theme ~17ms）。
    from rich.table import Table

    from fspack.console import console

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
    if ctx.opts.extras:
        dep_table.add_row(
            "启用 extras",
            str(len(ctx.opts.extras)),
            ", ".join(sorted(ctx.opts.extras)),
        )
        dep_table.add_row(
            "扩展后依赖",
            str(len(report.declared)),
            ", ".join(report.declared) or "(无)",
        )
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
