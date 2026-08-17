"""安装包生成基类与通用编排：Windows NSIS / Linux tar.gz + .deb / macOS .pkg + .dmg / 跨平台 zip 便携包.

本模块原为 ``installer.py`` facade，子包化后保留：
- :class:`Installer` 抽象基类与通用编排流程（``build()`` → 校验 → ``build_package``）
- 公共辅助：``_run_stage``/``_prepare_dist``/``_check_exe``/``_release_base`` 等
- 发行包调度：``_resolve_formats``/``build_release``（按 ``--format`` 调度多格式生成）
- 函数式 API：``build_installer``/``build_linux_installer``（委托子类）

facade 在 :mod:`fspack.packaging.installer` 包 ``__init__.py``，re-export 本模块与平台子类。
平台专属实现拆分到子模块：
- :mod:`fspack.packaging.installer.nsis`：NSIS 脚本生成与编译
- :mod:`fspack.packaging.installer.linux`：tar.gz 便携包与 .deb 安装包
- :mod:`fspack.packaging.installer.macos`：.pkg 安装包与 .dmg 磁盘镜像
- :mod:`fspack.packaging.installer.zip`：跨平台 zip 便携包

``build_release`` 按 ``--format`` 调度生成一种或多种格式产物：
``auto``（平台默认）/``zip``（跨平台便携包）/``nsis``（Windows 安装包）/
``tar.gz``（Linux 便携包）/``deb``（Linux 安装包）/``pkg``（macOS 安装包）/
``dmg``（macOS 磁盘镜像）/``all``（平台全部）。
"""

from __future__ import annotations

import abc
import logging
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from fspack.builder import (  # noqa: F401  # build 供 __init__.py re-export，base.py 内部经 _facade.build 调用
    build,
    resolve_project_info,
)
from fspack.config import MirrorConfig, ProjectInfo, build_options_from_defaults
from fspack.console import console
from fspack.exceptions import InstallerError
from fspack.platform import Platform, detect_platform
from fspack.progress import BuildTracker, spinner

__all__ = [
    "Installer",
    "LinuxInstaller",
    "MacInstaller",
    "NsisInstaller",
    "build_deb",
    "build_deb_release",
    "build_dmg",
    "build_dmg_release",
    "build_installer",
    "build_linux_installer",
    "build_mac_installer",
    "build_pkg",
    "build_pkg_release",
    "build_release",
    "build_tarball",
    "build_tarball_release",
    "build_zip",
    "compile_installer",
    "generate_nsis_script",
]

_logger = logging.getLogger(__name__)


# 发行包格式取值校验
_VALID_FORMATS = ("auto", "zip", "nsis", "tar.gz", "deb", "pkg", "dmg", "all")

_T = TypeVar("_T")


def _run_stage(
    tracker: BuildTracker,
    name: str,
    fn: Callable[[], _T],
    *,
    detail: str = "",
) -> _T:
    """执行单阶段并用 ``tracker.stage`` 包装，同时显示 ``console.step`` 实时反馈。

    打包阶段（生成脚本/编译安装包/打 zip 等）统一用此函数包装，确保耗时与项数
    进入 ``BuildTracker`` 汇总表。``console.step`` 提供实时反馈，``tracker.stage``
    累积统计数据，两者职责分离不冲突。
    """
    with tracker.stage(name) as st:
        with spinner(name):
            result = fn()
        st.processed()
        if detail:
            st.set_detail(detail)
    return result


def _run_tool(
    cmd: list[str],
    *,
    not_found_msg: str,
    fail_prefix: str,
    cwd: Path | None = None,
    produces: Path | None = None,
) -> None:
    """执行外部命令行工具，统一异常处理与产物校验，失败抛 :class:`InstallerError`.

    汇聚 dpkg-deb / gpg / makensis / signtool / pkgbuild / hdiutil / codesign
    等外部工具调用的相同 try/except 骨架：``FileNotFoundError`` 转 ``not_found_msg``
    （工具未安装），``CalledProcessError`` 转 ``{fail_prefix}:\\n{stderr}``（执行失败）。

    Args:
        cmd: 命令与参数列表（如 ``["dpkg-deb", "--build", ...]``）
        not_found_msg: 工具未找到时的完整异常消息（含安装建议）
        fail_prefix: 命令执行失败时异常消息前缀（后接 ``:\\n<stderr>``）
        cwd: 子进程工作目录（如 makensis 需在 .nsi 所在目录执行），``None`` 时继承当前目录
        produces: 命令应产出的文件路径，非 ``None`` 时命令成功后校验其存在，
            缺失抛 ``InstallerError``（makensis 静默失败兜底）

    Raises:
        InstallerError: 工具未找到、命令返回非零、或 ``produces`` 声明的产物缺失
    """
    _logger.info("执行: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace", cwd=cwd)
    except FileNotFoundError as e:
        raise InstallerError(not_found_msg) from e
    except subprocess.CalledProcessError as e:
        raise InstallerError(f"{fail_prefix}:\n{e.stderr or e.stdout}") from e
    if produces is not None and not produces.is_file():
        raise InstallerError(f"{cmd[0]} 未产出安装包: {produces}")


def _make_staged_archive(  # noqa: PLR0913
    dist_dir: Path,
    release_dir: Path,
    base: str,
    fmt: str,
    *,
    keep_staging: bool = False,
    reuse_staging: bool = False,
) -> Path:
    """将 dist 复制到 ``release_dir/<base>`` staging 目录后打包为归档，返回归档路径.

    汇聚 tar.gz（``linux.build_tarball``）与 zip（``zip._make_zip``）逐行相同的打包流程：
    创建 release_dir → 清理旧 staging → ``copytree(ignore=_DIST_IGNORE)`` →
    ``make_archive`` → 清理 staging。归档顶层目录为 ``<base>``，解压后即可运行；
    排除 ``release/`` 与构建中间文件（见 :data:`_DIST_IGNORE`）避免递归打包自身。

    Args:
        dist_dir: 待打包的 dist 目录
        release_dir: 归档输出目录（同时用作 staging 父目录）
        base: 归档基础名与内顶层目录名（如 ``<name>-<version>-<py_tag>-<platform>-slim``）
        fmt: ``shutil.make_archive`` 格式，``"gztar"``（tar.gz）或 ``"zip"``
        keep_staging: 打包后保留 staging 目录（供后续格式复用，消除重复全量
            copytree；调用方须在最后一个复用格式完成后清理）
        reuse_staging: 复用已存在的 staging 目录（跳过 copytree）；staging 不存在
            时回退到正常 copytree 流程（防御前序格式未产出的场景）

    Returns:
        生成的归档文件路径
    """
    staging = release_dir / base
    if not (reuse_staging and staging.is_dir()):
        release_dir.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(dist_dir, staging, ignore=_DIST_IGNORE)
    archive = shutil.make_archive(str(release_dir / base), fmt, root_dir=release_dir, base_dir=base)
    if not keep_staging:
        shutil.rmtree(staging)
    return Path(archive)


# ---- 关于 *_release 编排骨架抽取的 TODO ----
# 已实现的抽取：_run_tool / _make_staged_archive / _DIST_IGNORE / _DIST_INTERMEDIATE_EXCLUDES
# （前四项收益最大、风险最低、monkeypatch 无影响）。
#
# 剩余抽取候选：5 个 *_release（tarball/deb/pkg/dmg/zip）骨架高度重复：
#   own_tracker → BuildTracker → _prepare_dist → _check_exe → release/ → _run_stage →
#   console.success → (可选 codesign/sign_deb 钩子) → own_tracker 时 summary
#
# 暂不合并原因（待后续迭代复核）：
# 1) *_release 有 15+ 个 monkeypatch 测试断言，统一后 patch 路径需从 linux/macos/zip
#    三处 "xxx._prepare_dist" / "xxx.BuildTracker" 等改向 base._single_format_release，
#    涉及 test_installer/test_linux_installer/test_macos_installer 大量路径迁移。
# 2) deb 有 sign_deb、macos 有 codesign 的可选钩子，合并需设计 hook 参数；现阶段逐个
#    声明可读性更好。
# 3) Installer.build_installer 类方法与 *_release 函数式 API 同时存在，两者责任
#    边界尚未统一，过早合并引入抽象层混乱。
# ---- 基类 ----


class Installer(abc.ABC):
    """安装包生成器基类。

    封装通用编排流程：可选 ``build()`` → 校验可执行文件 → :meth:`build_package`。

    子类实现：
    - :meth:`target_platform`：目标平台（决定 ``build()`` 的 target 参数）
    - :meth:`exe_filename`：可执行文件名（Windows 为 ``<name>.exe``，Linux 为 ``<name>``）
    - :meth:`build_package`：生成具体安装包，返回产物路径
    """

    @classmethod
    @abc.abstractmethod
    def target_platform(cls) -> Platform:
        """目标平台。"""

    @classmethod
    @abc.abstractmethod
    def exe_filename(cls, info: ProjectInfo) -> str:
        """返回可执行文件名（用于校验已构建产物存在）。"""

    @classmethod
    @abc.abstractmethod
    def build_package(
        cls,
        dist_dir: Path,
        info: ProjectInfo,
        release_dir: Path,
        *,
        tracker: BuildTracker,
    ) -> Path:
        """生成安装包，返回产物路径。"""

    @classmethod
    def build_installer(  # noqa: PLR0913
        cls,
        project_dir: Path,
        mirror: MirrorConfig,
        py_version: str | None = None,
        no_build: bool = False,
        dist_dir: Path | None = None,
        *,
        tracker: BuildTracker | None = None,
        extras: Sequence[str] | None = None,
    ) -> Path:
        """编排：可选 build → 校验可执行文件 → build_package，返回安装包路径。

        ``extras`` 为 CLI ``--extra`` 透传的分组名列表，``None`` 时用
        ``[tool.fspack] extras`` 配置默认；非 ``None`` 时完全覆盖配置默认
        （集合语义，与 ``build`` 子命令一致）。仅在需要重新构建时生效，
        dist 已就绪时复用构建结果，extras 不再生效。
        """
        own_tracker = tracker is None
        tk = tracker or BuildTracker(title="打包阶段汇总")
        dist, info = _prepare_dist(
            project_dir, mirror, py_version, no_build, dist_dir, cls.target_platform(), extras=extras, tracker=tk
        )
        exe = dist / cls.exe_filename(info)
        if not exe.is_file():
            raise InstallerError(f"未找到已构建的可执行文件: {exe}（请先执行 fsp b）")
        release = dist / "release"
        result = cls.build_package(dist, info, release, tracker=tk)
        if own_tracker:
            console.rich.print(tk.summary())
        return result


def _prepare_dist(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None,
    no_build: bool,
    dist_dir: Path | None,
    target: Platform,
    *,
    extras: Sequence[str] | None = None,
    tracker: BuildTracker | None = None,
) -> tuple[Path, ProjectInfo]:
    """通用编排：可选 ``build()`` 构建项目到 dist，返回 ``(dist_dir, info)``。

    跳过 build 的两种情况：

    - ``no_build=True``：用户显式声明 dist 已就绪，dist 目录缺失时报错
    - ``no_build=False``（默认）：dist 存在且可执行文件就绪时复用，避免 ``fsp b``
      后 ``fsp p`` 重复构建（尤其 Nuitka 编译耗时较长）；dist 或可执行文件缺失时
      调用 :func:`build` 重建，并透传 ``[tool.fspack]`` 配置的构建默认值

    ``extras`` 为 CLI ``--extra`` 透传的分组名，``None`` 时用配置默认；
    非 ``None`` 时覆盖 ``BuildOptions.extras``，仅在重新构建时生效。

    ``tracker`` 提供时将项目解析与 extras 信息作为「准备项目」阶段记入打包汇总表，
    便于在 ``fsp p`` 汇总中看到启用的 extras 分组。

    不校验可执行文件存在（由调用方按平台 ``exe_filename`` 自行校验）。
    """
    project_dir = Path(project_dir).resolve()
    dist = dist_dir or project_dir / "dist"

    def _resolve_and_maybe_build() -> tuple[Path, ProjectInfo]:
        if no_build:
            if not dist.is_dir():
                raise InstallerError(f"未找到 dist 目录: {dist}（请先执行 fsp b）")
            info = resolve_project_info(project_dir, py_version, target)
            return dist, info
        # no_build=False：dist+exe 已就绪则复用，避免 fsp b 后 fsp p 重复构建
        info = resolve_project_info(project_dir, py_version, target)
        if dist.is_dir() and _exe_exists(dist, info, target):
            return dist, info
        options = build_options_from_defaults(info.build_defaults)
        if extras is not None:
            options = replace(options, extras=frozenset(extras))
        # 经 _facade 查找 build：兼容测试 monkeypatch "fspack.packaging.installer.build"
        info = _facade.build(project_dir, mirror, py_version, dist_dir=dist, target=target, options=options)
        return dist, info

    if tracker is None:
        return _resolve_and_maybe_build()

    # tracker 提供时记入「准备项目」阶段，detail 含 extras 信息
    with tracker.stage("准备项目") as st:
        dist_ret, info_ret = _resolve_and_maybe_build()
        detail = f"{info_ret.name} {info_ret.version} ({info_ret.app_type.value})"
        resolved_extras = frozenset(extras) if extras is not None else info_ret.build_defaults.extras
        if resolved_extras:
            detail += f" | extras: {', '.join(sorted(resolved_extras))}"
        st.set_detail(detail)
    return dist_ret, info_ret


def _exe_path(info: ProjectInfo, target: Platform) -> str:
    """返回目标平台期望的可执行文件名（Windows 为 ``<name>.exe``，Linux 为 ``<name>``）。"""
    return info.exe_name if target is Platform.WINDOWS else info.name


def _exe_exists(dist: Path, info: ProjectInfo, target: Platform) -> bool:
    """判断 dist 内可执行文件是否就绪（用于 ``_prepare_dist`` 决定是否跳过 build）。"""
    return (dist / _exe_path(info, target)).is_file()


def _check_exe(dist: Path, info: ProjectInfo, target: Platform) -> None:
    """校验已构建的可执行文件存在（Windows 为 <name>.exe，Linux 为 <name>）。"""
    if not _exe_exists(dist, info, target):
        raise InstallerError(f"未找到已构建的可执行文件: {dist / _exe_path(info, target)}（请先执行 fsp b）")


def _py_tag(info: ProjectInfo) -> str:
    """返回 Python 完整版本标签，如 ``py3.11.9``，用于发行包文件名标识运行时完整版本。"""
    return f"py{info.py_version}"


def _release_base(info: ProjectInfo, platform_suffix: str) -> str:
    """生成发行包基础名：``<name>-<version>-<py_tag>-<platform>-slim``。

    slim 标识体现 wheel 精简解压（slim_unpack 按需解压 + Qt 闭包），
    是 fspack 默认且唯一的打包策略，故始终体现在文件名中。
    """
    return f"{info.name}-{info.version}-{_py_tag(info)}-{platform_suffix}-slim"


# 构建中间文件/缓存文件，打包时排除（仅用于增量构建，对最终用户无用）.
# - .dep_cache.json: 依赖分析缓存（dist 根目录）
# - .nuitka_compile_stamp: Nuitka 编译 stamp（dist 根目录）
# - .pyc_stamp: pyc 预编译 stamp（dist 根目录）
# - *.build: Nuitka 临时构建目录（src 子目录下，--remove-output 仅成功时清理）
# - build: loader 编译工作目录（旧版残留，新版用 tempfile 自动清理，此处兜底）
_DIST_INTERMEDIATE_EXCLUDES: tuple[str, ...] = (
    ".dep_cache.json",
    ".nuitka_compile_stamp",
    ".pyc_stamp",
    "*.build",
    "build",
)


# 便携包/安装包打包排除模式：release 目录 + 构建中间文件（与 NSIS /x 排除一致）。
# tar.gz / zip / .deb / .pkg / .dmg 五处 staging 复制共用，避免递归打包自身与残留中间文件。
_DIST_IGNORE = shutil.ignore_patterns("release", *_DIST_INTERMEDIATE_EXCLUDES)


# ---- 函数式 API（委托给子类）----


def build_installer(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    *,
    tracker: BuildTracker | None = None,
    extras: Sequence[str] | None = None,
) -> Path:
    """编排：可选 build → 生成 NSIS 脚本 → 编译安装包，返回安装包路径。"""
    return NsisInstaller.build_installer(
        project_dir, mirror, py_version, no_build=no_build, dist_dir=dist_dir, tracker=tracker, extras=extras
    )


def build_linux_installer(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    *,
    tracker: BuildTracker | None = None,
    extras: Sequence[str] | None = None,
) -> Path:
    """编排：可选 build → tar.gz 便携包 → .deb 安装包，返回 .deb 路径。"""
    return LinuxInstaller.build_installer(
        project_dir, mirror, py_version, no_build=no_build, dist_dir=dist_dir, tracker=tracker, extras=extras
    )


# ---- 调度：按 --format 选择生成哪些格式 ----


def _resolve_formats(fmt: str, target: Platform) -> list[str]:
    """将 ``--format`` 取值解析为具体格式列表。

    - ``auto``：平台默认（Windows=nsis，Linux=tar.gz+deb，macOS=pkg+dmg），向后兼容
    - ``all``：平台全部（Windows=nsis+zip，Linux=tar.gz+deb+zip，macOS=pkg+dmg+zip）
    - 单一格式：校验平台兼容性（nsis 仅 Windows，tar.gz/deb 仅 Linux，
      pkg/dmg 仅 macOS，zip 跨平台）
    """
    if fmt not in _VALID_FORMATS:
        raise InstallerError(f"未知 --format 取值: {fmt}，可选: {', '.join(_VALID_FORMATS)}")
    # auto / all 按平台查表
    platform_defaults: dict[Platform, tuple[list[str], list[str]]] = {
        Platform.WINDOWS: (["nsis"], ["nsis", "zip"]),
        Platform.MACOS: (["pkg", "dmg"], ["pkg", "dmg", "zip"]),
        Platform.LINUX: (["tar.gz", "deb"], ["tar.gz", "deb", "zip"]),
    }
    defaults, all_formats = platform_defaults[target]
    if fmt == "auto":
        return defaults
    if fmt == "all":
        return all_formats
    # 单一格式：校验平台兼容性
    if fmt == "nsis" and target is not Platform.WINDOWS:
        raise InstallerError("NSIS 安装包仅支持 Windows 目标")
    if fmt in ("tar.gz", "deb") and target is not Platform.LINUX:
        raise InstallerError(f"{fmt} 格式仅支持 Linux 目标")
    if fmt in ("pkg", "dmg") and target is not Platform.MACOS:
        raise InstallerError(f"{fmt} 格式仅支持 macOS 目标")
    return [fmt]


def build_release(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    target: Platform | None = None,
    fmt: str = "auto",
    codesign: bool = False,
    extras: Sequence[str] | None = None,
    sign_exe: bool = False,
    sign_exe_certificate: Path | None = None,
    sign_exe_password: str | None = None,
    sign_deb: bool = False,
    sign_deb_key: str | None = None,
) -> list[Path]:
    """按 ``--format`` 调度生成发行包，返回产物路径列表。

    多格式时按 ``_resolve_formats`` 顺序逐个生成，每次复用同一 dist（``no_build=True``
    内部触发第一次 build，后续格式跳过 build 直接打包）。返回的列表顺序与生成顺序一致。

    所有格式共享同一 ``BuildTracker``，最终统一渲染「打包阶段汇总」表（与 ``build()``
    的「构建阶段汇总」对应）。单格式函数（``build_zip`` 等）单独调用时各自渲染汇总表。

    Args:
        codesign: macOS 产物是否做 ad-hoc 签名（``codesign --sign -``），仅对
            ``pkg``/``dmg`` 格式生效，其他平台忽略
        extras: 启用的 ``[project.optional-dependencies]`` 分组名，``None`` 时用
            ``[tool.fspack] extras`` 配置默认；非 ``None`` 时覆盖配置默认。
            仅在需要重新构建时生效（dist 不存在或 ``no_build=False`` 且 dist 未就绪）
        sign_exe: Windows 产物是否做代码签名（signtool），需配合 ``sign_exe_certificate``
        sign_exe_certificate: Windows 代码签名 PFX 证书路径
        sign_exe_password: Windows 代码签名 PFX 证书密码
        sign_deb: Linux .deb 是否做 GPG 分离签名
        sign_deb_key: Linux .deb GPG 签名密钥 ID
    """
    resolved_target = target or detect_platform()
    formats = _resolve_formats(fmt, resolved_target)
    tracker = BuildTracker(title="打包阶段汇总")
    # Linux all 场景 tar.gz 与 zip 的 staging/顶层目录同名（<base>）：tar.gz 打包后
    # 保留 staging、zip 直接复用，消除一次 dist 全量 copytree（tar/zip 仍各自 make_archive）
    share_staging = resolved_target is Platform.LINUX and "tar.gz" in formats and "zip" in formats
    outputs: list[Path] = []
    for index, f in enumerate(formats):
        # 首个格式负责 build，后续格式 no_build=True 复用同一 dist
        skip_build = no_build or index > 0
        # extras 仅在首个格式（可能触发 build）透传，后续格式复用 dist 无需 extras
        format_extras = extras if index == 0 else None
        if f == "zip":
            outputs.append(
                _facade.build_zip(
                    project_dir,
                    mirror,
                    py_version,
                    no_build=skip_build,
                    dist_dir=dist_dir,
                    target=resolved_target,
                    tracker=tracker,
                    extras=format_extras,
                    reuse_staging=share_staging,
                )
            )
        elif f == "nsis":
            outputs.append(
                NsisInstaller.build_installer(
                    project_dir,
                    mirror,
                    py_version,
                    no_build=skip_build,
                    dist_dir=dist_dir,
                    tracker=tracker,
                    extras=format_extras,
                    sign_exe=sign_exe,
                    sign_exe_certificate=sign_exe_certificate,
                    sign_exe_password=sign_exe_password,
                )
            )
        elif f == "tar.gz":
            outputs.append(
                _facade.build_tarball_release(
                    project_dir,
                    mirror,
                    py_version,
                    no_build=skip_build,
                    dist_dir=dist_dir,
                    tracker=tracker,
                    extras=format_extras,
                    keep_staging=share_staging,
                )
            )
        elif f == "deb":
            outputs.append(
                _facade.build_deb_release(
                    project_dir,
                    mirror,
                    py_version,
                    no_build=skip_build,
                    dist_dir=dist_dir,
                    tracker=tracker,
                    extras=format_extras,
                    sign_deb=sign_deb,
                    sign_deb_key=sign_deb_key,
                )
            )
        elif f == "pkg":
            outputs.append(
                _facade.build_pkg_release(
                    project_dir,
                    mirror,
                    py_version,
                    no_build=skip_build,
                    dist_dir=dist_dir,
                    tracker=tracker,
                    codesign=codesign,
                    extras=format_extras,
                )
            )
        elif f == "dmg":
            outputs.append(
                _facade.build_dmg_release(
                    project_dir,
                    mirror,
                    py_version,
                    no_build=skip_build,
                    dist_dir=dist_dir,
                    tracker=tracker,
                    codesign=codesign,
                    extras=format_extras,
                )
            )
    console.rich.print(tracker.summary())
    return outputs


# ---- 子模块 re-export（末尾导入避免循环依赖）----
# 子模块从本模块导入 Installer 基类与公共辅助，故须在所有定义之后导入

# 通过 facade 解析可 patch 函数：兼容测试 monkeypatch "fspack.packaging.installer.build"
# 等函数路径。base.py 顶层 import 的 build/build_zip 等为原始引用，测试 patch 指向
# :mod:`fspack.packaging.installer`（__init__.py），故 ``_prepare_dist``/``build_release``
# 内部调用经 ``_facade.<fn>`` 在运行时动态查找，使 patch 生效。
import fspack.packaging.installer as _facade  # noqa: E402
from fspack.packaging.installer.linux import (  # noqa: E402
    LinuxInstaller,
    build_deb,
    build_deb_release,
    build_tarball,
    build_tarball_release,
)
from fspack.packaging.installer.macos import (  # noqa: E402
    MacInstaller,
    build_dmg,
    build_dmg_release,
    build_mac_installer,
    build_pkg,
    build_pkg_release,
)
from fspack.packaging.installer.nsis import (  # noqa: E402
    NsisInstaller,
    compile_installer,
    generate_nsis_script,
)
from fspack.packaging.installer.zip import (  # noqa: E402,F401 # 测试通过 fspack.packaging.installer._make_zip 访问
    _make_zip,
    build_zip,
)
