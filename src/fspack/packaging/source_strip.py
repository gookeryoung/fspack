"""源码剥离：.py 文件删除 + .pyc PEP 3147 legacy 布局迁移.

拆自 :mod:`fspack.packaging.pyc`，仅含非 ``__init__.py`` 源码删除与
``__pycache__`` → legacy 布局迁移逻辑。
"""

from __future__ import annotations

import logging
from pathlib import Path

_logger = logging.getLogger(__name__)


def _strip_compiled_py(  # noqa: PLR0913
    src_dir: Path,
    site_packages: Path,
    entry_rels: frozenset[str],
    optimize: int,
    sp_optimize: int,
    py_version: str,
    data_dirs: tuple[Path, ...] = (),
    web_static_dirs: tuple[Path, ...] = (),
) -> int:
    """剥离 src 与 site-packages 的非 ``__init__.py`` 源码，返回剥离总数.

    src 用 ``optimize``、site-packages 用 ``sp_optimize`` 匹配 .pyc 文件名后缀
    （``cpython-{ver}.opt-{N}.pyc``）。``entry_rels`` 仅对 src 生效（入口文件在
    src 下，需保留 ``.py`` 供 ``runpy`` 定位模块）。

    ``data_dirs`` 为 src 下的数据资源目录绝对路径列表，其下 ``.py`` 不剥离
    （视为完整资源，如 fspack 的 ``assets/templates/`` 含项目模板源码）。

    ``web_static_dirs`` 为 src 下的前端构建产物目录绝对路径列表，与
    ``data_dirs`` 同等保护——其下 ``.py`` 不剥离。仅 ``AppType.WEB`` 项目使用。
    """
    stripped = 0
    if src_dir.is_dir():
        stripped += _strip_py_sources(
            [src_dir],
            entry_rels,
            optimize=optimize,
            py_version=py_version,
            data_dirs=data_dirs,
            web_static_dirs=web_static_dirs,
        )
    if site_packages.is_dir():
        stripped += _strip_py_sources([site_packages], frozenset(), optimize=sp_optimize, py_version=py_version)
    return stripped


def _strip_py_sources(  # noqa: PLR0913
    targets: list[Path],
    entry_rels: frozenset[str] = frozenset(),
    *,
    optimize: int = 0,
    py_version: str = "",
    data_dirs: tuple[Path, ...] = (),
    web_static_dirs: tuple[Path, ...] = (),
) -> int:
    """删除 targets 中非 ``__init__.py`` 的 ``.py`` 源码，返回剥离数量.

    保留 ``__init__.py`` 维持包标识，避免 PEP 420 命名空间包导致 ``.pyc`` 不被加载。

    **PEP 3147 迁移**：删除 ``.py`` 前，将对应的
    ``__pycache__/{stem}.cpython-{ver}{opt}.pyc`` 迁移到 ``{stem}.pyc``（legacy 布局）。
    PEP 3147 规定 ``__pycache__`` 中的 ``.pyc`` 仅在源码 ``.py`` 存在时才被加载，
    删除 ``.py`` 后 Python 不会从 ``__pycache__`` 加载 ``.pyc``，必须迁移到 legacy
    布局才能被 :class:`importlib.machinery.SourcelessFileLoader` 加载。
    若 ``.pyc`` 不存在（编译失败），保留 ``.py`` 避免模块完全丢失。

    ``entry_rels`` 为入口文件相对 ``targets[0]``（dist/src）的 POSIX 路径集合，
    这些文件会被跳过：入口包装器用 ``runpy.run_module``/``run_path`` 调用用户代码，
    需 ``.py`` 存在才能被 ``find_spec`` 定位（``__pycache__`` 下的 ``.pyc`` 不在
    ``FileFinder`` 搜索范围，``.pyd`` 模块无 Python 字节码无法被 ``runpy`` 执行）。

    ``data_dirs``/``web_static_dirs`` 为数据资源/前端构建产物目录绝对路径元组
    （如 ``dist/src/fspack/assets/templates``、``dist/src/dist``），其下 ``.py``
    不剥离：这些目录树视为完整资源原样保留（如 fspack 的项目模板源码、前端构建
    产物中的 JS 工具脚本），下游 ``fsp doctor --test`` 复制后需 ``.py`` 存在才能
    构建。``data_dirs``/``web_static_dirs`` 仅对 ``targets[0]``（src）生效，
    site-packages 无此类目录。
    """
    if py_version:
        major, minor = py_version.split(".")[:2]
        ver_tag = f"cpython-{major}{minor}"
    else:  # pragma: no cover - py_version 始终由 _precompile_pyc 传入
        ver_tag = "cpython-*"
    opt_suffix = "" if optimize == 0 else f".opt-{optimize}"
    pyc_name_pattern = f"{{stem}}.{ver_tag}{opt_suffix}.pyc"

    protected = (*data_dirs, *web_static_dirs)

    stripped = 0
    for d in targets:
        for py in d.rglob("*.py"):
            if py.name == "__init__.py":
                continue
            if _is_in_data_dirs(py, protected):
                continue
            try:
                rel = py.relative_to(d).as_posix()
            except ValueError:  # pragma: no cover - rglob 结果必在 d 下
                rel = ""
            if rel in entry_rels:
                continue
            pyc_in_cache = py.parent / "__pycache__" / pyc_name_pattern.format(stem=py.stem)
            if pyc_in_cache.is_file():
                legacy_pyc = py.parent / f"{py.stem}.pyc"
                try:
                    if legacy_pyc.is_file():
                        legacy_pyc.unlink()
                    pyc_in_cache.rename(legacy_pyc)
                except OSError as e:  # pragma: no cover - 文件系统异常容错
                    _logger.warning("迁移 .pyc 到 legacy 布局失败 %s: %s", pyc_in_cache, e)
                    continue
            else:
                continue
            py.unlink()
            stripped += 1
    return stripped


def _is_in_data_dirs(path: Path, data_dirs: tuple[Path, ...]) -> bool:
    """判断 ``path`` 是否位于任一 ``data_dirs`` 目录树内（含 data-dir 自身）.

    ``Path.is_relative_to`` 是 Python 3.9+，fspack 支持 3.8，用 try/except
    :class:`ValueError` 兼容。``data_dirs`` 为空时直接返回 ``False``（热路径短路）。
    """
    if not data_dirs:
        return False
    for d in data_dirs:
        if path == d:
            return True
        try:
            path.relative_to(d)
            return True
        except ValueError:
            continue
    return False
