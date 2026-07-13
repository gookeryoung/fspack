"""构建流水线编排：解析 → embed → 依赖 → 源码 → loader。."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Sequence

from fspack.analyzer import analyze_dependencies
from fspack.config import BuildConfig, MirrorConfig, ProjectInfo
from fspack.embed import ensure_embed, write_pth
from fspack.exceptions import DependencyError
from fspack.loader import compile_loader, generate_loader_source
from fspack.platform import Platform, detect_platform, wheel_platform_tags
from fspack.project import DEFAULT_PY_VERSION, parse_project
from fspack.standalone import STANDALONE_RELEASE_TAG, ensure_standalone

__all__ = ["DEFAULT_PY_VERSION", "build", "copy_source", "download_wheels", "unpack_wheels"]

_logger = logging.getLogger(__name__)

_EXCLUDE = shutil.ignore_patterns(
    "dist",
    "build",
    ".git",
    "__pycache__",
    "*.egg-info",
    ".venv",
    ".tox",
    ".fspack",
    ".trae",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
)


def build(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str = DEFAULT_PY_VERSION,
    dist_dir: Path | None = None,
    embed_cache: Path | None = None,
    target: Platform | None = None,
) -> ProjectInfo:
    """执行完整构建流水线，返回项目信息。."""
    project_dir = Path(project_dir).resolve()
    target = target or detect_platform()
    dist = dist_dir or project_dir / "dist"
    cache = embed_cache or Path.home() / ".fspack" / "cache" / "embed"
    cfg = BuildConfig(project_dir=project_dir, dist_dir=dist, embed_cache_dir=cache, mirror=mirror, target=target)
    info = parse_project(project_dir, py_version)
    _logger.info("项目: %s %s (%s) 目标: %s", info.name, info.version, info.app_type.value, target.value)

    runtime_dir = cfg.dist_dir / "runtime"
    if target is Platform.LINUX:
        standalone_cache = Path.home() / ".fspack" / "cache" / "standalone"
        ensure_standalone(info.py_version, STANDALONE_RELEASE_TAG, standalone_cache, runtime_dir)
        major, minor = info.py_version.split(".")[:2]
        site_packages = runtime_dir / "python" / "lib" / f"python{major}.{minor}" / "site-packages"
    else:
        ensure_embed(info.py_version, cfg.mirror, cfg.embed_cache_dir, runtime_dir)
        site_packages = runtime_dir / "Lib" / "site-packages"

    report = analyze_dependencies(project_dir, info.name, info.dependencies)
    if report.missing:
        _logger.info("AST 发现未声明依赖: %s", ", ".join(report.missing))
    if report.ast_third_party:
        wheelhouse = cfg.dist_dir / "wheelhouse"
        download_wheels(
            report.ast_third_party,
            info.py_version,
            cfg.mirror.pypi_index,
            wheelhouse,
            platform_tags=wheel_platform_tags(target),
        )
        unpack_wheels(wheelhouse, site_packages)
    else:
        _logger.info("无第三方依赖，跳过 wheel 下载")

    if target is Platform.WINDOWS:
        write_pth(cfg.dist_dir, info.py_version)
    src_dst = cfg.dist_dir / "src"
    copy_source(project_dir, src_dst)

    entry_rel = info.entry_file.relative_to(info.src_dir).as_posix()
    source = generate_loader_source(f"src/{entry_rel}", info.py_xy, target)
    exe_name = info.exe_name if target is Platform.WINDOWS else info.name
    exe = cfg.dist_dir / exe_name
    compile_loader(source, exe, info.app_type, cfg.dist_dir / "build", target)
    _logger.info("构建完成: %s", exe)
    return info


def copy_source(project_dir: Path, src_dst: Path) -> None:
    """将项目源码复制到 dist/src，排除构建产物与缓存。."""
    if src_dst.exists():
        shutil.rmtree(src_dst)
    shutil.copytree(project_dir, src_dst, ignore=_EXCLUDE)


def download_wheels(
    packages: tuple[str, ...] | list[str],
    py_version: str,
    pypi_index: str,
    wheelhouse_dir: Path,
    platform_tags: Sequence[str] = ("win_amd64",),
) -> list[Path]:
    """用 dev python 的 pip 下载指定平台 wheel 到 wheelhouse 目录。

    ``platform_tags`` 为 pip ``--platform`` 标签列表，可重复指定以匹配多个
    平台标签（如 Linux 同时匹配 manylinux2014 与 manylinux_2_28）。
    """
    wheelhouse_dir.mkdir(parents=True, exist_ok=True)
    major, minor = py_version.split(".")[:2]
    platform_args: list[str] = []
    for tag in platform_tags:
        platform_args.extend(["--platform", tag])
    cmd: list[str] = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "-d",
        str(wheelhouse_dir),
        *platform_args,
        "--python-version",
        f"{major}.{minor}",
        "--abi",
        f"cp{major}{minor}",
        "--implementation",
        "cp",
        "--only-binary=:all:",
        "-i",
        pypi_index,
        *packages,
    ]
    _logger.info("下载依赖 wheel: %s", " ".join(packages))
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise DependencyError(f"未找到 pip: {sys.executable}") from e
    except subprocess.CalledProcessError as e:
        raise DependencyError(f"依赖下载失败:\n{e.stderr}") from e
    return sorted(wheelhouse_dir.glob("*.whl"))


def unpack_wheels(wheelhouse_dir: Path, site_packages_dir: Path) -> int:
    """将 wheelhouse 内所有 .whl 解包到 site-packages 目录，返回解包数量。."""
    site_packages_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for whl in wheelhouse_dir.glob("*.whl"):
        try:
            with zipfile.ZipFile(whl) as zf:
                zf.extractall(site_packages_dir)
        except zipfile.BadZipFile as e:
            raise DependencyError(f"wheel 损坏: {whl}") from e
        count += 1
    return count
