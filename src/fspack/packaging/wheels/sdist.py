"""Wheel sdist 回退构建：``pip wheel --no-deps`` 从 sdist 构建纯 Python wheel.

从 :mod:`fspack.packaging.wheels.downloader` 拆分而来，封装 sdist 回退逻辑。
``--only-binary=:all:`` 无法下载无 wheel 的包（如 odfpy 仅有 sdist）时，
用 ``pip wheel --no-deps`` 从 sdist 构建纯 Python wheel（``py3-none-any``），
构建产物放入 cache_dir 供后续 ``pip download --find-links`` 使用。

依赖 :mod:`fspack.packaging.wheels.downloader` 提供 ``_stream_subprocess``（惰性导入
避免循环依赖：``downloader`` 顶层导入本模块，本模块不能顶层导入 ``downloader``）。
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Sequence

from fspack.exceptions import DependencyError

__all__ = ["_build_sdist_wheels", "_handle_sdist_fallback", "_parse_missing_packages"]

_logger = logging.getLogger(__name__)

# 匹配 pip download stderr 中的 "Could not find a version that satisfies the requirement <pkg>"
_MISSING_PKG_RE = re.compile(r"Could not find a version that satisfies the requirement (.+?) \(from versions:")


def _parse_missing_packages(stderr: str) -> list[str]:
    """从 pip download stderr 解析找不到 wheel 的依赖列表。

    匹配 ``Could not find a version that satisfies the requirement <pkg>``，
    返回去重的依赖字符串列表（含版本 specifier，供 pip wheel 使用）。
    """
    seen: set[str] = set()
    result: list[str] = []
    for m in _MISSING_PKG_RE.finditer(stderr):
        req = m.group(1).strip()
        if req and req not in seen:
            seen.add(req)
            result.append(req)
    return result


def _handle_sdist_fallback(  # noqa: PLR0913
    e: DependencyError,
    py: str,
    pypi_index: str,
    cache_dir: Path,
    *,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
) -> list[str]:
    """处理 sdist 回退：解析缺失包并构建无 wheel 的包，返回缺失包列表。

    无缺失包时重新抛出原异常（无法用 sdist 回退解决）。调用方据此重试下载。

    ``extra_index_urls``/``find_links`` 透传给 ``pip wheel``，确保私有 PyPI 服务器
    或本地 wheel 目录中的 sdist 也能被构建为 wheel。
    """
    missing = _parse_missing_packages(str(e))
    if not missing:
        raise e from None
    _logger.info("尝试用 pip wheel 构建无 wheel 的包: %s", ", ".join(missing))
    _build_sdist_wheels(missing, py, pypi_index, cache_dir, extra_index_urls, find_links)
    return missing


def _build_sdist_wheels(  # noqa: PLR0913
    packages: list[str],
    py: str,
    pypi_index: str,
    cache_dir: Path,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
) -> None:
    """用 ``pip wheel --no-deps`` 从 sdist 构建 wheel（纯 Python 包无 wheel 时回退）。

    ``pip download --only-binary=:all:`` 无法下载无 wheel 的包（如 odfpy 仅有 sdist）。
    ``pip wheel --no-deps`` 可从 sdist 构建纯 Python wheel（``py3-none-any``），
    构建产物放入 cache_dir 供后续 ``pip download --find-links`` 使用。

    构建失败仅 warning（可能是 C 扩展包无法在当前环境编译），
    不影响后续重试——重试失败时抛出原始下载错误。

    ``extra_index_urls``/``find_links`` 透传给 ``pip wheel``，确保私有包源中的
    sdist 也能被构建。
    """
    # 惰性导入打破循环依赖：downloader 顶层导入本模块，本模块不能顶层导入 downloader
    from fspack.packaging.wheels.downloader import _stream_subprocess

    extra_args: list[str] = []
    for url in extra_index_urls:
        extra_args.extend(["--extra-index-url", url])
    for link in find_links:
        extra_args.extend(["--find-links", link])
    for pkg in packages:
        cmd = [py, "-m", "pip", "wheel", "--no-deps", "-w", str(cache_dir), "-i", pypi_index, *extra_args, pkg]
        try:
            _stream_subprocess(cmd)
        except subprocess.CalledProcessError as e:
            _logger.warning("pip wheel 构建失败 %s: %s", pkg, (e.stderr or "").strip())
        except FileNotFoundError as e:
            raise DependencyError(f"未找到 pip: {cmd[0]}") from e
