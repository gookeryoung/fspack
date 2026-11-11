"""``pip download`` 参数构造：把目标 Python 版本与平台标签翻译为 pip CLI 参数.

从 :mod:`fspack.packaging.wheels.downloader` 拆分而来（参数构造块）。本模块
只负责命令行参数拼装，不执行进程、不解析输出。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from fspack.config.versions import _split_t_suffix

__all__ = ["_build_pip_download_args"]


def _build_pip_download_args(
    py: str,
    py_version: str,
    platform_tags: Sequence[str],
    cache_dir: Path,
) -> list[str]:
    """构造 ``pip download`` 基础参数（不含 ``-i index`` 与包名）.

    自由线程版本（PEP 703/779，``py_version`` 末尾 ``t`` 后缀）：
    - ``--python-version`` 传纯数字 ``3.13``（pip 不识别 ``t`` 后缀，
      ``--python-version 3.13t`` 报 "each version part must be an integer"）
    - ``--abi`` 传 ``cp313t``（free-threaded wheel abi tag，与标准版 cp313 不互通；
      pip 按 ``cp313-cp313t-<platform>`` 组合兼容 tag，正确命中 freethreaded wheel）
    """
    base, is_t = _split_t_suffix(py_version)
    major, minor = base.split(".")[:2]
    py_ver_arg = f"{major}.{minor}"
    abi_arg = f"cp{major}{minor}{'t' if is_t else ''}"
    platform_args: list[str] = []
    for tag in platform_tags:
        platform_args.extend(["--platform", tag])
    return [
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
        py_ver_arg,
        "--abi",
        abi_arg,
        "--implementation",
        "cp",
        "--only-binary=:all:",
    ]
