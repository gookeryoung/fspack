"""精简打包：按子模块 import 分析选择性解压 wheel。."""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from fspack.exceptions import DependencyError
from fspack.progress import StageRecorder, iter_with_progress
from fspack.wheel_cache import normalize_name, parse_wheel_filename

__all__ = ["classify_entry", "slim_unpack"]

_logger = logging.getLogger(__name__)

# 子模块扩展名：仅这些文件按子模块名选择性保留，其余（含 Qt5*.dll 等原生库）一律保留
_SUBMODULE_EXTS = frozenset({".pyd", ".pyi", ".so"})


def classify_entry(entry: str, top_pkg: str) -> tuple[str, str | None]:
    """分类 wheel 条目归属。

    返回 ``(类别, 子模块名|None)``，类别为 ``"metadata"``/``"shared"``/``"submodule"``：

    - ``metadata``: ``*.dist-info/**`` 元数据，始终保留
    - ``shared``: 包级共享文件（``__init__.py``、``_*.py``、子目录、非目标包文件、
      ``Qt5*.dll`` 等原生库），始终保留——原生库间依赖复杂，剥离易致运行时 DLL 加载失败
    - ``submodule``: 子模块专属文件（``.pyd``/``.pyi``/``.so``），仅当子模块被 import 时保留
    """
    parts = entry.split("/")
    if parts[0].endswith(".dist-info"):
        return ("metadata", None)
    if parts[0] != top_pkg or len(parts) != 2:
        return ("shared", None)
    filename = parts[1]
    if filename.startswith("__init__.") or filename.startswith("_"):
        return ("shared", None)
    suffix = Path(filename).suffix.lower()
    if suffix in _SUBMODULE_EXTS:
        return ("submodule", Path(filename).stem)
    return ("shared", None)


def _detect_top_pkg(whl: Path, whl_pkg: str) -> str | None:
    """从 wheel 条目中找出与 whl_pkg 归一化名匹配的顶层目录名。

    遍历 wheel 条目，返回第一个 ``normalize_name`` 后等于 ``whl_pkg`` 的目录名。
    无匹配时返回 None（调用方走全量解压）。
    """
    try:
        with zipfile.ZipFile(whl) as zf:
            for name in zf.namelist():
                top = name.split("/")[0]
                if not top.endswith(".dist-info") and normalize_name(top) == whl_pkg:
                    return top
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e
    return None


def _full_unpack(whl: Path, dest: Path) -> None:
    """全量解压单个 wheel 到目标目录。."""
    try:
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(dest)
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e


def _slim_extract(whl: Path, dest: Path, top_pkg: str, keep_subs: set[str]) -> None:
    """按需解压 wheel，跳过未保留子模块的文件。."""
    skipped = 0
    try:
        with zipfile.ZipFile(whl) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    zf.extract(info, dest)
                    continue
                category, sub = classify_entry(info.filename, top_pkg)
                if category == "submodule" and sub not in keep_subs:
                    skipped += 1
                    continue
                zf.extract(info, dest)
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e
    if skipped:
        _logger.info("精简 %s: 跳过 %d 个未用子模块文件", whl.name, skipped)


def slim_unpack(
    wheelhouse_dir: Path,
    site_packages_dir: Path,
    submodule_usage: dict[str, frozenset[str]] | None = None,
    keep_modules: set[str] | None = None,
    *,
    stage: StageRecorder | None = None,
) -> int:
    """按子模块 import 分析选择性解压 wheelhouse 内所有 wheel。

    - 合并 ``submodule_usage`` 与 ``keep_modules``（``"PySide2.QtGui" → (PySide2, QtGui)``）
      构建每个包的保留集合
    - 有保留集合的 wheel 按需解压（跳过未保留子模块的 ``.pyd``/``.pyi``/``.so``，
      原生库如 ``Qt5*.dll`` 始终保留）
    - 无保留集合的 wheel 全量解压（向后兼容：纯顶层 import 或无子模块分析时）
    - 返回解包 wheel 数量

    ``stage`` 用于通过 ``iter_with_progress`` 显示解压进度并回写处理项数到 BuildTracker。
    """
    site_packages_dir.mkdir(parents=True, exist_ok=True)

    merged: dict[str, set[str]] = {}
    if submodule_usage:
        for pkg, subs in submodule_usage.items():
            merged[normalize_name(pkg)] = set(subs)
    if keep_modules:
        for spec in keep_modules:
            if "." not in spec:
                continue
            pkg, sub = spec.split(".", 1)
            merged.setdefault(normalize_name(pkg), set()).add(sub)

    wheels = sorted(wheelhouse_dir.glob("*.whl"))
    count = 0
    for whl in iter_with_progress(wheels, "解压 wheel", stage=stage):
        info = parse_wheel_filename(whl.name)
        if info is None:
            _full_unpack(whl, site_packages_dir)
            count += 1
            continue
        whl_pkg = normalize_name(info.name)
        keep_subs = merged.get(whl_pkg)
        if not keep_subs:
            _full_unpack(whl, site_packages_dir)
            count += 1
            continue
        top_pkg = _detect_top_pkg(whl, whl_pkg)
        if top_pkg is None:
            _full_unpack(whl, site_packages_dir)
            count += 1
            continue
        _logger.info("精简解压 %s: 保留子模块 %s", whl.name, ", ".join(sorted(keep_subs)))
        _slim_extract(whl, site_packages_dir, top_pkg, keep_subs)
        count += 1
    if stage is not None and count:
        stage.set_detail(f"{count} wheels 解压")
    return count
