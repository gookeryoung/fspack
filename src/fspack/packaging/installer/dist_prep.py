"""dist 准备与产物命名：``_prepare_dist`` 编排、exe 校验、发行包命名、staging 归档.

从 :mod:`fspack.packaging.installer.base` 拆分而来，聚集「构建前准备」职责：

- :func:`_prepare_dist`：可选 ``build()`` 构建项目到 dist（dist+exe 就绪则复用）
- ``_exe_path``/``_exe_exists``/``_check_exe``：目标平台可执行文件校验
- ``_py_tag``/``_release_base``：发行包文件名命名
- ``_DIST_INTERMEDIATE_EXCLUDES``/``_DIST_IGNORE``/``_make_staged_archive``：
  打包排除模式与 staging 归档（tar.gz / zip / .deb / .pkg / .dmg 共用）
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from pathlib import Path

from fspack.builder import resolve_project_info
from fspack.config import ProjectInfo, build_options_from_defaults
from fspack.exceptions import InstallerError
from fspack.packaging.installer.request import ReleaseRequest
from fspack.platform import Platform

__all__ = [
    "_DIST_IGNORE",
    "_DIST_INTERMEDIATE_EXCLUDES",
    "_check_exe",
    "_exe_exists",
    "_exe_path",
    "_make_staged_archive",
    "_prepare_dist",
    "_py_tag",
    "_release_base",
]

_logger = logging.getLogger(__name__)


def _prepare_dist(req: ReleaseRequest, target: Platform) -> tuple[Path, ProjectInfo]:
    """通用编排：可选 ``build()`` 构建项目到 dist，返回 ``(dist_dir, info)``。

    跳过 build 的两种情况：

    - ``req.no_build=True``：用户显式声明 dist 已就绪，dist 目录缺失时报错
    - ``req.no_build=False``（默认）：dist 存在且可执行文件就绪时复用，避免 ``fsp b``
      后 ``fsp p`` 重复构建（尤其 Nuitka 编译耗时较长）；dist 或可执行文件缺失时
      调用 :func:`build` 重建，并透传 ``[tool.fspack]`` 配置的构建默认值

    ``req.extras`` 为 CLI ``--extra`` 透传的分组名，``None`` 时用配置默认；
    非 ``None`` 时覆盖 ``BuildOptions.extras``，仅在重新构建时生效。

    ``req.tracker`` 提供时将项目解析与 extras 信息作为「准备项目」阶段记入打包
    汇总表，便于在 ``fsp p`` 汇总中看到启用的 extras 分组；``None`` 时直接执行
    不记阶段。

    不校验可执行文件存在（由调用方按平台 ``exe_filename`` 自行校验）。
    """
    project_dir = Path(req.project_dir).resolve()
    dist = req.dist_dir or project_dir / "dist"

    def _resolve_and_maybe_build() -> tuple[Path, ProjectInfo]:
        if req.no_build:
            if not dist.is_dir():
                raise InstallerError(f"未找到 dist 目录: {dist}（请先执行 fsp b）")
            info = resolve_project_info(project_dir, req.py_version, target)
            return dist, info
        # no_build=False：dist+exe 已就绪则复用，避免 fsp b 后 fsp p 重复构建
        info = resolve_project_info(project_dir, req.py_version, target)
        if dist.is_dir() and _exe_exists(dist, info, target):
            return dist, info
        options = build_options_from_defaults(info.build_defaults)
        if req.extras is not None:
            options = replace(options, extras=frozenset(req.extras))
        # 经 _facade 查找 build：兼容测试 monkeypatch "fspack.packaging.installer.build"
        info = _facade.build(project_dir, req.mirror, req.py_version, dist_dir=dist, target=target, options=options)
        return dist, info

    if req.tracker is None:
        return _resolve_and_maybe_build()

    # tracker 提供时记入「准备项目」阶段，detail 含 extras 信息
    with req.tracker.stage("准备项目") as st:
        dist_ret, info_ret = _resolve_and_maybe_build()
        detail = f"{info_ret.name} {info_ret.version} ({info_ret.app_type.value})"
        resolved_extras = frozenset(req.extras) if req.extras is not None else info_ret.build_defaults.extras
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

    slim 标识体现 wheel 精简解压（slimunpack 按需解压 + Qt 闭包），
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


# ---- 末尾导入避免循环依赖 ----
# 通过 facade 解析可 patch 函数：兼容测试 monkeypatch "fspack.packaging.installer.build"
# 路径。dist_prep 顶层 import 的 build 为原始引用，测试 patch 指向
# :mod:`fspack.packaging.installer`（__init__.py），故 ``_prepare_dist`` 内部调用经
# ``_facade.build`` 在运行时动态查找，使 patch 生效。
import fspack.packaging.installer as _facade  # noqa: E402
