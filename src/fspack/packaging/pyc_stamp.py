"""字节码预编译 stamp 管理.

拆自 :mod:`fspack.packaging.pyc`，仅含 .pyc_stamp 路径计算与键值计算。
"""

from __future__ import annotations

from pathlib import Path


def _pyc_stamp_path(dist_dir: Path) -> Path:
    """预编译 stamp 文件路径：``dist/.pyc_stamp``."""
    return dist_dir / ".pyc_stamp"


def _pyc_stamp_key(
    src_dir: Path,
    site_packages: Path,
    strip_py: bool,
    optimize: int = 0,
    sp_optimize: int = 0,
) -> str:
    """计算预编译 stamp 键：src 指纹 + site-packages 指纹 + strip_py + optimize + sp_optimize.

    ``copy_source`` 在预编译前已将 ``.py`` 同步到 ``dist/src``（``strip_py`` 模式下
    也会重新复制），故 ``src_fp`` 始终反映完整源码状态，无需特殊处理 ``strip_py``
    的 ``.py`` 缺失场景。stamp 键在检查与写入时复用，避免重复计算指纹。

    ``optimize`` 与 ``sp_optimize`` 均纳入 stamp 键：src 与 site-packages 分别用不同
    optimize 级别编译（site-packages 用 ``min(optimize, 1)`` 保留 docstring，见
    :func:`_precompile_pyc`），切换任一级别时强制重编译对应目录。老 stamp（无
    ``sp_optimize`` 字段）自然失效触发全量重编译，避免旧的剥离 docstring 的 .pyc 被加载。
    """
    from fspack.analyzer import source_fingerprint
    from fspack.packaging.sync import _site_packages_fingerprint

    src_fp = source_fingerprint(src_dir) if src_dir.is_dir() else ""
    sp_fp = _site_packages_fingerprint(site_packages)
    return f"{src_fp}|{sp_fp}|{strip_py}|{optimize}|{sp_optimize}"
