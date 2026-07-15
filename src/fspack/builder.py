"""构建流水线编排：解析 → embed → 依赖 → 源码 → loader。."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from fspack.config import BuildConfig, DependencyReport, MirrorConfig, ProjectInfo
from fspack.console import success
from fspack.embed import download_embed, embed_dirname, extract_embed, write_pth
from fspack.exceptions import DependencyError
from fspack.loader import compile_loader, generate_loader_source
from fspack.platform import Platform, detect_platform, wheel_platform_tags
from fspack.progress import BuildTracker, StageRecorder, spinner
from fspack.project import DEFAULT_LINUX_PY_VERSION, DEFAULT_PY_VERSION, resolve_py_version
from fspack.standalone import STANDALONE_RELEASE_TAG, download_standalone, extract_standalone
from fspack.wheel_cache import fspack_wheel_cache_dir

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


def build(  # noqa: PLR0912, PLR0913
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
        info = ProjectInfo.from_dir(project_dir, py_version)
        default_ver = DEFAULT_LINUX_PY_VERSION if target is Platform.LINUX else DEFAULT_PY_VERSION
        resolved = resolve_py_version(project_dir, py_version, info.requires_python, default_ver)
        if resolved != info.py_version:
            _logger.info("自动选择 Python 版本: %s", resolved)
            info = replace(info, py_version=resolved)
        _logger.info("项目: %s %s (%s) 目标: %s", info.name, info.version, info.app_type.value, target.value)
        st.set_detail(f"{info.name} {info.version} ({info.app_type.value})")

    runtime_dir = cfg.dist_dir / "runtime"
    if target is Platform.LINUX:
        major, minor = info.py_version.split(".")[:2]
        python_bin = runtime_dir / "python" / "bin" / f"python{major}.{minor}"
        runtime_ready = python_bin.is_file()
        standalone_cache = Path.home() / ".fspack" / "cache" / "standalone"
        tar_path: Path | None = None
        with tracker.stage("下载运行时") as st:
            if runtime_ready:
                st.hit_cache()
                st.set_detail("runtime 已就绪")
            else:
                tar_path = download_standalone(info.py_version, STANDALONE_RELEASE_TAG, standalone_cache, stage=st)
                st.set_detail("python-build-standalone")
        with tracker.stage("解压运行时") as st:
            if runtime_ready:
                st.hit_cache()
                st.set_detail("runtime 已就绪")
            else:
                assert tar_path is not None
                extract_standalone(tar_path, runtime_dir)
                st.processed(1)
                st.set_detail("python-build-standalone")
        site_packages = runtime_dir / "python" / "lib" / f"python{major}.{minor}" / "site-packages"
    else:
        dll_marker = runtime_dir / f"{embed_dirname(info.py_version)}.dll"
        runtime_ready = dll_marker.is_file()
        zip_path: Path | None = None
        with tracker.stage("下载运行时") as st:
            if runtime_ready:
                st.hit_cache()
                st.set_detail("runtime 已就绪")
            else:
                zip_path = download_embed(info.py_version, cfg.mirror, cfg.embed_cache_dir, stage=st)
                st.set_detail("embed python")
        with tracker.stage("解压运行时") as st:
            if runtime_ready:
                st.hit_cache()
                st.set_detail("runtime 已就绪")
            else:
                assert zip_path is not None
                extract_embed(zip_path, runtime_dir)
                st.processed(1)
                st.set_detail("embed python")
        site_packages = runtime_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)

    with tracker.stage("分析依赖") as st:
        report = DependencyReport.from_src(project_dir, info.name, info.dependencies)
        if report.missing:
            _logger.info("AST 发现未声明依赖: %s", ", ".join(report.missing))
        ast_count = len(report.ast_third_party)
        st.processed(ast_count)
        st.set_detail(f"AST {ast_count} 个第三方")

    # 下载用包名：优先 declared（pyproject.toml 声明的 PyPI 包名，权威），
    # declared 为空时回退到 ast_third_party（AST 扫描的导入名，best effort）。
    # 原因：导入名 ≠ PyPI 包名时（如 orderedset → ordered-set），用导入名 pip download 会失败。
    # declared 非空时以声明为准，未声明的依赖通过 report.missing 日志提示用户补充。
    packages_to_download: tuple[str, ...] = report.declared if report.declared else report.ast_third_party

    if packages_to_download:
        if _site_packages_has_deps(site_packages):
            with tracker.stage("下载依赖") as st:
                _logger.info("site-packages 已有依赖，跳过下载解压")
                st.skip(len(packages_to_download))
                st.set_detail("已存在跳过")
        else:
            wheel_cache = fspack_wheel_cache_dir()
            with tracker.stage("下载依赖") as st:
                wheels = download_wheels(
                    packages_to_download,
                    info.py_version,
                    cfg.mirror.pypi_index,
                    wheel_cache,
                    platform_tags=wheel_platform_tags(target),
                    stage=st,
                )
            with tracker.stage("解压 wheel") as st:
                unpack_wheels(wheels, site_packages, report.ast_submodules, keep_modules, stage=st)
    else:
        _logger.info("无第三方依赖，跳过 wheel 下载")

    if target is Platform.WINDOWS:
        write_pth(cfg.dist_dir, info.py_version)

    with tracker.stage("复制源码") as st:
        src_dst = cfg.dist_dir / "src"
        with spinner(f"复制 {info.name} 源码"):
            copy_source(project_dir, src_dst)

    exes: list[Path] = []
    with tracker.stage("生成 C loader") as st:
        source = generate_loader_source(info.py_xy, target)
        build_dir = cfg.dist_dir / "build"
        for ep in info.all_entries:
            entry_rel = ep.entry_rel(info.src_dir)
            entry_file_in_dist = f"src/{entry_rel}"
            if info.entries:
                # 多入口模式：每个入口写 <name>.entry
                (cfg.dist_dir / f"{ep.name}.entry").write_text(entry_file_in_dist, encoding="utf-8")
            else:
                # 单入口模式：写 .entry（向后兼容）
                (cfg.dist_dir / ".entry").write_text(entry_file_in_dist, encoding="utf-8")
            exe_name = f"{ep.name}.exe" if target is Platform.WINDOWS else ep.name
            exe = cfg.dist_dir / exe_name
            compile_loader(source, exe, ep.app_type, build_dir, target, stage=st)
            exes.append(exe)
        st.processed(len(exes))

    rich_console.print(tracker.summary())
    if len(exes) == 1:
        success(f"构建完成: {exes[0]}")
    else:
        success(f"构建完成: {len(exes)} 个入口")
        for exe in exes:
            rich_console.print(f"  - {exe}")
    return info


def copy_source(project_dir: Path, src_dst: Path) -> None:
    """将项目源码复制到 dist/src，排除构建产物与缓存。."""
    if src_dst.exists():
        shutil.rmtree(src_dst)
    shutil.copytree(project_dir, src_dst, ignore=_EXCLUDE)


def _site_packages_has_deps(site_packages: Path) -> bool:
    """检查 site-packages 是否已有解压的 wheel 依赖。

    通过检查 ``*.dist-info`` 目录是否存在判断：有则认为依赖已解压，
    可跳过下载+解压阶段（需 ``fspack c`` 清理后才会重新解压）。
    """
    return site_packages.is_dir() and any(site_packages.glob("*.dist-info"))


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
    cache_dir: Path,
    platform_tags: Sequence[str] = ("win_amd64",),
    *,
    stage: StageRecorder | None = None,
) -> list[Path]:
    """用 dev python 的 pip 下载指定平台 wheel 到 cache_dir，返回本次依赖的 wheel 路径列表。

    优先用 ``--no-index --find-links cache_dir`` 从本地缓存解析依赖，命中则完全跳过
    网络查询；缓存不完整或条件依赖未满足（如 pypdf 的 ``typing_extensions`` marker）
    时回退到带 ``-i index`` 的完整下载。

    ``platform_tags`` 为 pip ``--platform`` 标签列表，可重复指定以匹配多个
    平台标签（如 Linux 同时匹配 manylinux2014 与 manylinux_2_28）。

    ``cache_dir`` 为 fspack wheel 缓存目录（``~/.fspack/cache/wheels/``），持久化
    保存已下载的 wheel。pip 自动跳过已存在的 wheel（"File was already downloaded"），
    仅下载缺失项（"Saved"）。解析 stdout 获取本次所有 wheel 路径（含传递依赖），
    供 unpack_wheels 解压。

    自动选择能跑 pip 的 python 解释器：优先当前 venv，回退系统 python3
    （uv venv 默认不含 pip）。

    ``stage`` 用于回写缓存命中数、下载字节数与 wheel 数到 BuildTracker。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 尝试读取依赖解析缓存，命中则跳过 pip 调用
    deps_key = _deps_cache_key(packages, py_version, platform_tags)
    cached_wheels = _load_deps_cache(cache_dir, deps_key)
    if cached_wheels is not None:
        _logger.info("依赖解析缓存命中，跳过 pip 调用")
        if stage is not None:
            stage.hit_cache(len(cached_wheels))
            stage.processed(len(cached_wheels))
            stage.set_detail(f"{len(cached_wheels)} wheels, 解析缓存命中")
        return cached_wheels

    py = _find_pip_python()
    major, minor = py_version.split(".")[:2]
    platform_args: list[str] = []
    for tag in platform_tags:
        platform_args.extend(["--platform", tag])
    common_args: list[str] = [
        py,
        "-m",
        "pip",
        "download",
        "-d",
        str(cache_dir),
        "--find-links",
        str(cache_dir),
        *platform_args,
        "--python-version",
        f"{major}.{minor}",
        "--abi",
        f"cp{major}{minor}",
        "--implementation",
        "cp",
        "--only-binary=:all:",
        *packages,
    ]

    _logger.info("下载依赖 wheel: %s", " ".join(packages))
    before = {f.name for f in cache_dir.glob("*.whl")}

    # 先用 --no-index 从本地缓存解析，命中则跳过网络查询；
    # 缓存不完整或条件依赖未满足时回退到带 index 的完整下载
    result = _run_pip([*common_args, "--no-index"], f"检查缓存 {len(packages)} 个依赖", suppress_error=True)
    if result is None:
        _logger.info("缓存解析失败，回退到索引下载")
        result = _run_pip([*common_args, "-i", pypi_index], f"pip download {len(packages)} 个依赖")
    else:
        _logger.info("缓存解析成功，跳过网络查询")
    assert result is not None  # 回退路径 suppress_error=False，要么返回结果要么抛异常

    wheel_names = _parse_pip_download_wheels(result.stdout)
    if not wheel_names:
        _logger.warning("pip download 输出解析失败，回退到目录扫描")
        wheel_names = sorted(f.name for f in cache_dir.glob("*.whl"))

    wheels = [cache_dir / name for name in wheel_names if (cache_dir / name).is_file()]
    if wheels:
        _save_deps_cache(cache_dir, deps_key, wheels)
    if stage is not None:
        new_wheels = [w for w in wheels if w.name not in before]
        existing_wheels = [w for w in wheels if w.name in before]
        if new_wheels:
            stage.add_bytes(sum(w.stat().st_size for w in new_wheels))
        if existing_wheels:
            stage.hit_cache(len(existing_wheels))
        stage.processed(len(wheels))
        cache_status = "缓存命中" if not new_wheels else f"新增 {len(new_wheels)}"
        stage.set_detail(f"{len(wheels)} wheels, {cache_status}")
    return wheels


def _deps_cache_key(
    packages: tuple[str, ...] | list[str],
    py_version: str,
    platform_tags: Sequence[str],
) -> str:
    """根据依赖列表、Python 版本与平台标签计算缓存键。

    不同组合产生不同键，确保跨项目/跨版本/跨平台不会误命中。
    返回 16 位 hex 摘要，用于 ``.deps-<key>.json`` 文件名。
    """
    data = f"{sorted(packages)}|{py_version}|{list(platform_tags)}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def _load_deps_cache(cache_dir: Path, key: str) -> list[Path] | None:
    """读取依赖解析缓存，返回 wheel 路径列表；未命中或文件丢失返回 None。

    缓存文件 ``.deps-<key>.json`` 记录上次 pip 解析出的 wheel 文件名列表。
    命中后逐个校验 wheel 文件仍存在于 cache_dir，任一缺失则视为未命中
    （避免 wheel 被手动删除后仍跳过 pip）。
    """
    cache_file = cache_dir / f".deps-{key}.json"
    if not cache_file.is_file():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        names: list[str] = data.get("wheels", [])
        wheels = [cache_dir / name for name in names]
        if wheels and all(w.is_file() for w in wheels):
            return wheels
    except (OSError, json.JSONDecodeError, ValueError):
        _logger.warning("依赖解析缓存损坏，将重新解析: %s", cache_file)
    return None


def _save_deps_cache(cache_dir: Path, key: str, wheels: Sequence[Path]) -> None:
    """写入依赖解析缓存，记录 wheel 文件名列表。

    best-effort：写入失败仅 warning 不影响构建（缓存只是优化，缺失会回退到 pip）。
    """
    cache_file = cache_dir / f".deps-{key}.json"
    try:
        cache_file.write_text(
            json.dumps({"wheels": [w.name for w in wheels]}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        _logger.warning("写入依赖解析缓存失败: %s", e)


def _run_pip(
    cmd: list[str],
    label: str,
    *,
    suppress_error: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    """运行 pip download 命令，返回执行结果。

    ``suppress_error=True`` 时 ``CalledProcessError`` 返回 None（用于 ``--no-index``
    回退路径，调用方据 None 回退到带 index 命令）；``suppress_error=False`` 时转为
    ``DependencyError`` 抛出（含 stderr）。``FileNotFoundError`` 总是转为
    ``DependencyError``（pip 消失）。
    """
    try:
        with spinner(label):
            return subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise DependencyError(f"未找到 pip: {cmd[0]}") from e
    except subprocess.CalledProcessError as e:
        if suppress_error:
            _logger.info("pip 命令失败（将回退）: %s", (e.stderr or "").strip())
            return None
        raise DependencyError(f"依赖下载失败:\n{e.stderr}") from e


# 匹配 pip download stdout 中的 "Saved <path>.whl" 和 "File was already downloaded <path>.whl"
_PIP_WHEEL_LINE_RE = re.compile(r"(?:Saved|File was already downloaded)\s+(.+\.whl)", re.IGNORECASE)


def _parse_pip_download_wheels(stdout: str) -> list[str]:
    """解析 pip download stdout，提取本次涉及的 wheel 文件名（含传递依赖）。

    匹配 ``Saved <path>.whl``（新下载）和 ``File was already downloaded <path>.whl``（已存在跳过）。
    返回 wheel 文件名列表（去重保序）。
    """
    names: list[str] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        m = _PIP_WHEEL_LINE_RE.search(line)
        if m:
            name = Path(m.group(1).strip()).name
            if name not in seen:
                names.append(name)
                seen.add(name)
    return names


def unpack_wheels(
    wheels: Sequence[Path],
    site_packages_dir: Path,
    submodule_usage: dict[str, frozenset[str]] | None = None,
    keep_modules: set[str] | None = None,
    *,
    stage: StageRecorder | None = None,
) -> int:
    """将给定 wheel 列表解包到 site-packages 目录，返回解包数量。

    当提供 ``submodule_usage`` 时按子模块分析选择性解压（精简打包），
    否则全量解压。
    """
    from fspack.slim import slim_unpack

    return slim_unpack(wheels, site_packages_dir, submodule_usage, keep_modules, stage=stage)
