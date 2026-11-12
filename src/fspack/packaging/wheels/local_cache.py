"""本地 wheel 缓存快速解析：替代 pip ``--no-index`` 预检.

背景：deps 解析缓存未命中时，原流程先跑一次 ``pip download --no-index`` 做
"缓存能否满足"的预检。pip 每次进程启动约 480ms，再对 cache_dir 全量 wheel
做解析，400 个 wheel 的缓存目录实测 1.5–2.7s——比真正下载还慢，且空缓存场景
里这次预检注定失败（纯浪费）。

本模块用 ``uv pip compile --no-index``（Rust/PubGrub，热缓存约 40ms）只做
**本地**解析，再把 ``name==version`` 映射回 cache_dir / find-links 中的
wheel 文件（按目标 Python/ABI/平台 tag 精确匹配）。凡不能确定的情形一律
返回 ``None`` 交由原 pip 预检处理，正确性优先：

- uv 不可用 / free-threaded 目标（uv compile 无 abi 参数，可能选到非 t wheel）
- find-links 含远程 URL、依赖带 direct URL/local path
- 目录内存在 sdist（可能被 pip 现场构建）而顶层 wheel 全部缺失
- uv 解析失败、某个 pin 无对应 wheel、同版本多个可兼容 wheel（无法确定选哪个）
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.packaging.site_packages import normalize_pkg_name
from fspack.packaging.wheels.uv_bridge import _extract_resolved_lines, _resolve_with_uv_offline

if TYPE_CHECKING:
    from fspack.packaging.wheels.resolver import DownloadContext
    from fspack.slim.spec import WheelInfo

__all__ = ["_try_local_resolution"]

_logger = logging.getLogger(__name__)

# 依赖声明中的包名（PEP 508 名称位于 extras/版本约束/marker 之前）
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# 本地目录中的源码归档扩展名：存在 sdist 时 pip 可能现场构建，不能断言"缓存不足"
_SDIST_SUFFIXES = (".tar.gz", ".zip", ".tar.bz2", ".tar.xz", ".tgz")


def _requirement_name(req: str) -> str | None:
    """提取依赖声明的规范化包名；含 direct URL/local path 的返回 ``None``."""
    if "@" in req:
        return None
    m = _REQ_NAME_RE.match(req)
    return normalize_pkg_name(m.group(1)) if m else None


def _local_dirs(ctx: DownloadContext) -> list[Path] | None:
    """本地搜索目录：cache_dir + 用户 find-links；含远程 URL 时返回 ``None``."""
    dirs = [ctx.cache_dir]
    for link in ctx.find_links:
        if "://" in link:
            return None
        dirs.append(Path(link))
    return dirs


def _wheel_info(filename: str) -> WheelInfo | None:
    """解析 wheel 文件名（惰性导入）.

    ``fspack.slim`` 包 ``__init__`` 会连带加载 console/progress 等重量模块，
    顶层导入会让 ``import fspack.builder`` 触发这些导入，故按需导入。
    """
    from fspack.slim.spec import WheelInfo

    return WheelInfo.from_filename(filename)


def _scan_local(dirs: Sequence[Path]) -> tuple[dict[str, list[Path]], bool]:
    """扫描本地目录：规范化包名 → wheel 路径，以及是否存在 sdist 归档."""
    by_name: dict[str, list[Path]] = {}
    has_sdist = False
    for d in dirs:
        if not d.is_dir():
            continue
        for entry in d.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix == ".whl":
                info = _wheel_info(entry.name)
                if info is not None:
                    by_name.setdefault(normalize_pkg_name(info.name), []).append(entry)
            elif entry.name.endswith(_SDIST_SUFFIXES):
                has_sdist = True
    return by_name, has_sdist


def _py_target(py_version: str) -> tuple[str, int, str] | None:
    """目标 (major, minor, abi)；free-threaded（t 后缀）返回 ``None``（不打快路径）."""
    if py_version.endswith("t"):
        return None
    base = py_version.split(".")[:2]
    if len(base) != 2 or not all(p.isdigit() for p in base):
        return None
    major, minor = base
    return major, int(minor), f"cp{major}{minor}"


def _wheel_compatible(info: WheelInfo, py_version: str, platform_tags: Sequence[str]) -> bool:
    """wheel 是否匹配目标解释器/ABI/平台（保守规则，不确定即 False）."""
    target = _py_target(py_version)
    if target is None:
        return False
    major, minor, abi_target = target
    if not any(t in platform_tags for t in info.platform_tags) and "any" not in info.platform_tags:
        return False
    abi = info.abi_tag
    cp_prefix = f"cp{major}"
    for py_tag in info.python_tags:
        if abi == "none":
            if py_tag in {"py3", f"py{major}", f"py{major}{minor}"}:
                return True
        elif abi == abi_target:
            if py_tag == abi_target:
                return True
        elif abi == "abi3":
            # abi3 wheel 可用于更高 minor；cp3X 中的 X 必须 <= 目标 minor
            suffix = py_tag.removeprefix(cp_prefix)
            if py_tag.startswith(cp_prefix) and suffix.isdigit() and int(suffix) <= minor:
                return True
    return False


def _versions_equal(a: str, b: str) -> bool:
    """版本字符串比较（优先 PEP 440，packaging 缺失时退化为原样比较）."""
    if a == b:
        return True
    try:
        from packaging.version import Version
    except ImportError:  # pragma: no cover - 依赖 pip 环境
        return False
    try:
        return Version(a) == Version(b)
    except Exception:
        return False


def _match_wheels(
    pins: Sequence[str],
    by_name: dict[str, list[Path]],
    py_version: str,
    platform_tags: Sequence[str],
) -> list[Path] | None:
    """把 ``name==version`` 列表映射为唯一确定的 wheel 路径；任一不确定返回 ``None``."""
    chosen: list[Path] = []
    for pin in pins:
        name, _, version = pin.partition("==")
        norm = normalize_pkg_name(name)
        candidates: list[Path] = []
        for path in by_name.get(norm, ()):
            info = _wheel_info(path.name)
            if info is None or not _versions_equal(info.version, version):
                continue
            if _wheel_compatible(info, py_version, platform_tags):
                candidates.append(path)
        if len(candidates) != 1:
            return None
        chosen.append(candidates[0])
    return chosen


def _copy_into_cache(paths: Sequence[Path], cache_dir: Path) -> list[Path] | None:
    """确保解析出的 wheel 都位于 cache_dir（find-links 来源需复制，保持下游约定）."""
    copied: list[Path] = []
    for path in paths:
        if path.parent == cache_dir:
            copied.append(path)
            continue
        dst = cache_dir / path.name
        if not dst.is_file():
            try:
                shutil.copy2(path, dst)
            except OSError:
                return None
        copied.append(dst)
    return copied


def _resolve_in_dirs(
    filtered: Sequence[str], names: Sequence[str], dirs: Sequence[Path], ctx: DownloadContext
) -> list[Path] | None:
    """在已确定的本地目录中解析（见 :func:`_try_local_resolution` 返回值语义）."""
    by_name, has_sdist = _scan_local(dirs)
    if not has_sdist and all(n not in by_name for n in names):
        # 顶层包一个都不在本地，pip --no-index 必失败：跳过预检
        return []
    try:
        output = _resolve_with_uv_offline(ctx, list(filtered), cache_dir=ctx.cache_dir)
    except Exception as e:
        _logger.info("本地离线解析不可用，回退 pip 预检: %s", e)
        return None
    pins = _extract_resolved_lines(output)
    if not pins:
        return None
    chosen = _match_wheels(pins, by_name, ctx.py_version, ctx.platform_tags)
    if chosen is None:
        return None
    return _copy_into_cache(chosen, ctx.cache_dir)


def _try_local_resolution(filtered: Sequence[str], ctx: DownloadContext) -> list[Path] | None:
    """尝试纯本地解析依赖，返回 wheel 路径列表.

    Returns:
        - ``list[Path]``：本地缓存可完整满足，调用方可直接使用（不再跑 pip 预检）
        - ``[]``：顶层包在本地目录中**全部缺失**（且无 sdist），pip 预检必然失败，
          调用方应直接走在线下载/离线报错
        - ``None``：本地无法判定（缺 uv、远程 find-links、映射不确定等），
          调用方回退到原 pip ``--no-index`` 预检
    """
    if ctx.uv_path is None:
        return None
    dirs = _local_dirs(ctx)
    if dirs is None:
        return None
    names = [_requirement_name(req) for req in filtered]
    if any(n is None for n in names):
        return None
    # any(n is None) 已排除，此处均为 str
    return _resolve_in_dirs(filtered, [n for n in names if n is not None], dirs, ctx)
