"""构建流水线编排：解析 → embed → 依赖 → 源码 → loader."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable, Sequence

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


# Win7 兼容性 DLL：Python 3.9+ 官方不再支持 Win7，需注入 api-ms-win-core-path-l1-1-0.dll。
# DLL 来源 https://github.com/adang1345/api-ms-win-core-path（LGPL-2.1，基于 Wine 实现）。
# 随 fspack 分发（assets/runtime/），无需网络下载。
_WIN7_COMPAT_DLL_NAME = "api-ms-win-core-path-l1-1-0.dll"


def _needs_win7_compat_dll(py_version: str) -> bool:
    """Python 3.9+ 官方不再支持 Win7，需注入兼容 DLL。

    Python 3.8 是最后官方支持 Win7 的版本；3.9+ 调用 ``PathCchSkipRoot`` 等
    API，需 ``api-ms-win-core-path-l1-1-0.dll`` 提供（Win8+ 自带，Win7 缺失）。
    """
    parts = py_version.split(".")
    return (int(parts[0]), int(parts[1])) >= (3, 9)


def _inject_win7_compat_dll(runtime_dir: Path) -> None:
    """将内置 ``api-ms-win-core-path-l1-1-0.dll`` 复制到 runtime 根目录。

    Python 3.9+ 在 Win7 SP1 上启动时需此 DLL（提供 ``PathCchSkipRoot`` 等 API）。
    DLL 随 fspack 分发（``assets/runtime/``），无需网络下载。重复构建时若
    DLL 已存在则跳过。DLL 缺失时仅告警不报错（向后兼容旧 fspack 安装）。
    """
    dest = runtime_dir / _WIN7_COMPAT_DLL_NAME
    if dest.is_file():
        _logger.info("Win7 兼容 DLL 已就绪: %s", dest)
        return
    src = Path(__file__).parent / "assets" / "runtime" / _WIN7_COMPAT_DLL_NAME
    if not src.is_file():
        _logger.warning("Win7 兼容 DLL 缺失: %s，跳过注入", src)
        return
    shutil.copy2(src, dest)
    _logger.info("注入 Win7 兼容 DLL: %s", dest)


# Linux standalone 标准库精简：剥离运行时无用的模块目录。
# Windows embed 标准库在 python3XX.zip 内（只读、官方已精简），无需处理。
_STDLIB_TRIM_DIRS = ("test", "ensurepip", "idlelib", "pydoc_data", "turtledemo", "tkinter/test", "sqlite3/test")


def _trim_stdlib(runtime_dir: Path, py_version: str, target: Platform, stage: StageRecorder) -> None:
    """剥离 Linux standalone 标准库中运行时无用的模块目录。

    Windows embed 标准库在 python3XX.zip 内（只读、官方已精简），跳过。
    重复构建时已剥离的目录不存在则跳过，幂等。
    """
    if target is not Platform.LINUX:
        stage.set_detail("embed zip 已精简，跳过")
        return
    major, minor = py_version.split(".")[:2]
    stdlib = runtime_dir / "python" / "lib" / f"python{major}.{minor}"
    if not stdlib.is_dir():
        stage.set_detail("标准库目录不存在，跳过")
        return
    removed = 0
    for name in _STDLIB_TRIM_DIRS:
        d = stdlib / name
        if d.is_dir():
            shutil.rmtree(d)
            removed += 1
            _logger.info("精简标准库: 剥离 %s", d)
    stage.skip(removed)
    stage.set_detail(f"剥离 {removed} 目录")


def _site_packages_fingerprint(sp: Path) -> str:
    """site-packages 指纹：``dist-info`` 目录名排序后哈希，快速检测依赖变化。

    用 :meth:`Path.glob` 直接匹配 ``*.dist-info``，避免 ``iterdir`` 遍历
    site-packages 中数千个文件（如 PySide2）时的 stat 开销。
    """
    if not sp.is_dir():
        return ""
    h = hashlib.sha256()
    for d in sorted(sp.glob("*.dist-info")):
        h.update(d.name.encode())
    return h.hexdigest()


def _pyc_stamp_path(dist_dir: Path) -> Path:
    """预编译 stamp 文件路径：``dist/.pyc_stamp``。"""
    return dist_dir / ".pyc_stamp"


def _pyc_stamp_key(src_dir: Path, site_packages: Path, strip_py: bool) -> str:
    """计算预编译 stamp 键：src 指纹 + site-packages 指纹 + strip_py。

    ``copy_source`` 在预编译前已将 ``.py`` 同步到 ``dist/src``（``strip_py`` 模式下
    也会重新复制），故 ``src_fp`` 始终反映完整源码状态，无需特殊处理 ``strip_py``
    的 ``.py`` 缺失场景。stamp 键在检查与写入时复用，避免重复计算指纹。
    """
    from fspack.analyzer import source_fingerprint

    src_fp = source_fingerprint(src_dir) if src_dir.is_dir() else ""
    sp_fp = _site_packages_fingerprint(site_packages)
    return f"{src_fp}|{sp_fp}|{strip_py}"


def _precompile_pyc(  # noqa: PLR0913
    dist_dir: Path,
    runtime_dir: Path,
    py_version: str,
    target: Platform,
    *,
    strip_py: bool,
    stage: StageRecorder,
) -> None:
    """预编译 src 与 site-packages 的 .py 为 .pyc，加速首次启动。

    用 runtime 自身的 python 调用 ``compileall``，保证 ABI 一致。生成
    ``__pycache__/{name}.cpython-{ver}.pyc``（optimize=0），运行时默认加载。

    ``strip_py=True`` 时额外删除非 ``__init__.py`` 的 ``.py`` 源码（保留包标识，
    避免 PEP 420 命名空间包导致 ``.pyc`` 不被加载）。docstring/assert 保留，
    剥离需运行时 ``PYTHONOPTIMIZE=2`` 配合，作为未来增强。

    重复构建时用 ``dist/.pyc_stamp``（src 指纹 + site-packages 指纹 + strip_py）
    跳过 compileall，避免 subprocess 启动与文件遍历开销。
    """
    if target is Platform.WINDOWS:
        py_exe = runtime_dir / "python.exe"
        site_packages = runtime_dir / "Lib" / "site-packages"
    else:
        major, minor = py_version.split(".")[:2]
        py_exe = runtime_dir / "python" / "bin" / f"python{major}.{minor}"
        site_packages = runtime_dir / "python" / "lib" / f"python{major}.{minor}" / "site-packages"
    src_dir = dist_dir / "src"
    if not py_exe.is_file():
        _logger.warning("预编译跳过: runtime python 未就绪 %s", py_exe)
        stage.set_detail("runtime python 未就绪，跳过")
        return

    # stamp 检查：命中则跳过 compileall，stamp_key 留待未命中时写入
    stamp_key = _pyc_stamp_key(src_dir, site_packages, strip_py)
    stamp = _pyc_stamp_path(dist_dir)
    try:
        if stamp.is_file() and stamp.read_text(encoding="utf-8") == stamp_key:
            stage.hit_cache()
            stage.set_detail("缓存命中，跳过编译")
            return
    except OSError:
        pass

    targets = [d for d in (src_dir, site_packages) if d.is_dir()]
    compiled = 0
    for d in targets:
        result = subprocess.run(
            [str(py_exe), "-m", "compileall", str(d), "-q", "-j", "0"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            _logger.warning("compileall 失败 %s: %s", d, result.stderr.strip())
        else:
            compiled += 1
        stage.processed()

    # 写 stamp（编译后、strip 前写入，存编译前的 src_fp）
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(stamp_key, encoding="utf-8")

    stripped = _strip_py_sources(targets) if strip_py else 0
    if stripped:
        stage.skip(stripped)
        stage.set_detail(f"编译 {compiled} 目录，剥离 {stripped} 个 .py")
    else:
        stage.set_detail(f"编译 {compiled} 目录")


def _strip_py_sources(targets: list[Path]) -> int:
    """删除 targets 中非 ``__init__.py`` 的 ``.py`` 源码，返回剥离数量。

    保留 ``__init__.py`` 维持包标识，避免 PEP 420 命名空间包导致 ``.pyc`` 不被加载。
    """
    stripped = 0
    for d in targets:
        for py in d.rglob("*.py"):
            if py.name == "__init__.py":
                continue
            py.unlink()
            stripped += 1
    return stripped


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
    no_stdlib_trim: bool = False,
    no_pyc: bool = False,
    pyc_strip: bool = False,
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

    # Win7 兼容性：Python 3.9+ 官方不再支持 Win7，注入 api-ms-win-core-path-l1-1-0.dll
    # 使 embed python 3.9+ 在 Win7 SP1 / Server 2008 R2 SP1 上也能运行。
    # 仅 Windows 目标需要（Linux standalone 不存在此问题）。
    if target is Platform.WINDOWS and _needs_win7_compat_dll(info.py_version):
        _inject_win7_compat_dll(runtime_dir)

    # 标准库精简：剥离 Linux standalone 中 test/ensurepip/idlelib 等运行时无用模块。
    # Windows embed 标准库在 python3XX.zip 内（官方已精简），阶段内自动跳过。
    if not no_stdlib_trim:
        with tracker.stage("精简标准库") as st:
            _trim_stdlib(runtime_dir, info.py_version, target, st)

    with tracker.stage("分析依赖") as st:
        # 源码指纹缓存：源码未变时跳过 AST 分析，重复构建加速 ~478ms
        from fspack.analyzer import source_fingerprint

        fingerprint = source_fingerprint(project_dir)
        report = _dep_cache_load(cfg.dist_dir, fingerprint, info.dependencies)
        if report is not None:
            st.hit_cache()
            ast_count = len(report.ast_third_party)
            st.set_detail(f"缓存命中，AST {ast_count} 个第三方")
        else:
            report = DependencyReport.from_src(project_dir, info.name, info.dependencies)
            _dep_cache_save(cfg.dist_dir, fingerprint, report)
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
            with tracker.stage("解压 wheel(精简)") as st:
                unpack_wheels(wheels, site_packages, report.ast_submodules, keep_modules, stage=st)
    else:
        _logger.info("无第三方依赖，跳过 wheel 下载")

    if target is Platform.WINDOWS:
        write_pth(cfg.dist_dir, info.py_version)

    with tracker.stage("复制源码") as st:
        src_dst = cfg.dist_dir / "src"
        with spinner(f"复制 {info.name} 源码"):
            copy_source(project_dir, src_dst)

    # 预编译字节码：用 runtime 自身 python 编译 src + site-packages 为 .pyc，加速首次启动。
    # pyc_strip=True 时额外剥离非 __init__.py 源码（源码保护，保留包标识避免命名空间包问题）。
    # 交叉构建时（构建机平台 ≠ 目标平台）runtime python 无法执行，跳过预编译。
    if not no_pyc and target is detect_platform():
        with tracker.stage("预编译字节码") as st:
            _precompile_pyc(cfg.dist_dir, runtime_dir, info.py_version, target, strip_py=pyc_strip, stage=st)

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
    """将项目源码同步到 dist/src，剥离开发期文件。

    保留应用运行所需源码与资源（``.py``/数据文件/``LICENSE`` 等），
    排除构建产物、缓存、虚拟环境、工具配置、项目元数据（
    ``pyproject.toml``/``.python-version``/``uv.lock`` 等）、
    凭证（``.env``）、文档（``*.md``/``*.rst``/``docs``）与测试代码（``tests``）。
    详见 ``_EXCLUDE`` 模式列表。

    增量同步：``src_dst`` 已存在时保留 ``__pycache__`` 目录以复用 ``.pyc`` 缓存，
    仅删除源码中已不存在的文件、覆盖复制新增/改动的文件（``copy2`` 保留 mtime）。
    """
    if src_dst.exists():
        _sync_tree(project_dir, src_dst, _EXCLUDE)
    else:
        shutil.copytree(project_dir, src_dst, ignore=_EXCLUDE)


def _sync_tree(src: Path, dst: Path, ignore_fn: Callable[..., set[str]]) -> None:
    """增量同步 src 到 dst，保留 dst 中的 ``__pycache__`` 以复用 .pyc 缓存。

    1. 删除 dst 中 src 没有的文件/目录（``__pycache__`` 除外）；
    2. 复制 src 中的文件——mtime_ns + size 相同时跳过 ``copy2``（避免重复磁盘写），
       否则用 ``copy2`` 覆盖（保留 mtime 供 compileall 增量判断）。
    """
    src_names = [p.name for p in src.iterdir()]
    ignored = ignore_fn(str(src), src_names) if ignore_fn else set()
    keep = set(src_names) - ignored

    for item in dst.iterdir():
        if item.name == "__pycache__":
            continue
        if item.name not in keep:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

    for name in keep:
        src_item = src / name
        dst_item = dst / name
        if src_item.is_dir():
            dst_item.mkdir(exist_ok=True)
            _sync_tree(src_item, dst_item, ignore_fn)
        elif dst_item.is_file():
            # mtime_ns + size 相同视为未改动，跳过 copy2 避免不必要的磁盘写
            src_st = src_item.stat()
            dst_st = dst_item.stat()
            if src_st.st_mtime_ns == dst_st.st_mtime_ns and src_st.st_size == dst_st.st_size:
                continue
            shutil.copy2(src_item, dst_item)
        else:
            shutil.copy2(src_item, dst_item)


def _dep_cache_path(dist_dir: Path) -> Path:
    """依赖分析缓存文件路径：``dist/.dep_cache.json``。"""
    return dist_dir / ".dep_cache.json"


def _dep_cache_load(dist_dir: Path, fingerprint: str, declared: tuple[str, ...]) -> DependencyReport | None:
    """加载依赖分析缓存，指纹或声明依赖不匹配时返回 ``None``。"""
    cache = _dep_cache_path(dist_dir)
    if not cache.is_file():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("fingerprint") != fingerprint or tuple(data.get("declared", [])) != declared:
        return None
    r = data["report"]
    return DependencyReport(
        declared=tuple(r["declared"]),
        ast_third_party=tuple(r["ast_third_party"]),
        ast_stdlib=tuple(r["ast_stdlib"]),
        ast_local=tuple(r["ast_local"]),
        ast_submodules={k: frozenset(v) for k, v in r["ast_submodules"].items()},
    )


def _dep_cache_save(dist_dir: Path, fingerprint: str, report: DependencyReport) -> None:
    """保存依赖分析缓存。"""
    cache = _dep_cache_path(dist_dir)
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": fingerprint,
        "declared": list(report.declared),
        "report": {
            "declared": list(report.declared),
            "ast_third_party": list(report.ast_third_party),
            "ast_stdlib": list(report.ast_stdlib),
            "ast_local": list(report.ast_local),
            "ast_submodules": {k: sorted(v) for k, v in report.ast_submodules.items()},
        },
    }
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


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
