"""pip 输出解析：从 stdout/目录扫描抽取 wheel 列表并消解同包多版本冲突.

从 :mod:`fspack.packaging.wheels.downloader` 拆分而来（输出解析块）。负责
``pip download`` stdout 解析、缓存目录回退扫描、wheel 文件名去重与
METADATA 约束消解、顶层声明版本校验。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fspack.exceptions import DependencyError

__all__ = [
    "_dedup_wheels_by_pkg",
    "_parse_pip_download_wheels",
    "_parse_wheel_names",
    "_select_coherent_wheels",
    "_validate_declared_constraints",
]

_logger = logging.getLogger(__name__)

# 匹配 pip download stdout 中的 "Saved <path>.whl" 和 "File was already downloaded <path>.whl"
_PIP_WHEEL_LINE_RE = re.compile(r"(?:Saved|File was already downloaded)\s+(.+\.whl)", re.IGNORECASE)

# 匹配 pip download 成功结尾行 "Successfully downloaded <name> <name> ..."。
# 这是 pip 最终解析集的权威信号：回溯解析会把被放弃的候选版本也打印进
# Saved/File was already downloaded 行，只有本行列出的包名才是最终选中集。
_PIP_SUCCESS_LINE_RE = re.compile(r"^Successfully downloaded (.+)$", re.MULTILINE | re.IGNORECASE)


def _parse_pip_download_wheels(stdout: str) -> list[str]:
    """解析 pip download stdout，提取本次涉及的 wheel 文件名（含传递依赖）。

    匹配 ``Saved <path>.whl``（新下载）和 ``File was already downloaded <path>.whl``（已存在跳过）。
    返回 wheel 文件名列表（去重保序）。

    pip 回溯解析会把被放弃的候选版本也打印成 Saved/File was already downloaded
    行（如 ``pydantic>=2.6.0,<2.15.0`` 先试 2.13.5 失败再回溯到 2.6.4，两行都在）。
    若 stdout 末尾有 ``Successfully downloaded <name> ...`` 行（最终解析集的权威
    信号），按规范化包名过滤，只保留最终选中的包；同包多版本残留交由
    :func:`_select_coherent_wheels` 做版本消解。
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
    # 并行下载时各 worker 的 stdout 拼接在一起，每个进程各有一行
    # "Successfully downloaded ..."；必须取全部成功行的并集（findall），只取
    # 首行会把最终包集合误缩成第一个 worker 的包（冷缓存下只装 1 个依赖的根因）
    success_lines = _PIP_SUCCESS_LINE_RE.findall(stdout)
    if success_lines and names:
        from fspack.packaging.site_packages import normalize_pkg_name
        from fspack.slim.spec import WheelInfo

        final_pkgs: set[str] = set()
        for line in success_lines:
            final_pkgs.update(normalize_pkg_name(n.strip()) for n in line.split())
        filtered: list[str] = []
        for name in names:
            info = WheelInfo.from_filename(name)
            # 文件名解析失败的 wheel 无法判包名，保守保留
            if info is None or normalize_pkg_name(info.name) in final_pkgs:
                filtered.append(name)
        if len(filtered) < len(names):
            _logger.info("按 Successfully downloaded 最终包名过滤: %d → %d 个 wheel", len(names), len(filtered))
        return filtered
    return names


def _parse_wheel_names(stdout: str, cache_dir: Path) -> tuple[list[str], bool]:
    """解析 pip download stdout 获取 wheel 文件名列表.

    Returns:
        (wheel 文件名列表, 是否回退到目录扫描). 回退扫描时不可作为 deps_key 缓存，
        否则下次命中缓存会返回错误依赖列表（如 requests 命中却返回 pygame wheel）。

    回退到目录扫描时按 PEP 503 规范化包名分组，每包只保留**版本最高**的 wheel
    （用 ``packaging.version.Version`` 做 PEP 440 比较）。wheel 缓存目录是
    跨项目持久化的，不同项目可能曾下载过同包名的不同版本——直接 glob 全部返回
    会把旧版本误带入本次构建，导致 site-packages 里同时存在多个 dist-info。
    """
    wheel_names = _parse_pip_download_wheels(stdout)
    if wheel_names:
        return _select_coherent_wheels(wheel_names, cache_dir), False
    _logger.warning("pip download 输出解析失败，回退到目录扫描")
    # 目录扫描可能包含其他项目遗留的 wheel，不可作为本 deps_key 的缓存
    wheels = sorted(f.name for f in cache_dir.glob("*.whl"))
    # 按规范化包名分组，每包只保留版本最高者（防止缓存里同包多版本被误带入）
    return _select_coherent_wheels(_dedup_wheels_by_pkg(wheels), cache_dir), True


def _dedup_wheels_by_pkg(wheel_names: list[str]) -> list[str]:
    """wheel 文件名列表按包名去重，每包只保留版本最高的 wheel.

    用 ``packaging.version.Version`` 做 PEP 440 比较。pip 运行时 packaging 必然
    已安装（pip 的硬依赖），但 fspack 本身未声明为顶层依赖，故惰性导入。
    解析 wheel 文件名失败（非标准格式）时回退到原顺序保留全部。
    """
    from fspack.packaging.site_packages import normalize_pkg_name
    from fspack.slim.spec import WheelInfo

    # packaging 仅 pip 运行时可用，lazy import 避免 fspack 顶层依赖膨胀
    try:
        from packaging.version import Version as _V
    except ImportError:  # pragma: no cover - pip 必然可用
        _logger.warning("packaging 不可用，wheel 去重降级为原样返回")
        return wheel_names

    best: dict[str, tuple[_V, str]] = {}
    for name in wheel_names:
        info = WheelInfo.from_filename(name)
        if info is None:
            # 无法解析的 wheel 保持原样（best effort）
            continue
        norm = normalize_pkg_name(info.name)
        try:
            ver = _V(info.version)
        except Exception:  # pragma: no cover - 版本字符串异常
            continue
        cur = best.get(norm)
        if cur is None or ver > cur[0]:
            best[norm] = (ver, name)
    result = sorted(path for _, path in best.values())
    dropped = len(wheel_names) - len(result)
    if dropped:
        _logger.info("wheel 缓存按包名去重: %d → %d（剔除 %d 个旧版本）", len(wheel_names), len(result), dropped)
    return result


def _wheel_requires(wheel_path: Path) -> list[str]:
    """读取 wheel 包内 METADATA 的 ``Requires-Dist`` 原文列表；读取失败返回空列表.

    仅用于同包多版本冲突消解（:func:`_select_coherent_wheels`）与测试桩替换，
    不做任何解析——Requirement 解析由调用方按需进行并容忍失败。
    """
    import zipfile

    text = ""
    try:
        with zipfile.ZipFile(wheel_path) as zf:
            for member in zf.namelist():
                if member.endswith(".dist-info/METADATA"):
                    text = zf.read(member).decode("utf-8", errors="replace")
                    break
    except (OSError, zipfile.BadZipFile):
        return []
    return [line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("Requires-Dist:")]


def _parse_wheel_versions(
    groups: dict[str, list[str]],
) -> dict[str, dict[str, Any]] | None:
    """解析各分组候选 wheel 的版本号；任一组解析失败返回 ``None``.

    Args:
        groups: 规范化包名 → 候选 wheel 文件名列表（保序）.

    Returns:
        包名 → {wheel 文件名 → packaging Version}；``packaging`` 不可用或版本号异常时 ``None``.
    """
    from fspack.slim.spec import WheelInfo

    try:
        from packaging.version import Version
    except ImportError:  # pragma: no cover - pip 运行时必然可用
        return None
    versions: dict[str, dict[str, Version]] = {}
    for norm, cands in groups.items():
        vmap: dict[str, Version] = {}
        for fname in cands:
            info = WheelInfo.from_filename(fname)
            try:
                assert info is not None
                vmap[fname] = Version(info.version)
            except Exception:
                return None
        versions[norm] = vmap
    return versions


def _select_coherent_wheels(wheel_names: list[str], cache_dir: Path) -> list[str]:
    """同包多版本 wheel 冲突消解：选出一组相互满足依赖约束的版本.

    pip download 回溯解析会把被放弃的候选版本也打印进 Saved/File was already
    downloaded 行（如 pydantic 2.13.5 与 2.6.4 同时出现），若不去重，同包多个
    版本会被全部解压进 site-packages，import 命中哪个 dist-info 看目录序——
    pydantic 2.13.5 + pydantic-core 2.16.3 不配套导致打包产物无法启动的事故
    即源于此。

    消解策略：

    1. 按规范化包名分组，单候选组直接保留；
    2. 冲突组用 wheel METADATA 的 ``Requires-Dist``（仅无环境标记的依赖）做
       约束回溯：每个包选一个版本，使全部选中 wheel 的依赖约束互相满足。
       候选按 stdout 中出现顺序的**逆序**尝试——pip 回溯从新到旧，最终版本
       最后打印，优先命中 pip 的实际选择；
    3. 无一致解（packaging 不可用 / wheel 文件名或版本无法解析 / METADATA
       缺失导致约束无解）时回退保留每组最后一个 wheel（pip 最终选择的经验
       位置）。

    Args:
        wheel_names: wheel 文件名列表（stdout 解析或目录扫描结果）.
        cache_dir: wheel 缓存目录（读取 METADATA 用）.

    Returns:
        消解后的 wheel 文件名列表，保持原有相对顺序.
    """
    from fspack.packaging.site_packages import normalize_pkg_name
    from fspack.slim.spec import WheelInfo

    try:
        from packaging.requirements import Requirement
    except ImportError:  # pragma: no cover - pip 运行时必然可用
        _logger.warning("packaging 不可用，同包多版本消解降级为保留每组最后一个 wheel")
        return _keep_last_wheel_per_pkg(wheel_names)

    # 按规范化包名分组（保序）；文件名无法解析的 wheel 不参与分组，原样保留
    groups: dict[str, list[str]] = {}
    unparsed: list[str] = []
    for name in wheel_names:
        info = WheelInfo.from_filename(name)
        if info is None:
            unparsed.append(name)
            continue
        groups.setdefault(normalize_pkg_name(info.name), []).append(name)
    conflicts = {norm: cands for norm, cands in groups.items() if len(cands) > 1}
    if not conflicts:
        return wheel_names
    _logger.warning(
        "解析结果存在同包多版本 wheel（pip 回溯残留），启动约束消解: %s",
        dict(conflicts),
    )

    # 各 wheel 的无标记 Requires-Dist（带环境标记的依赖无法离线求值，跳过）
    requires: dict[str, list[Requirement]] = {}
    for fname in wheel_names:
        reqs: list[Requirement] = []
        for raw in _wheel_requires(cache_dir / fname):
            try:
                req = Requirement(raw)
            except Exception:  # pragma: no cover - 非法 Requires-Dist 行
                continue
            if req.marker is None:
                reqs.append(req)
        requires[fname] = reqs

    # 各候选 wheel 的版本号；解析失败（含 packaging 不可用）则回退保留最后一个
    versions = _parse_wheel_versions(groups)
    if versions is None:
        _logger.warning("wheel 版本号解析失败，同包多版本消解回退为保留每组最后一个 wheel")
        return _keep_last_wheel_per_pkg(wheel_names)

    def _consistent(assignment: dict[str, str]) -> bool:
        """assignment（包名 → wheel 文件名）中全部选中 wheel 的依赖约束互满足."""
        for fname in assignment.values():
            for req in requires.get(fname, ()):
                target = normalize_pkg_name(req.name)
                other = assignment.get(target)
                if other is None:
                    continue
                if req.specifier and not req.specifier.contains(versions[target][other], prereleases=True):
                    return False
        return True

    conflict_norms = [norm for norm in groups if norm in conflicts]
    assignment: dict[str, str] = {norm: cands[0] for norm, cands in groups.items() if norm not in conflicts}

    def _backtrack(i: int) -> bool:
        if i == len(conflict_norms):
            return _consistent(assignment)
        norm = conflict_norms[i]
        for fname in reversed(groups[norm]):  # 逆序尝试：优先命中 pip 最终选择（最后打印）
            assignment[norm] = fname
            if _backtrack(i + 1):
                return True
            del assignment[norm]
        return False

    if not _backtrack(0):
        _logger.warning("同包多版本约束消解无一致解，回退为保留每组最后一个 wheel")
        return _keep_last_wheel_per_pkg(wheel_names)
    selected = set(assignment.values())
    dropped = [name for name in wheel_names if name not in selected and name not in unparsed]
    _logger.info("同包多版本消解完成: 剔除 %s", dropped)
    return [name for name in wheel_names if name in selected or name in unparsed]


def _keep_last_wheel_per_pkg(wheel_names: list[str]) -> list[str]:
    """同包多版本回退策略：保留每组（按规范化包名）最后一个 wheel.

    pip 回溯从新到旧尝试，最终选中的版本在 stdout 中最后出现；文件名无法
    解析的 wheel 原样保留。
    """
    from fspack.packaging.site_packages import normalize_pkg_name
    from fspack.slim.spec import WheelInfo

    last_per_pkg: dict[str, str] = {}
    unparsed: list[str] = []
    for name in wheel_names:
        info = WheelInfo.from_filename(name)
        if info is None:
            unparsed.append(name)
        else:
            last_per_pkg[normalize_pkg_name(info.name)] = name
    keep = set(last_per_pkg.values()) | set(unparsed)
    return [name for name in wheel_names if name in keep]


def _validate_declared_constraints(wheel_names: list[str], requirements: Sequence[str]) -> None:
    """校验解析出的 wheel 版本满足项目顶层声明的版本约束.

    ``download_wheels`` 的硬门禁：任一顶层依赖（如 ``pydantic>=2.6.0,<2.15.0``）
    的选中 wheel 版本超出声明范围即抛 :class:`DependencyError`，禁止打包超出
    项目约定版本的库。仅校验顶层声明（传递依赖约束由解析器保证，另由
    :func:`_select_coherent_wheels` 的 METADATA 约束消解兜底）。

    Args:
        wheel_names: 选中的 wheel 文件名列表.
        requirements: 顶层依赖声明列表（已过 python_version 标记预过滤）.

    Raises:
        DependencyError: 选中 wheel 版本不满足对应顶层声明的 specifier.
    """
    from fspack.packaging.site_packages import normalize_pkg_name
    from fspack.slim.spec import WheelInfo

    try:
        from packaging.requirements import Requirement
        from packaging.version import Version
    except ImportError:  # pragma: no cover - pip 运行时必然可用
        _logger.warning("packaging 不可用，跳过顶层版本约束校验")
        return

    selected: dict[str, str] = {}
    for name in wheel_names:
        info = WheelInfo.from_filename(name)
        if info is not None:
            selected.setdefault(normalize_pkg_name(info.name), name)
    for req_str in requirements:
        try:
            req = Requirement(req_str)
        except Exception:  # pragma: no cover - 非法依赖声明由 pip 报错
            continue
        if not req.specifier:
            continue
        fname = selected.get(normalize_pkg_name(req.name))
        if fname is None:
            continue
        info = WheelInfo.from_filename(fname)
        try:
            assert info is not None
            version = Version(info.version)
        except Exception:  # pragma: no cover - 版本号异常
            continue
        if not req.specifier.contains(version, prereleases=True):
            raise DependencyError(
                f"依赖解析结果超出项目版本约束: {fname} 不满足声明 {req_str}。"
                f"请检查 wheel 缓存（{normalize_pkg_name(req.name)} 相关 wheel）是否被其他项目污染，"
                f"或依赖声明是否存在环境标记分歧"
            )
