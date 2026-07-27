"""安装包生成 facade：Windows NSIS / Linux tar.gz + .deb / 跨平台 zip 便携包。

本模块为 facade，保留：
- :class:`Installer` 抽象基类与通用编排流程（``build()`` → 校验 → ``build_package``）
- 公共辅助：``_run_stage``/``_prepare_dist``/``_check_exe``/``_release_base`` 等
- 发行包调度：``_resolve_formats``/``build_release``（按 ``--format`` 调度多格式生成）
- 函数式 API：``build_installer``/``build_linux_installer``（委托子类）

平台专属实现拆分到子模块：
- :mod:`fspack.packaging.installer_nsis`：NSIS 脚本生成与编译
- :mod:`fspack.packaging.installer_linux`：tar.gz 便携包与 .deb 安装包
- :mod:`fspack.packaging.installer_zip`：跨平台 zip 便携包

``build_release`` 按 ``--format`` 调度生成一种或多种格式产物：
``auto``（平台默认）/``zip``（跨平台便携包）/``nsis``（Windows 安装包）/
``tar.gz``（Linux 便携包）/``deb``（Linux 安装包）/``all``（平台全部）。
"""

from __future__ import annotations

import abc
import logging
import subprocess  # noqa: F401 # 测试 monkeypatch 通过 fspack.packaging.installer.subprocess.run 访问
from pathlib import Path
from typing import Callable, TypeVar

from fspack.builder import build, resolve_project_info
from fspack.config import MirrorConfig, ProjectInfo, build_options_from_defaults
from fspack.console import console
from fspack.exceptions import InstallerError
from fspack.platform import Platform, detect_platform
from fspack.progress import BuildTracker, spinner

__all__ = [
    "Installer",
    "LinuxInstaller",
    "NsisInstaller",
    "build_deb",
    "build_deb_release",
    "build_installer",
    "build_linux_installer",
    "build_release",
    "build_tarball",
    "build_tarball_release",
    "build_zip",
    "compile_installer",
    "generate_nsis_script",
]

_logger = logging.getLogger(__name__)


# 发行包格式取值校验
_VALID_FORMATS = ("auto", "zip", "nsis", "tar.gz", "deb", "all")

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
    ) -> Path:
        """编排：可选 build → 校验可执行文件 → build_package，返回安装包路径。"""
        own_tracker = tracker is None
        tk = tracker or BuildTracker(title="打包阶段汇总")
        dist, info = _prepare_dist(project_dir, mirror, py_version, no_build, dist_dir, cls.target_platform())
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
) -> tuple[Path, ProjectInfo]:
    """通用编排：可选 ``build()`` 构建项目到 dist，返回 ``(dist_dir, info)``。

    跳过 build 的两种情况：

    - ``no_build=True``：用户显式声明 dist 已就绪，dist 目录缺失时报错
    - ``no_build=False``（默认）：dist 存在且可执行文件就绪时复用，避免 ``fsp b``
      后 ``fsp p`` 重复构建（尤其 Nuitka 编译耗时较长）；dist 或可执行文件缺失时
      调用 :func:`build` 重建，并透传 ``[tool.fspack]`` 配置的构建默认值

    不校验可执行文件存在（由调用方按平台 ``exe_filename`` 自行校验）。
    """
    project_dir = Path(project_dir).resolve()
    dist = dist_dir or project_dir / "dist"
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
    info = build(project_dir, mirror, py_version, dist_dir=dist, target=target, options=options)
    return dist, info


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


# ---- 函数式 API（委托给子类）----


def build_installer(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    *,
    tracker: BuildTracker | None = None,
) -> Path:
    """编排：可选 build → 生成 NSIS 脚本 → 编译安装包，返回安装包路径。"""
    return NsisInstaller.build_installer(
        project_dir, mirror, py_version, no_build=no_build, dist_dir=dist_dir, tracker=tracker
    )


def build_linux_installer(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    *,
    tracker: BuildTracker | None = None,
) -> Path:
    """编排：可选 build → tar.gz 便携包 → .deb 安装包，返回 .deb 路径。"""
    return LinuxInstaller.build_installer(
        project_dir, mirror, py_version, no_build=no_build, dist_dir=dist_dir, tracker=tracker
    )


# ---- 调度：按 --format 选择生成哪些格式 ----


def _resolve_formats(fmt: str, target: Platform) -> list[str]:
    """将 ``--format`` 取值解析为具体格式列表。

    - ``auto``：平台默认（Windows=nsis，Linux=tar.gz+deb），向后兼容
    - ``all``：平台全部（Windows=nsis+zip，Linux=tar.gz+deb+zip）
    - 单一格式：校验平台兼容性（nsis 仅 Windows，tar.gz/deb 仅 Linux，zip 跨平台）
    """
    if fmt not in _VALID_FORMATS:
        raise InstallerError(f"未知 --format 取值: {fmt}，可选: {', '.join(_VALID_FORMATS)}")
    if fmt == "auto":
        return ["nsis"] if target is Platform.WINDOWS else ["tar.gz", "deb"]
    if fmt == "all":
        return ["nsis", "zip"] if target is Platform.WINDOWS else ["tar.gz", "deb", "zip"]
    if fmt == "nsis" and target is not Platform.WINDOWS:
        raise InstallerError("NSIS 安装包仅支持 Windows 目标")
    if fmt in ("tar.gz", "deb") and target is not Platform.LINUX:
        raise InstallerError(f"{fmt} 格式仅支持 Linux 目标")
    return [fmt]


def build_release(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    target: Platform | None = None,
    fmt: str = "auto",
) -> list[Path]:
    """按 ``--format`` 调度生成发行包，返回产物路径列表。

    多格式时按 ``_resolve_formats`` 顺序逐个生成，每次复用同一 dist（``no_build=True``
    内部触发第一次 build，后续格式跳过 build 直接打包）。返回的列表顺序与生成顺序一致。

    所有格式共享同一 ``BuildTracker``，最终统一渲染「打包阶段汇总」表（与 ``build()``
    的「构建阶段汇总」对应）。单格式函数（``build_zip`` 等）单独调用时各自渲染汇总表。
    """
    resolved_target = target or detect_platform()
    formats = _resolve_formats(fmt, resolved_target)
    tracker = BuildTracker(title="打包阶段汇总")
    outputs: list[Path] = []
    for index, f in enumerate(formats):
        # 首个格式负责 build，后续格式 no_build=True 复用同一 dist
        skip_build = no_build or index > 0
        if f == "zip":
            outputs.append(
                build_zip(
                    project_dir,
                    mirror,
                    py_version,
                    no_build=skip_build,
                    dist_dir=dist_dir,
                    target=resolved_target,
                    tracker=tracker,
                )
            )
        elif f == "nsis":
            outputs.append(
                NsisInstaller.build_installer(
                    project_dir, mirror, py_version, no_build=skip_build, dist_dir=dist_dir, tracker=tracker
                )
            )
        elif f == "tar.gz":
            outputs.append(
                build_tarball_release(
                    project_dir, mirror, py_version, no_build=skip_build, dist_dir=dist_dir, tracker=tracker
                )
            )
        elif f == "deb":
            outputs.append(
                build_deb_release(
                    project_dir, mirror, py_version, no_build=skip_build, dist_dir=dist_dir, tracker=tracker
                )
            )
    console.rich.print(tracker.summary())
    return outputs


# ---- 子模块 re-export（末尾导入避免循环依赖）----
# 子模块从本模块导入 Installer 基类与公共辅助，故须在所有定义之后导入

from fspack.packaging.installer_linux import (  # noqa: E402
    LinuxInstaller,
    build_deb,
    build_deb_release,
    build_tarball,
    build_tarball_release,
)
from fspack.packaging.installer_nsis import (  # noqa: E402
    NsisInstaller,
    compile_installer,
    generate_nsis_script,
)
from fspack.packaging.installer_zip import (  # noqa: E402,F401 # 测试通过 fspack.packaging.installer._make_zip 访问
    _make_zip,
    build_zip,
)
