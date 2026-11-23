"""package 子命令：发行包构建与签名配置合并."""

from __future__ import annotations

import argparse
from pathlib import Path

from fspack.cli.commands.build import _parse_target, _resolve_build_targets, _resolve_mirror

__all__ = ["_run_package"]


def _run_package(project: Path, ns: argparse.Namespace) -> None:
    """执行单项目 package 子命令."""
    from fspack.config import ProjectInfo
    from fspack.exceptions import ProjectError
    from fspack.packaging.installer import ReleaseRequest, SignOptions, build_release

    # 项目信息：extras 校验、多版本目标解析与签名配置默认值共用（from_dir 有缓存）
    info = ProjectInfo.from_dir(project, ns.py_version)
    # extras 校验：CLI --extra 指定时校验未知分组（与 build 子命令一致）
    # 仅校验，不构造 BuildOptions——build_release 内部 _prepare_dist 用配置默认，
    # CLI 指定时通过 ReleaseRequest.extras 透传覆盖配置默认
    cli_extras = ns.extras if getattr(ns, "extras", None) else None
    if cli_extras:
        unknown = set(cli_extras) - set(info.optional_dependencies)
        if unknown:
            raise ProjectError(f"未知的 extras 分组: {sorted(unknown)}，可选: {sorted(info.optional_dependencies)}")

    # 安全加固签名：CLI 优先合并配置默认（与 extras 不同，签名证书/密钥
    # 用 CLI 优先 + 配置回退语义，避免 --sign-exe 显式开关与配置证书路径分离）
    info_for_sign = info if (ns.sign_exe or ns.sign_deb) else None
    cfg_cert = info_for_sign.build_defaults.sign_exe_certificate if info_for_sign else None
    sign_exe_cert = (
        Path(ns.sign_exe_certificate).resolve()
        if ns.sign_exe_certificate
        else (Path(cfg_cert).resolve() if cfg_cert else None)
    )
    sign_exe_pwd = (
        ns.sign_exe_password
        if ns.sign_exe_password is not None
        else (info_for_sign.build_defaults.sign_exe_password if info_for_sign else None)
    )
    sign_deb_key = (
        ns.sign_deb_key
        if ns.sign_deb_key is not None
        else (info_for_sign.build_defaults.sign_deb_key if info_for_sign else None)
    )
    target = _parse_target(ns.target)
    mirror = _resolve_mirror(ns.mirror)
    sign = SignOptions(
        codesign=ns.codesign,
        sign_exe=ns.sign_exe,
        sign_exe_certificate=sign_exe_cert,
        sign_exe_password=sign_exe_pwd,
        sign_deb=ns.sign_deb,
        sign_deb_key=sign_deb_key,
    )
    # 多版本：与 build 子命令同一套目标解析（--py-version 优先，其次
    # python-versions 配置），各版本从各自的 dist 目录打包，互不覆盖
    targets = _resolve_build_targets(project, info, ns.py_version, target)
    outputs: list[Path] = []
    for py_ver, dist_dir in targets:
        req = ReleaseRequest(
            project_dir=project,
            mirror=mirror,
            py_version=py_ver,
            no_build=ns.no_build,
            extras=cli_extras,
            dist_dir=dist_dir,
        )
        outputs.extend(build_release(req, target=target, fmt=ns.format, sign=sign))
    import logging

    for out in outputs:
        logging.getLogger("fspack.cli").info("发行包已生成: %s", out)
