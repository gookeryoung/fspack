"""字节码预编译 stamp 管理.

拆自 :mod:`fspack.packaging.pyc`，仅含 .pyc_stamp 路径计算与键值计算。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _pyc_stamp_path(dist_dir: Path) -> Path:
    """预编译 stamp 文件路径：``dist/.pyc_stamp``."""
    return dist_dir / ".pyc_stamp"


def _pyc_stamp_key(  # noqa: PLR0913
    src_dir: Path,
    site_packages: Path,
    strip_py: bool,
    optimize: int = 0,
    sp_optimize: int = 0,
    entry_rels: frozenset[str] | None = None,
) -> str:
    """计算预编译 stamp 键：src 指纹 + site-packages 指纹 + strip_py + optimize + sp_optimize + entry_rels + py 版本.

    ``copy_source`` 在预编译前已将 ``.py`` 同步到 ``dist/src``（``strip_py`` 模式下
    也会重新复制），故 ``src_fp`` 始终反映完整源码状态，无需特殊处理 ``strip_py``
    的 ``.py`` 缺失场景。stamp 键在检查与写入时复用，避免重复计算指纹。

    ``optimize`` 与 ``sp_optimize`` 均纳入 stamp 键：src 与 site-packages 分别用不同
    optimize 级别编译（site-packages 用 ``min(optimize, 1)`` 保留 docstring，见
    :func:`_precompile_pyc`），切换任一级别时强制重编译对应目录。老 stamp（无
    ``sp_optimize`` 字段）自然失效触发全量重编译，避免旧的剥离 docstring 的 .pyc 被加载。

    ``entry_rels`` 纳入 stamp 键（与 Nuitka :meth:`_stamp_key` 对齐）：入口文件集合
    决定哪些 ``.py`` 保留源码形态（供 ``runpy.run_path()`` 定位）而不被剥离，
    集合变化时剥离范围变化，须强制重编译与重剥离。排序后拼接保证顺序无关。

    构建机 Python 版本（``major.minor.micro``）纳入 stamp 键：字节码格式随
    Python 版本变化（如 3.13 的 pyc 格式调整），切换构建机解释器时强制
    重编译。老 stamp 自然失效触发一次全量重编译。
    """
    from fspack.analyzer.fingerprint import cached_source_fingerprint
    from fspack.packaging.sync import _site_packages_fingerprint

    src_fp = cached_source_fingerprint(src_dir) if src_dir.is_dir() else ""
    sp_fp = _site_packages_fingerprint(site_packages)
    entry_part = "|".join(sorted(entry_rels)) if entry_rels else ""
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return f"{src_fp}|{sp_fp}|{strip_py}|{optimize}|{sp_optimize}|{entry_part}|{py_ver}"
