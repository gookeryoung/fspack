"""构建流水线编排：解析 → embed → 依赖 → 源码 → loader."""

from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from fspack.config import (
    DEFAULT_LINUX_PY_VERSION,
    DEFAULT_PY_VERSION,
    BuildConfig,
    DependencyReport,
    MirrorConfig,
    ProjectInfo,
    resolve_py_version,
)
from fspack.console import console
from fspack.packaging.builtin import TkinterBundler
from fspack.packaging.entry import EntryWrapper
from fspack.packaging.icon import ensure_ico, find_favicon
from fspack.packaging.loader import compile_loader, generate_loader_source
from fspack.packaging.runtime import (
    STANDALONE_RELEASE_TAG,
    download_embed,
    download_standalone,
    embed_dirname,
    extract_embed,
    extract_standalone,
    write_pth,
)
from fspack.packaging.wheels import download_wheels
from fspack.platform import Platform, detect_platform, wheel_platform_tags
from fspack.progress import BuildTracker, StageRecorder, spinner

__all__ = [
    "DEFAULT_PY_VERSION",
    "build",
    "copy_source",
    "default_icon_path",
    "download_wheels",
    "fspack_wheel_cache_dir",
    "unpack_wheels",
]

_logger = logging.getLogger(__name__)

# 默认 icon：打包在 fspack 包内，随 wheel 分发
_DEFAULT_ICON = Path(__file__).parent / "assets" / "icons" / "app.ico"


def default_icon_path() -> Path:
    """返回 fspack 自带的默认 icon 路径（``assets/icons/app.ico``）."""
    return _DEFAULT_ICON


def fspack_wheel_cache_dir() -> Path:
    """返回 fspack wheel 缓存目录 ``~/.fspack/cache/wheels/``."""
    return Path.home() / ".fspack" / "cache" / "wheels"


# dist/src 仅保留应用运行所需源码与资源，剥离所有开发期文件。
# 向后兼容策略：未在下方显式列出的文件默认保留，避免误删项目特有运行时资源。
# LICENSE 不排除：分发产物保留许可证文件满足 MIT/GPL 等开源协议「随附 LICENSE」要求。
_EXCLUDE = shutil.ignore_patterns(
    # 构建产物与 Python 缓存
    "dist",
    "build",
    "__pycache__",
    "*.egg-info",
    "*.pyc",
    "*.pyo",
    # 虚拟环境、测试与覆盖率
    ".venv",
    ".tox",
    ".pytest_cache",
    "htmlcov",
    ".coverage",
    ".coverage.*",
    "coverage.xml",
    "tests",
    # 工具缓存
    ".ruff_cache",
    ".pyrefly_cache",
    ".mypy_cache",
    ".uv-cache",
    # 版本控制
    ".git",
    ".gitignore",
    ".gitattributes",
    # IDE 与编辑器
    ".idea",
    ".vscode",
    "*.code-workspace",
    # fspack 自身目录
    ".fspack",
    ".trae",
    # 凭证与敏感信息（rule-11 安全要求：.env 须排除避免泄漏到 dist）
    ".env",
    ".env.*",
    # Python 项目元数据（打包阶段已解析完毕，运行时不再需要）
    ".python-version",
    "pyproject.toml",
    "uv.lock",
    "uv.toml",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
    "requirements*.txt",
    # 工具链配置文件（rule-11 独立配置文件，仅开发期使用）
    "ruff.toml",
    ".ruff.toml",
    "pyrefly.toml",
    "pytest.ini",
    "tox.ini",
    ".bumpversion.toml",
    ".pre-commit-config.yaml",
    ".coveragerc",
    ".readthedocs.yaml",
    "Makefile",
    ".copier-answers.yml",
    # CI/CD
    ".github",
    # 文档（应用运行时不需要）
    "*.md",
    "*.rst",
    "docs",
)


def build(  # noqa: PLR0912, PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    dist_dir: Path | None = None,
    embed_cache: Path | None = None,
    target: Platform | None = None,
    keep_modules: set[str] | None = None,
    icon: Path | None = None,
) -> ProjectInfo:
    """执行完整构建流水线，返回项目信息。

    icon 优先级：CLI ``--icon`` > 项目 ``[tool.fspack] icon`` > 自动搜索
    ``favicon.*`` > 默认 ``assets/icons/app.ico``。非 ``.ico`` 格式（如
    ``.png``/``.jpg``）通过 Pillow 转换为 ``.ico``（需安装 ``fspack[image]``），
    转换失败回退到默认 icon。仅 Windows 目标生效，Linux 忽略（ELF 无图标资源概念）。
    """
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

    # 补充内置库：embed python 缺失 tkinter（纯 Python 包 + _tkinter.pyd + Tcl/Tk 脚本），
    # 若 AST 检测到 tkinter 使用则从 python-build-standalone Windows 构建提取并补充到 runtime。
    # Linux standalone 已含全部 stdlib，无需补充。
    has_tkinter = False
    if TkinterBundler.is_needed(report.ast_stdlib, target):
        builtin_cache = Path.home() / ".fspack" / "cache"
        with tracker.stage("补充内置库") as st:
            TkinterBundler.ensure(runtime_dir, info.py_version, builtin_cache, stage=st)
            has_tkinter = True
            st.set_detail("tkinter")

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

    # icon 优先级：CLI --icon > 项目 [tool.fspack] icon > 自动搜索 favicon.* > 默认 app.ico（仅 Windows）
    # Linux 目标无图标资源概念，统一传 None
    resolved_icon = _resolve_project_icon(icon, info.icon, project_dir, cfg.dist_dir / "build", target)

    exes: list[Path] = []
    with tracker.stage("生成 C loader") as st:
        source = generate_loader_source(info.py_xy, target)
        build_dir = cfg.dist_dir / "build"
        for ep in info.all_entries:
            entry_rel = ep.entry_rel(info.src_dir)
            result = EntryWrapper.dotted_module_name(info.src_dir, ep.file)
            module_dotted = result[0] if result is not None else None
            pkg_root_rel = result[1] if result is not None else "."
            # 生成入口包装器：处理 sys.path、Qt 插件路径与包上下文（相对导入）
            wrapper_name = f"_entry_{ep.name}.py"
            wrapper_path = cfg.dist_dir / wrapper_name
            wrapper_path.write_text(
                EntryWrapper.generate_wrapper_source(
                    ep.name, module_dotted, entry_rel, pkg_root_rel, has_tkinter=has_tkinter
                ),
                encoding="utf-8",
            )
            # .entry 指向 wrapper（loader 读 .entry 路径运行）
            if info.entries:
                # 多入口模式：每个入口写 <name>.entry
                (cfg.dist_dir / f"{ep.name}.entry").write_text(wrapper_name, encoding="utf-8")
            else:
                # 单入口模式：写 .entry（向后兼容）
                (cfg.dist_dir / ".entry").write_text(wrapper_name, encoding="utf-8")
            exe_name = f"{ep.name}.exe" if target is Platform.WINDOWS else ep.name
            exe = cfg.dist_dir / exe_name
            compile_loader(source, exe, ep.app_type, build_dir, target, icon=resolved_icon, stage=st)
            exes.append(exe)
        st.processed(len(exes))

    console.rich.print(tracker.summary())
    if len(exes) == 1:
        console.success(f"构建完成: {exes[0]}")
    else:
        console.success(f"构建完成: {len(exes)} 个入口")
        for exe in exes:
            console.rich.print(f"  - {exe}")
    return info


def copy_source(project_dir: Path, src_dst: Path) -> None:
    """将项目源码复制到 dist/src，剥离开发期文件。

    保留应用运行所需源码与资源（``.py``/数据文件/``LICENSE`` 等），
    排除构建产物、缓存、虚拟环境、工具配置、项目元数据（
    ``pyproject.toml``/``.python-version``/``uv.lock`` 等）、
    凭证（``.env``）、文档（``*.md``/``*.rst``/``docs``）与测试代码（``tests``）。
    详见 ``_EXCLUDE`` 模式列表。
    """
    if src_dst.exists():
        shutil.rmtree(src_dst)
    shutil.copytree(project_dir, src_dst, ignore=_EXCLUDE)


def _resolve_project_icon(
    cli_icon: Path | None,
    project_icon: Path | None,
    project_dir: Path,
    work_dir: Path,
    target: Platform,
) -> Path | None:
    """按优先级解析最终 icon 路径，非 .ico 格式自动转换。

    优先级：``cli_icon`` > ``project_icon`` > 自动搜索 ``favicon.*`` > 默认 ``app.ico``。

    - Linux 目标：始终返回 ``None``（ELF 无图标资源概念）
    - 非 ``.ico`` 格式（``.png``/``.jpg`` 等）：调用 :func:`ensure_ico` 转换，
      转换失败（如 Pillow 未安装）回退到默认 ``app.ico``
    - 默认 ``app.ico`` 是 fspack 自带资源，必定存在，无需转换

    ``work_dir`` 为图片转换的临时目录（通常是 ``dist/build``）。
    """
    if target is Platform.LINUX:
        return None

    # 选定候选 icon：CLI > 项目配置 > favicon 自动搜索
    candidate = cli_icon
    if candidate is None:
        candidate = project_icon
    if candidate is None:
        candidate = find_favicon(project_dir)
        if candidate is not None:
            _logger.info("使用 favicon 作为 icon: %s", candidate)

    # 无任何候选 → 默认 icon（.ico，无需转换）
    if candidate is None:
        return _DEFAULT_ICON

    # 转换为 .ico（.ico 原样返回，其他格式用 Pillow 转换，失败回退默认）
    resolved = ensure_ico(candidate, work_dir)
    if resolved is not None:
        return resolved
    _logger.warning("icon 转换失败，回退到默认 icon: %s", _DEFAULT_ICON)
    return _DEFAULT_ICON


def _site_packages_has_deps(site_packages: Path) -> bool:
    """检查 site-packages 是否已有解压的 wheel 依赖。

    通过检查 ``*.dist-info`` 目录是否存在判断：有则认为依赖已解压，
    可跳过下载+解压阶段（需 ``fspack c`` 清理后才会重新解压）。
    """
    return site_packages.is_dir() and any(site_packages.glob("*.dist-info"))


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
