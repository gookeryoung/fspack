"""源码与目录同步：copy_source、增量同步、目录大小、site-packages 指纹.

本模块从 :mod:`fspack.builder` 抽离，仅含源码同步与目录度量辅助函数，
无外部 API 依赖。``builder.py`` 通过 re-export 保持公开 API 不变。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Callable

from fspack._util.fsutil import scandir_dir_size

_logger = logging.getLogger(__name__)

# 始终排除的模式：构建产物、Python 缓存、虚拟环境、覆盖率、工具缓存、版本控制、
# IDE 配置、fspack 自身目录、凭证、CI/CD、测试目录。
# data-dirs 内的目录树也应用这些模式（如 assets/templates/ 下的 __pycache__ 也应排除）。
_EXCLUDE_ALWAYS = shutil.ignore_patterns(
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
    # 前端依赖缓存：pnpm install 可再生（.pnpm 内路径可超 MAX_PATH 260，
    # 拷入 dist 会导致清理失败），模板用户创建项目后自行安装
    "node_modules",
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
    # CI/CD
    ".github",
)

# 元数据/工具配置/文档模式：默认排除（应用运行时不需要），data-dirs 内保留
# （data-dirs 内的目录树视为完整资源，如 fspack 的 assets/templates/ 含完整项目模板，
# 其内的 pyproject.toml/README.md/uv.lock 等是模板必需文件，必须保留）。
_EXCLUDE_METADATA = shutil.ignore_patterns(
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
    # 文档（应用运行时不需要）
    "*.md",
    "*.rst",
    "docs",
)


def _merge_ignore_fns(
    *fns: Callable[..., set[str]],
) -> Callable[..., set[str]]:
    """合并多个 ignore 函数为并集返回的单一函数.

    ``shutil.ignore_patterns`` 返回的函数签名 ``（directory, names) -> set[str]``，
    合并后对同一 ``(directory, names)`` 取所有函数返回集的并集。
    """

    def combined(directory: str, names: list[str]) -> set[str]:
        result: set[str] = set()
        for fn in fns:
            result |= fn(directory, names)
        return result

    return combined


# 完整排除集：始终排除 + 元数据排除（默认行为，向后兼容）。
# 等价于原单一 _EXCLUDE，拆分为两层以支持 data-dirs 选择性跳过元数据排除。
_EXCLUDE = _merge_ignore_fns(_EXCLUDE_ALWAYS, _EXCLUDE_METADATA)


def copy_source(  # noqa: PLR0913
    project_dir: Path,
    src_dst: Path,
    extra_excludes: tuple[str, ...] = (),
    data_dirs: tuple[str, ...] = (),
    web_static_dirs: tuple[str, ...] = (),
    frontend_prune: Mapping[Path, Sequence[Path]] | None = None,
) -> None:
    """将项目源码同步到 dist/src，剥离开发期文件.

    保留应用运行所需源码与资源（``.py``/数据文件/``LICENSE`` 等），
    排除构建产物、缓存、虚拟环境、工具配置、项目元数据（
    ``pyproject.toml``/``.python-version``/``uv.lock`` 等）、
    凭证（``.env``）、文档（``*.md``/``*.rst``/``docs``）与测试代码（``tests``）。
    详见 ``_EXCLUDE_ALWAYS``/``_EXCLUDE_METADATA`` 模式列表。

    ``extra_excludes`` 为 ``[tool.fspack] exclude`` 配置的额外排除模式，
    合并到内置 ``_EXCLUDE`` 中（如排除 ``examples`` 目录）。

    ``data_dirs`` 为 ``[tool.fspack] data-dirs`` 配置的数据资源目录树（相对
    ``project_dir`` 的 POSIX 路径，如 ``src/fspack/assets/templates``）。
    这些目录树内的元数据/文档文件（``pyproject.toml``/``*.md``/``uv.lock`` 等）
    不被排除，仅应用 ``_EXCLUDE_ALWAYS``（构建产物/缓存/IDE 等）。
    用于含子项目作为资源的场景（如 fspack 自身的 ``assets/templates/`` 含完整
    项目模板，其 ``pyproject.toml``/``README.md`` 是模板必需文件，不能剥离）。

    ``web_static_dirs`` 为 ``[tool.fspack] web-static-dirs`` 配置的前端构建
    产物目录（相对 ``project_dir`` 的 POSIX 路径，如 ``dist``），与 ``data_dirs``
    同等保护——目录树内元数据/文档不被排除。仅 ``AppType.WEB`` 项目使用，
    wrapper 在打包时把这些目录解析为 dist 下绝对路径，注入 Flask ``static_folder``
    / FastAPI ``StaticFiles`` serve。

    ``frontend_prune`` 为前端裁剪映射（前端根目录绝对路径 → 产物目录绝对路径
    元组，来自构建阶段的 :class:`FrontendProject` 集合）：前端根目录下仅保留
    产物路径上的条目（``deploy/``/``dist/``），源码（``src/``/``public/``/
    ``package.json``/构建配置等）不进入发布产物——发布产物离线可用，无需
    node 环境。产物目录同时并入保护集合（目录树内文件不被元数据排除，产物
    目录名命中 ``_EXCLUDE_ALWAYS`` 时如 ``dist`` 会被恢复）。

    增量同步：``src_dst`` 已存在时保留 ``__pycache__`` 目录以复用 ``.pyc`` 缓存，
    仅删除源码中已不存在的文件、覆盖复制新增/改动的文件（``copy2`` 保留 mtime）。
    """
    ignore_fn = _build_ignore_fn(project_dir, extra_excludes, data_dirs, web_static_dirs, frontend_prune)
    if src_dst.exists():
        _sync_tree(project_dir, src_dst, ignore_fn)
    else:
        shutil.copytree(project_dir, src_dst, ignore=ignore_fn)


def _build_frontend_prune_fn(
    frontend_prune: Mapping[Path, Sequence[Path]],
) -> Callable[[Path, list[str]], set[str]]:
    """构造前端裁剪函数：前端根目录下仅保留产物路径上的条目.

    返回 ``(dir_resolved, names) -> set[str]``（接收已 resolve 的目录路径，
    与 :func:`_build_ignore_fn` 共享 resolve 结果避免重复系统调用）：

    - 目录即前端根：保留各产物目录相对根的首段名（``deploy``/``dist``），
      其余（``src``/``public``/``package.json``/构建配置等）全排除
    - 目录在产物路径上（产物的祖先）：保留通往产物的下一段
    - 目录在前端根内但不在任何产物路径上：全排除（不可达，防御分支）
    - 产物目录即前端根本身（配置指向前端根的语义）：不裁剪，返回空集
    """
    # 预 resolve（root/产物均绝对），忽略大小写差异由 Path 语义保证
    resolved: list[tuple[Path, tuple[Path, ...]]] = [
        (root.resolve(), tuple(o.resolve() for o in outs)) for root, outs in frontend_prune.items()
    ]

    def prune_fn(dir_resolved: Path, names: list[str]) -> set[str]:
        for root, outs in resolved:
            if dir_resolved != root and root not in dir_resolved.parents:
                continue
            if root in outs:
                return set()
            # 目录在某产物目录内部（含产物自身，即产物是目录的祖先）：
            # 产物内部不裁剪（html/js/css 等全保留）
            if any(dir_resolved == out or out in dir_resolved.parents for out in outs):
                return set()
            rel = dir_resolved.relative_to(root).parts
            keep: set[str] = set()
            for out in outs:
                parts = out.relative_to(root).parts
                if len(parts) > len(rel) and parts[: len(rel)] == rel:
                    keep.add(parts[len(rel)])
            # keep 为空 = 目录不在任何产物路径上（不可达，防御分支）：全裁
            return {n for n in names if n not in keep}
        return set()

    return prune_fn


def _build_ignore_fn(
    project_dir: Path,
    extra_excludes: tuple[str, ...],
    data_dirs: tuple[str, ...],
    web_static_dirs: tuple[str, ...] = (),
    frontend_prune: Mapping[Path, Sequence[Path]] | None = None,
) -> Callable[..., set[str]]:
    """构造 ignore 函数：data-dirs/web-static-dirs 内只应用 _EXCLUDE_ALWAYS，外应用完整 _EXCLUDE.

    data-dirs 与 web-static-dirs 解析为绝对路径前缀集合，ignore 函数对每个
    ``directory`` 判断是否在任一保护目录内（前缀匹配），是则跳过 ``_EXCLUDE_METADATA``。
    两者语义等价（同等保护），合并为一个集合判断。

    ``frontend_prune`` 的产物目录并入保护集合；裁剪函数
    :func:`_build_frontend_prune_fn` 的排除集合并入返回值（前端源码不进 dist）。

    ``extra_excludes`` 始终应用（用户显式排除优先级最高，不论是否在保护目录内）。
    """
    extra_fn = shutil.ignore_patterns(*extra_excludes) if extra_excludes else None
    prune_fn = _build_frontend_prune_fn(frontend_prune) if frontend_prune else None
    # 合并 data_dirs + web_static_dirs（两者同等保护，无顺序差异）
    protected_dirs = (*data_dirs, *web_static_dirs)
    if not protected_dirs and not frontend_prune:
        # 无保护目录：返回完整 _EXCLUDE（已含 _ALWAYS + _METADATA）+ extra
        if extra_fn is None:
            return _EXCLUDE

        def full_ignore(directory: str, names: list[str]) -> set[str]:
            return _EXCLUDE(directory, names) | extra_fn(directory, names)

        return full_ignore

    # 预解析保护目录为绝对路径，避免每个 directory 调用时重复 resolve
    project_dir_abs = project_dir.resolve()
    protected_abs: list[Path] = []
    for rel in protected_dirs:
        # 配置为 POSIX 路径（如 "src/fspack/assets/templates" 或 "dist"），
        # Path() 跨平台接受正斜杠，resolve 后与 directory 比较前缀
        abs_path = (project_dir_abs / Path(rel)).resolve()
        protected_abs.append(abs_path)
    # 前端产物目录同等保护：目录树内文件不被元数据排除（产物内的
    # asset-manifest.json/README 等随包分发）；产物目录名命中
    # _EXCLUDE_ALWAYS（如 "dist"）时由下方恢复机制救回
    if frontend_prune:
        for outs in frontend_prune.values():
            protected_abs.extend(o.resolve() for o in outs)

    def ignore_fn(directory: str, names: list[str]) -> set[str]:
        excluded = _EXCLUDE_ALWAYS(directory, names)
        # 判断 directory 是否在任一保护目录内（含目录自身）。
        # Path.is_relative_to 是 3.9+，fspack 支持 3.8，用 try/except ValueError 兼容。
        dir_path = Path(directory)
        try:
            dir_resolved = dir_path.resolve()
        except OSError:
            dir_resolved = dir_path
        in_protected = False
        for d in protected_abs:
            if dir_resolved == d:
                in_protected = True
                break
            try:
                dir_resolved.relative_to(d)
                in_protected = True
                break
            except ValueError:
                continue
        if not in_protected:
            excluded |= _EXCLUDE_METADATA(directory, names)
        # 保护目录自身或其祖先目录名可能匹配 _EXCLUDE_ALWAYS/_EXCLUDE_METADATA
        # 模式（如产物目录 "dist"/嵌套产物 "build/www" 的祖先 "build" 匹配构建
        # 产物模式），需从排除集中移除该名字，保护目录（含嵌套路径链）被正常复制。
        # extra_excludes 优先级最高，不从中移除（用户显式排除始终生效）。
        if excluded:
            for name in list(excluded):
                child = dir_resolved / name
                for d in protected_abs:
                    if child == d or child in d.parents:
                        excluded.discard(name)
                        break
        if prune_fn is not None:
            excluded |= prune_fn(dir_resolved, names)
        if extra_fn is not None:
            excluded |= extra_fn(directory, names)
        return excluded

    return ignore_fn


def _merge_excludes(base: Callable[..., set[str]], extra: tuple[str, ...]) -> Callable[..., set[str]]:
    """合并内置排除函数与配置额外排除模式.

    返回的函数对同一 ``(directory, names)`` 取两者排除集的并集。
    """
    extra_fn = shutil.ignore_patterns(*extra)

    def combined(directory: str, names: list[str]) -> set[str]:
        return base(directory, names) | extra_fn(directory, names)

    return combined


def _sync_tree(src: Path, dst: Path, ignore_fn: Callable[..., set[str]]) -> None:
    """增量同步 src 到 dst，保留 dst 中的 ``__pycache__`` 以复用 .pyc 缓存.

    1. 删除 dst 中 src 没有的文件/目录（``__pycache__`` 除外）；
    2. 复制 src 中的文件——mtime_ns + size 相同时跳过 ``copy2``（避免重复磁盘写），
       否则用 ``copy2`` 覆盖（保留 mtime 供 compileall 增量判断）。

    用 :func:`os.scandir` 替代 :meth:`Path.iterdir`：``DirEntry.stat`` 复用
    目录枚举时的 stat 缓存，避免对每个文件单独 stat 系统调用。增量同步场景
    下需对比 src 与 dst 的 mtime_ns/size，DirEntry 缓存可减半 stat 调用次数。
    """
    src_names: list[str] = []
    src_entries: dict[str, os.DirEntry[str]] = {}
    try:
        with os.scandir(src) as it:
            for entry in it:
                src_names.append(entry.name)
                src_entries[entry.name] = entry
    except OSError:
        return
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
        _sync_entry(src_entries[name], src / name, dst / name, ignore_fn)


def _sync_entry(
    src_entry: os.DirEntry[str],
    src_item: Path,
    dst_item: Path,
    ignore_fn: Callable[..., set[str]],
) -> None:
    """同步单个 ``src_entry`` 到 ``dst_item``（从 :func:`_sync_tree` 拆分，降低分支数）.

    - 目录：递归 :func:`_sync_tree`
    - 已存在文件：mtime_ns + size 相同跳过，否则 ``copy2`` 覆盖
    - 不存在文件：直接 ``copy2``

    ``DirEntry.stat(follow_symlinks=False)`` 复用枚举缓存，避免独立 stat 调用。
    """
    try:
        is_dir = src_entry.is_dir(follow_symlinks=False)
    except OSError:
        return
    if is_dir:
        dst_item.mkdir(exist_ok=True)
        _sync_tree(src_item, dst_item, ignore_fn)
        return
    if not dst_item.is_file():
        shutil.copy2(src_item, dst_item)
        return
    # mtime_ns + size 相同视为未改动，跳过 copy2 避免不必要的磁盘写
    try:
        src_st = src_entry.stat(follow_symlinks=False)
    except OSError:
        return
    try:
        dst_st = dst_item.stat()
    except OSError:
        shutil.copy2(src_item, dst_item)
        return
    if src_st.st_mtime_ns == dst_st.st_mtime_ns and src_st.st_size == dst_st.st_size:
        return
    shutil.copy2(src_item, dst_item)


def _dir_size(path: Path) -> int:
    """递归计算目录总字节数（文件大小累加，不含目录元数据）.

    实现搬迁至 :func:`fspack._util.fsutil.scandir_dir_size`，此处保留同名薄封装
    维持 ``fspack.packaging.sync._dir_size`` 引用兼容（``pyc.py`` 直接导入）。
    """
    return scandir_dir_size(path)


def _site_packages_fingerprint(sp: Path) -> str:
    """site-packages 指纹：``dist-info`` 目录名排序后哈希，快速检测依赖变化.

    用 :meth:`Path.glob` 直接匹配 ``*.dist-info``，避免 ``iterdir`` 遍历
    site-packages 中数千个文件（如 PySide2）时的 stat 开销。
    """
    if not sp.is_dir():
        return ""
    h = hashlib.sha256()
    for d in sorted(sp.glob("*.dist-info")):
        h.update(d.name.encode())
    return h.hexdigest()
