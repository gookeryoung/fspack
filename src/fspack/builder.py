"""构建流水线编排：解析 → embed → 依赖 → 源码 → loader。."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from fspack.analyzer import analyze_dependencies
from fspack.config import BuildConfig, MirrorConfig, ProjectInfo
from fspack.console import success
from fspack.embed import ensure_embed, write_pth
from fspack.exceptions import DependencyError
from fspack.loader import compile_loader, generate_loader_source
from fspack.platform import Platform, detect_platform, wheel_platform_tags
from fspack.progress import BuildTracker, StageRecorder, spinner
from fspack.project import DEFAULT_LINUX_PY_VERSION, DEFAULT_PY_VERSION, parse_project, resolve_py_version
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

# Windows 系统标准命名为 python.exe；Microsoft Store 版本另提供 python3.exe stub。
# Linux/macOS 用 python3，回退 python。
_PIP_PYTHON_NAMES: tuple[str, ...] = ("python.exe", "python3.exe") if sys.platform == "win32" else ("python3", "python")


def build(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    dist_dir: Path | None = None,
    embed_cache: Path | None = None,
    target: Platform | None = None,
    keep_modules: set[str] | None = None,
) -> ProjectInfo:
    """执行完整构建流水线，返回项目信息。."""
    from fspack.console import console as rich_console

    tracker = BuildTracker()
    project_dir = Path(project_dir).resolve()
    target = target or detect_platform()
    dist = dist_dir or project_dir / "dist"
    cache = embed_cache or Path.home() / ".fspack" / "cache" / "embed"
    cfg = BuildConfig(project_dir=project_dir, dist_dir=dist, embed_cache_dir=cache, mirror=mirror, target=target)

    with tracker.stage("解析项目") as st:
        info = parse_project(project_dir, py_version)
        default_ver = DEFAULT_LINUX_PY_VERSION if target is Platform.LINUX else DEFAULT_PY_VERSION
        resolved = resolve_py_version(project_dir, py_version, info.requires_python, default_ver)
        if resolved != info.py_version:
            _logger.info("自动选择 Python 版本: %s", resolved)
            info = replace(info, py_version=resolved)
        _logger.info("项目: %s %s (%s) 目标: %s", info.name, info.version, info.app_type.value, target.value)
        st.set_detail(f"{info.name} {info.version} ({info.app_type.value})")

    runtime_dir = cfg.dist_dir / "runtime"
    with tracker.stage("准备运行时") as st:
        if target is Platform.LINUX:
            standalone_cache = Path.home() / ".fspack" / "cache" / "standalone"
            ensure_standalone(info.py_version, STANDALONE_RELEASE_TAG, standalone_cache, runtime_dir, stage=st)
            major, minor = info.py_version.split(".")[:2]
            site_packages = runtime_dir / "python" / "lib" / f"python{major}.{minor}" / "site-packages"
            st.set_detail("python-build-standalone")
        else:
            ensure_embed(info.py_version, cfg.mirror, cfg.embed_cache_dir, runtime_dir, stage=st)
            site_packages = runtime_dir / "Lib" / "site-packages"
            st.set_detail("embed python")

    with tracker.stage("分析依赖") as st:
        report = analyze_dependencies(project_dir, info.name, info.dependencies)
        if report.missing:
            _logger.info("AST 发现未声明依赖: %s", ", ".join(report.missing))
        ast_count = len(report.ast_third_party)
        st.processed(ast_count)
        st.set_detail(f"AST {ast_count} 个第三方")

    if report.ast_third_party:
        with tracker.stage("下载依赖") as st:
            wheelhouse = cfg.dist_dir / "wheelhouse"
            download_wheels(
                report.ast_third_party,
                info.py_version,
                cfg.mirror.pypi_index,
                wheelhouse,
                platform_tags=wheel_platform_tags(target),
                stage=st,
            )
            unpack_wheels(wheelhouse, site_packages, report.ast_submodules, keep_modules, stage=st)
    else:
        _logger.info("无第三方依赖，跳过 wheel 下载")

    if target is Platform.WINDOWS:
        write_pth(cfg.dist_dir, info.py_version)

    with tracker.stage("复制源码") as st:
        src_dst = cfg.dist_dir / "src"
        with spinner(f"复制 {info.name} 源码"):
            copy_source(project_dir, src_dst)

    with tracker.stage("生成 C loader") as st:
        entry_rel = info.entry_file.relative_to(info.src_dir).as_posix()
        source = generate_loader_source(f"src/{entry_rel}", info.py_xy, target)
        exe_name = info.exe_name if target is Platform.WINDOWS else info.name
        exe = cfg.dist_dir / exe_name
        compile_loader(source, exe, info.app_type, cfg.dist_dir / "build", target, stage=st)

    rich_console.print(tracker.summary())
    success(f"构建完成: {exe}")
    return info


def copy_source(project_dir: Path, src_dst: Path) -> None:
    """将项目源码复制到 dist/src，排除构建产物与缓存。."""
    if src_dst.exists():
        shutil.rmtree(src_dst)
    shutil.copytree(project_dir, src_dst, ignore=_EXCLUDE)


def _find_pip_python() -> str:
    """找一个能跑 ``python -m pip`` 的解释器。

    优先当前 venv（``sys.executable``），无 pip 时遍历 ``PATH`` 找系统 python
    （跳过 venv 所在目录，因为 ``shutil.which`` 在 venv 激活时只返回 venv python）。
    候选名按平台：Windows 为 ``python.exe``/``python3.exe``，其他为 ``python3``/``python``。
    ``pip download`` 的 ``--python-version``/``--abi``/``--implementation`` 参数
    支持跨版本下载，跑 pip 的 python 版本无需匹配目标版本。

    uv 管理的 venv 默认不含 pip（用 Rust 实现的 ``uv pip``），需回退系统 python。
    """
    candidates: list[str] = [sys.executable]
    venv_bin = Path(sys.executable).parent.resolve()
    seen: set[str] = {sys.executable}
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not path_dir:
            continue
        try:
            resolved_dir = Path(path_dir).resolve()
        except OSError:
            continue
        if resolved_dir == venv_bin:
            continue
        for name in _PIP_PYTHON_NAMES:
            candidate = resolved_dir / name
            if candidate.is_file() and str(candidate) not in seen:
                candidates.append(str(candidate))
                seen.add(str(candidate))
    for py in candidates:
        try:
            subprocess.run([py, "-m", "pip", "--version"], check=True, capture_output=True, text=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        return py
    raise DependencyError("未找到可用的 pip，请在当前 venv 执行 `uv pip install pip`，或在系统安装 python3-pip 包")


def download_wheels(  # noqa: PLR0913
    packages: tuple[str, ...] | list[str],
    py_version: str,
    pypi_index: str,
    wheelhouse_dir: Path,
    platform_tags: Sequence[str] = ("win_amd64",),
    wheel_cache_dir: Path | None = None,
    *,
    stage: StageRecorder | None = None,
) -> list[Path]:
    """用 dev python 的 pip 下载指定平台 wheel 到 wheelhouse 目录。

    ``platform_tags`` 为 pip ``--platform`` 标签列表，可重复指定以匹配多个
    平台标签（如 Linux 同时匹配 manylinux2014 与 manylinux_2_28）。

    ``wheel_cache_dir`` 为 fspack wheel 缓存目录，默认 ``~/.fspack/cache/wheels/``。
    构建前从 uv/pip 缓存收割匹配 wheel 到此目录，``pip download`` 追加
    ``--find-links`` 让 pip 优先用本地缓存，下载后回写缓存供后续复用。

    自动选择能跑 pip 的 python 解释器：优先当前 venv，回退系统 python3
    （uv venv 默认不含 pip）。

    ``stage`` 用于回写缓存命中数与 wheel 数到 BuildTracker。
    """
    from fspack.wheel_cache import fspack_wheel_cache_dir, harvest_external_caches, normalize_name, save_to_cache

    wheelhouse_dir.mkdir(parents=True, exist_ok=True)
    cache = wheel_cache_dir or fspack_wheel_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    pkg_set = {normalize_name(p) for p in packages}
    harvested = harvest_external_caches(pkg_set, py_version, platform_tags, cache)
    if harvested:
        _logger.info("从外部缓存收割 %d 个 wheel", harvested)
        if stage is not None:
            stage.hit_cache(harvested)

    py = _find_pip_python()
    major, minor = py_version.split(".")[:2]
    platform_args: list[str] = []
    for tag in platform_tags:
        platform_args.extend(["--platform", tag])
    cmd: list[str] = [
        py,
        "-m",
        "pip",
        "download",
        "-d",
        str(wheelhouse_dir),
        "--find-links",
        str(cache),
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
        with spinner(f"pip download {len(packages)} 个依赖"):
            subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise DependencyError(f"未找到 pip: {py}") from e
    except subprocess.CalledProcessError as e:
        raise DependencyError(f"依赖下载失败:\n{e.stderr}") from e

    wheels = sorted(wheelhouse_dir.glob("*.whl"))
    saved = save_to_cache(wheels, cache)
    if saved:
        _logger.info("缓存 %d 个 wheel", saved)
    if stage is not None:
        stage.processed(len(wheels))
        stage.set_detail(f"{len(wheels)} wheels")
    return wheels


def unpack_wheels(
    wheelhouse_dir: Path,
    site_packages_dir: Path,
    submodule_usage: dict[str, frozenset[str]] | None = None,
    keep_modules: set[str] | None = None,
    *,
    stage: StageRecorder | None = None,
) -> int:
    """将 wheelhouse 内所有 .whl 解包到 site-packages 目录，返回解包数量。

    当提供 ``submodule_usage`` 时按子模块分析选择性解压（精简打包），
    否则全量解压。
    """
    from fspack.slim import slim_unpack

    return slim_unpack(wheelhouse_dir, site_packages_dir, submodule_usage, keep_modules, stage=stage)
