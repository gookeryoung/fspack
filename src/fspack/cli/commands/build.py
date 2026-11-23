"""build 子命令：参数合并、多版本目标解析与 builder 调用."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fspack.config import MirrorConfig, ProjectInfo
    from fspack.core.platform import Platform

__all__ = ["_in_ci_environment", "_parse_lazy_imports", "_parse_target", "_resolve_mirror", "_run_build"]


def _resolve_mirror(value: str | None) -> MirrorConfig:
    """执行期解析 ``--mirror``：非法值报错并以退出码 2 退出（与 argparse 一致）.

    choices 校验刻意从 parser 构建期移到执行期，避免 ``fsp --help`` 等
    轻命令为 choices 加载 ``fspack.config``（~20ms）。
    """
    from fspack.config import get_mirror

    try:
        return get_mirror(value)
    except KeyError as exc:
        from fspack.ui.console import console

        console.error(exc.args[0])
        raise SystemExit(2) from None


def _in_ci_environment() -> bool:
    """是否运行在 CI（GitHub Actions/GitLab/Jenkins/Azure 等）."""
    return any(os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILD_NUMBER", "TF_BUILD"))


def _resolve_build_targets(
    project: Path,
    info: ProjectInfo,
    explicit: str | None,
    target: Platform | None,
) -> list[tuple[str | None, Path | None]]:
    """解析本次 build/package 的 Python 版本目标列表.

    返回 ``(py_version, dist_dir)`` 列表，语义：

    - CLI ``--py-version`` 显式指定：单目标 ``(版本, None)``，输出到 dist 根，
      并忽略 ``[tool.fspack] python-versions``（显式优先于配置）
    - 未配置 ``python-versions``：单目标 ``(None, None)``，版本由
      :func:`fspack.config.resolve_py_version` 按原有优先级解析，行为不变
    - 配置 1 个版本：单目标 ``(版本, None)``，等价于 ``--py-version``
    - 配置 ≥2 个版本：每版本一个目标，输出目录 ``<project>/dist/py<完整版本>``，
      各版本产物互不覆盖（发行包仍在各自 ``dist/py<ver>/release/``，文件名已含
      ``py<ver>`` 段）

    配置的短版本号（如 ``"3.11"``）按目标平台归一化为完整版本（如 ``3.11.9``），
    供目录名与产物名使用；重复版本去重并保留首次出现顺序。
    """
    if explicit:
        return [(explicit, None)]
    configured = info.build_defaults.python_versions
    if not configured:
        return [(None, None)]

    from fspack.config import (
        DEFAULT_LINUX_PY_VERSION,
        DEFAULT_PY_VERSION,
        resolve_py_version,
    )
    from fspack.core.platform import Platform, detect_platform

    resolved_target = target or detect_platform()
    default_ver = (
        DEFAULT_LINUX_PY_VERSION if resolved_target in (Platform.LINUX, Platform.MACOS) else DEFAULT_PY_VERSION
    )
    versions: list[str] = []
    for raw in configured:
        resolved = resolve_py_version(project, raw, info.requires_python, default_ver, resolved_target)
        if resolved not in versions:
            versions.append(resolved)
    if len(versions) == 1:
        return [(versions[0], None)]
    dist_root = project / "dist"
    return [(ver, dist_root / f"py{ver}") for ver in versions]


def _run_build(project: Path, ns: argparse.Namespace) -> None:
    """执行单项目 build 子命令."""
    from dataclasses import replace

    from fspack.builder import build
    from fspack.config import ProjectInfo, build_options_from_defaults
    from fspack.core.exceptions import ProjectError
    from fspack.packaging.log_file import LogFormat

    # 合并 [tool.fspack] 构建默认值与 CLI 标志：
    # - 先用 build_options_from_defaults 构造配置层 base（config or BuildOptions 默认值）
    # - 再用 replace() 应用 CLI 覆盖：布尔开关用 ``cli or base``（任一启用 → 启用），
    #   pyc_optimize 用 ``cli if cli is not None else base``（CLI 显式指定优先）
    info = ProjectInfo.from_dir(project, ns.py_version)
    base = build_options_from_defaults(info.build_defaults)
    # extras 合并：CLI --extra 指定时完全覆盖配置默认（集合语义，非合并）；
    # 未指定时用 [tool.fspack] extras 配置默认。校验未知分组名。
    cli_extras = ns.extras if getattr(ns, "extras", None) else None
    enabled_extras = frozenset(cli_extras) if cli_extras is not None else base.extras
    unknown = enabled_extras - set(info.optional_dependencies)
    if unknown:
        raise ProjectError(f"未知的 extras 分组: {sorted(unknown)}，可选: {sorted(info.optional_dependencies)}")
    options = replace(
        base,
        keep_modules=set(ns.keep_modules) if ns.keep_modules else base.keep_modules,
        icon=Path(ns.icon).resolve() if ns.icon else base.icon,
        no_stdlib_trim=ns.no_stdlib_trim or base.no_stdlib_trim,
        no_slim_runtime=ns.no_slim_runtime or base.no_slim_runtime,
        no_stdlib_zip=ns.no_stdlib_zip or base.no_stdlib_zip,
        slim_stdlib=ns.slim_stdlib if ns.slim_stdlib is not None else base.slim_stdlib,
        splash=ns.splash or base.splash,
        no_pyc=ns.no_pyc or base.no_pyc,
        pyc_strip=ns.pyc_strip or base.pyc_strip,
        pyc_optimize=ns.pyc_optimize if ns.pyc_optimize is not None else base.pyc_optimize,
        no_site=ns.no_site or base.no_site,
        nuitka=ns.nuitka or base.nuitka,
        ccache=ns.ccache or base.ccache,
        nuitka_packages=tuple(dict.fromkeys((*base.nuitka_packages, *(ns.nuitka_pkg or [])))),
        no_size_report=ns.no_size_report or base.no_size_report,
        analyze_deps=ns.analyze_deps or base.analyze_deps,
        extras=enabled_extras,
        lazy_imports=_parse_lazy_imports(ns.lazy_imports, base.lazy_imports),
        require_hashes=ns.require_hashes or base.require_hashes,
        lock=ns.lock or base.lock,
        frozen=ns.frozen or base.frozen,
        no_sbom=ns.no_sbom or base.no_sbom,
        no_manifest=getattr(ns, "no_manifest", False) or base.no_manifest,
        no_win7_scan=getattr(ns, "no_win7_scan", False) or base.no_win7_scan,
        no_win7_dll=getattr(ns, "no_win7_dll", False) or base.no_win7_dll,
        open_browser=ns.open_browser or base.open_browser,
        compiler=ns.compiler if getattr(ns, "compiler", None) is not None else base.compiler,
    )
    if ns.log_file:
        log_file = Path(ns.log_file).resolve()
    elif not getattr(ns, "no_log_file", False) and _in_ci_environment():
        # CI 日志默认落盘，失败时可直接上传/翻查；本地交互构建不产生额外文件。
        log_file = project / ".fspack" / "build.log"
    else:
        log_file = None
    log_format = LogFormat.parse(ns.log_format)
    if (ns.profile_out or ns.profile_compare) and not ns.profile:
        raise ProjectError("--profile-out/--profile-compare 需配合 --profile 使用")
    from fspack.packaging.profile_log import ProfileOptions

    target = _parse_target(ns.target)
    mirror = _resolve_mirror(ns.mirror)
    # 多版本：--py-version 显式优先，否则按 [tool.fspack] python-versions 逐个
    # 构建到 dist/py<ver>；未配置时单目标 (None, None)，行为与旧版一致
    targets = _resolve_build_targets(project, info, ns.py_version, target)
    from fspack.ui.console import console

    if len(targets) > 1:
        console.step(f"多版本打包：{len(targets)} 个 Python 版本")
    for index, (py_ver, dist_dir) in enumerate(targets, 1):
        if len(targets) > 1:
            console.step(f"[{index}/{len(targets)}] Python {py_ver} → {dist_dir}")
        build(
            project,
            mirror,
            py_ver,
            dist_dir=dist_dir,
            target=target,
            options=options,
            extra_index_urls=tuple(ns.extra_index_urls or ()),
            find_links=tuple(ns.find_links or ()),
            dry_run=ns.dry_run,
            log_file=log_file,
            log_format=log_format,
            profile=ProfileOptions(
                enabled=ns.profile,
                out=Path(ns.profile_out).resolve() if ns.profile_out else None,
                compare=ns.profile_compare,
            ),
            auto_clean=getattr(ns, "auto_clean", False),
        )


def _parse_target(value: str | None) -> Platform | None:
    """将 CLI 字符串转为 Platform 枚举，None 表示用当前平台."""
    if value is None:
        return None
    from fspack.core.platform import Platform

    if value == "windows":
        return Platform.WINDOWS
    if value == "macos":
        return Platform.MACOS
    return Platform.LINUX


def _parse_lazy_imports(cli_value: str | None, base: tuple[str, ...]) -> tuple[str, ...]:
    """解析 ``--lazy-import`` 逗号分隔字符串为模块名元组.

    ``cli_value`` 为 None 时用配置默认 ``base``；为空字符串时返回空元组（用户
    显式清除）；非空时按逗号分割并去空白、去重。CLI 完全覆盖配置默认（与 extras
    语义一致，非合并）。
    """
    if cli_value is None:
        return base
    parts = [s.strip() for s in cli_value.split(",") if s.strip()]
    return tuple(dict.fromkeys(parts))
