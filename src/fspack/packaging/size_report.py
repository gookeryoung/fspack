"""打包产物体积报告.

在 :func:`fspack.packaging.pipeline.build` 完成后输出 dist 体积报告，
按 runtime/src/site-packages/其他 四大类统计，site-packages 按 Top N 包
占比排序，帮助用户定位体积热点。

公共 API：

- :func:`print_size_report` — 扫描 dist 目录并渲染体积报告到控制台
- :func:`collect_size_report` — 扫描 dist 目录返回结构化数据（便于测试）
- :class:`SizeCategory` / :class:`PackageSize` — 数据类
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fspack.console import console
from fspack.progress import fmt_bytes

__all__ = [
    "PackageSize",
    "SizeCategory",
    "collect_size_report",
    "print_size_report",
]

_logger = logging.getLogger(__name__)

# 体积报告 Top N 包数量（site-packages 中按 dist-info 占比排序前 N）
_TOP_N_PACKAGES = 10

# 三大类别名称（与 dist 目录下子目录对应）
_RUNTIME_DIR = "runtime"
_SRC_DIR = "src"
_SITE_PACKAGES_GLOBS = ("runtime/Lib/site-packages", "runtime/python/lib/python*/site-packages")


@dataclass(frozen=True)
class SizeCategory:
    """体积报告单个类别统计."""

    name: str
    size: int
    file_count: int

    @property
    def size_formatted(self) -> str:
        """人类可读体积字符串."""
        return fmt_bytes(self.size)


@dataclass(frozen=True)
class PackageSize:
    """site-packages 单个包体积统计."""

    name: str
    version: str
    size: int
    file_count: int

    @property
    def size_formatted(self) -> str:
        """人类可读体积字符串."""
        return fmt_bytes(self.size)


@dataclass(frozen=True)
class SizeReport:
    """完整的体积报告数据."""

    categories: tuple[SizeCategory, ...]
    top_packages: tuple[PackageSize, ...]
    total_size: int
    total_files: int
    # 未进入 top_packages 的剩余包合计（仅当 packages 数 > top_n 时非零）
    other_packages_size: int = 0
    other_packages_count: int = 0

    @property
    def total_size_formatted(self) -> str:
        """人类可读总体积字符串."""
        return fmt_bytes(self.total_size)


def _dir_size(path: Path) -> tuple[int, int]:
    """递归计算目录总字节数与文件数.

    返回 ``(total_bytes, file_count)``。文件被并发删除或权限问题时跳过，
    不阻断报告生成。
    """
    total = 0
    count = 0
    if not path.is_dir():
        return 0, 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
                count += 1
            except OSError:
                continue
    return total, count


def _find_site_packages(dist_dir: Path) -> Path | None:
    """在 dist 目录下定位 site-packages 目录.

    Windows embed python：``dist/runtime/Lib/site-packages``
    Linux standalone：``dist/runtime/python/lib/python<X.Y>/site-packages``
    """
    for pattern in _SITE_PACKAGES_GLOBS:
        for sp in dist_dir.glob(pattern):
            if sp.is_dir():
                return sp
    return None


def _normalize_pkg_name(name: str) -> str:
    """按 PEP 503 规范化包名：连续的 ``-_.`` 替换为单 ``-``，转小写."""
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_dist_info_name(dist_info_dir: Path) -> tuple[str, str]:
    """从 ``<name>-<version>.dist-info`` 目录名解析包名与版本.

    返回 ``(name, version)``。无法解析时 version 返回空字符串。
    """
    stem = dist_info_dir.name[: -len(".dist-info")]
    parts = stem.rsplit("-", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return stem, ""


def _package_dir_size(
    site_packages: Path,
    dist_info_dir: Path,
    *,
    name_index: dict[str, list[Path]] | None = None,
) -> tuple[int, int]:
    """估算单个包在 site-packages 中的体积.

    通过 ``RECORD`` 文件（wheel 安装时生成）记录的文件列表累加大小，
    无 RECORD 时回退到按包名前缀匹配顶层目录/文件（best effort）。

    ``name_index`` 为预构建的 ``normalized_name → [paths]`` 映射，避免对每个
    dist-info 都扫描整个 site-packages（O(N*M) → O(N+M)）。由调用方
    :func:`collect_size_report` 一次性构建后传入；None 时回退到现场扫描
    （向后兼容单次调用场景）。

    返回 ``(total_bytes, file_count)``。
    """
    # 优先用 RECORD 文件：wheel 安装时生成，记录该包所有文件相对路径
    record = dist_info_dir / "RECORD"
    if record.is_file():
        return _size_from_record(site_packages, record)

    # 回退：按 normalized 包名匹配顶层目录
    pkg_name, _ = _parse_dist_info_name(dist_info_dir)
    normalized = _normalize_pkg_name(pkg_name)
    # 包名含命名空间时只取首段（如 "package.name" → "package"）
    top_name = normalized.split("-")[0].replace("-", "_")
    total = 0
    count = 0
    if name_index is not None:
        # 用预构建索引：O(1) 查找而非 O(M) 扫描
        candidates = name_index.get(normalized, []) + name_index.get(top_name, [])
        seen: set[Path] = set()
        for entry in candidates:
            if entry in seen:
                continue
            seen.add(entry)
            if entry.is_dir():
                sz, n = _dir_size(entry)
                total += sz
                count += n
            elif entry.is_file():
                try:
                    total += entry.stat().st_size
                    count += 1
                except OSError:
                    continue
        return total, count

    for entry in site_packages.iterdir():
        # 跳过 .dist-info/.egg-info 元数据目录（按包名前缀会误匹配）
        if entry.name.endswith((".dist-info", ".egg-info")):
            continue
        entry_norm = _normalize_pkg_name(entry.name).split("-")[0]
        if entry_norm == normalized or entry.name.startswith(top_name + ".") or entry.name == top_name:
            if entry.is_dir():
                sz, n = _dir_size(entry)
                total += sz
                count += n
            elif entry.is_file():
                try:
                    total += entry.stat().st_size
                    count += 1
                except OSError:
                    continue
    return total, count


def _size_from_record(site_packages: Path, record: Path) -> tuple[int, int]:
    """从 dist-info/RECORD 文件累加包体积.

    RECORD 格式每行：``<path>,<hash>,<size>``，path 相对 site-packages。
    """
    total = 0
    count = 0
    try:
        text = record.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        rel_path = parts[0]
        # 跳过 RECORD 自身（size 字段为空）与目录条目
        size_str = parts[2].strip()
        if not size_str:
            continue
        try:
            int(size_str)
        except ValueError:
            continue
        # RECORD 中路径用正斜杠，Path 自动处理跨平台
        target = site_packages / rel_path
        try:
            if target.is_file():
                # 用实际文件大小（RECORD 中的 size 可能与磁盘大小不一致，以磁盘为准）
                total += target.stat().st_size
                count += 1
        except OSError:
            continue
    return total, count


def _build_name_index(site_packages: Path) -> dict[str, list[Path]]:
    """构建 normalized_name → [paths] 索引，加速无 RECORD 时的包体积估算.

    一次性扫描 site-packages 顶层（O(M)），按 PEP 503 规范化名分组。
    后续 :func:`_package_dir_size` 用 O(1) 字典查找替代 O(M) 遍历，
    将 N 个 dist-info 的总体积估算从 O(N*M) 降到 O(N+M)。

    包含两种键：完整 normalized 名（如 ``numpy.libs`` → ``numpy-libs``）
    与首段名（如 ``numpy``），覆盖 :func:`_package_dir_size` 的两种匹配路径。
    """
    index: dict[str, list[Path]] = {}
    for entry in site_packages.iterdir():
        # 跳过 .dist-info/.egg-info 元数据目录，避免与同名的源码目录混淆
        if entry.name.endswith((".dist-info", ".egg-info")):
            continue
        full_norm = _normalize_pkg_name(entry.name)
        index.setdefault(full_norm, []).append(entry)
        # 首段名（处理 package.name 形态）
        first_segment = full_norm.split("-")[0]
        if first_segment != full_norm:
            index.setdefault(first_segment, []).append(entry)
    return index


def collect_size_report(dist_dir: Path, *, top_n: int = _TOP_N_PACKAGES) -> SizeReport:
    """扫描 dist 目录，返回结构化体积报告.

    扫描 ``dist/runtime``、``dist/src`` 与 site-packages 三大类，其余文件
    （exe、entry wrapper、pth 等）归入"其他"。site-packages 按 dist-info
    目录统计 Top N 包占比。

    :param dist_dir: dist 目录路径
    :param top_n: site-packages Top N 包数量，默认 10
    :return: :class:`SizeReport` 数据
    """
    if not dist_dir.is_dir():
        return SizeReport(categories=(), top_packages=(), total_size=0, total_files=0)

    categories: list[SizeCategory] = []

    # runtime 类别
    runtime_dir = dist_dir / _RUNTIME_DIR
    runtime_size, runtime_files = _dir_size(runtime_dir)
    categories.append(SizeCategory(name="runtime", size=runtime_size, file_count=runtime_files))

    # src 类别
    src_dir = dist_dir / _SRC_DIR
    src_size, src_files = _dir_size(src_dir)
    categories.append(SizeCategory(name="src", size=src_size, file_count=src_files))

    # site-packages 类别（在 runtime 内，单独统计 Top N 包）
    site_packages = _find_site_packages(dist_dir)
    sp_size, sp_files = (0, 0)
    packages: list[PackageSize] = []
    if site_packages is not None:
        sp_size, sp_files = _dir_size(site_packages)
        # 预构建 normalized_name → [paths] 索引：O(M) 一次性扫描，避免
        # 每个无 RECORD 的 dist-info 都扫描整个 site-packages（O(N*M)）
        name_index = _build_name_index(site_packages)
        for d in site_packages.glob("*.dist-info"):
            if not d.is_dir():
                continue
            pkg_name, pkg_ver = _parse_dist_info_name(d)
            pkg_size, pkg_files = _package_dir_size(site_packages, d, name_index=name_index)
            if pkg_size > 0:
                packages.append(PackageSize(name=pkg_name, version=pkg_ver, size=pkg_size, file_count=pkg_files))
        packages.sort(key=lambda p: p.size, reverse=True)

    categories.append(SizeCategory(name="site-packages", size=sp_size, file_count=sp_files))

    # 其他类别：dist 根目录下非 runtime/src 的文件（exe、entry wrapper、pth、.entry 等）
    other_size = 0
    other_files = 0
    for entry in dist_dir.iterdir():
        if entry.name in (_RUNTIME_DIR, _SRC_DIR):
            continue
        if entry.is_dir():
            sz, n = _dir_size(entry)
            other_size += sz
            other_files += n
        elif entry.is_file():
            try:
                other_size += entry.stat().st_size
                other_files += 1
            except OSError:
                continue
    categories.append(SizeCategory(name="其他", size=other_size, file_count=other_files))

    total_size = sum(c.size for c in categories)
    total_files = sum(c.file_count for c in categories)
    # 计算 Top N 之外剩余包的合计体积与数量（用于"其他"汇总行）
    top = packages[:top_n]
    rest = packages[top_n:]
    other_size = sum(p.size for p in rest)
    other_count = len(rest)
    return SizeReport(
        categories=tuple(categories),
        top_packages=tuple(top),
        total_size=total_size,
        total_files=total_files,
        other_packages_size=other_size,
        other_packages_count=other_count,
    )


def print_size_report(dist_dir: Path, *, top_n: int = _TOP_N_PACKAGES) -> SizeReport:
    """扫描 dist 目录并渲染体积报告到控制台.

    :param dist_dir: dist 目录路径
    :param top_n: site-packages Top N 包数量，默认 10
    :return: :class:`SizeReport` 数据（便于调用方进一步处理）
    """
    from rich.table import Table

    report = collect_size_report(dist_dir, top_n=top_n)
    if report.total_size == 0:
        _logger.debug("dist 目录为空，跳过体积报告: %s", dist_dir)
        return report

    console.step(f"体积报告：{report.total_size_formatted} / {report.total_files} 文件")

    # 类别分布表
    cat_table = Table(title="类别分布", show_lines=False)
    cat_table.add_column("类别", style="cyan", no_wrap=True)
    cat_table.add_column("体积", justify="right")
    cat_table.add_column("占比", justify="right")
    cat_table.add_column("文件数", justify="right")
    for cat in report.categories:
        pct = f"{cat.size / report.total_size * 100:.1f}%" if report.total_size else "-"
        cat_table.add_row(cat.name, cat.size_formatted, pct, str(cat.file_count))
    cat_table.add_row(
        "总计",
        report.total_size_formatted,
        "100.0%",
        str(report.total_files),
        style="bold",
    )
    console.rich.print(cat_table)

    # Top N 包表
    if report.top_packages:
        console.rich.print()
        pkg_table = Table(title=f"site-packages Top {len(report.top_packages)} 包", show_lines=False)
        pkg_table.add_column("#", justify="right", style="dim")
        pkg_table.add_column("包名", style="cyan", no_wrap=True)
        pkg_table.add_column("版本", style="dim")
        pkg_table.add_column("体积", justify="right")
        pkg_table.add_column("占比", justify="right")
        pkg_table.add_column("文件数", justify="right")
        sp_total = next((c.size for c in report.categories if c.name == "site-packages"), 0)
        for idx, pkg in enumerate(report.top_packages, 1):
            pct = f"{pkg.size / sp_total * 100:.1f}%" if sp_total else "-"
            pkg_table.add_row(
                str(idx),
                pkg.name,
                pkg.version,
                pkg.size_formatted,
                pct,
                str(pkg.file_count),
            )
        # 当剩余包数量 > 0 时追加"其他"汇总行，显示未进入 Top N 的包合计体积与占比
        if report.other_packages_count > 0:
            other_pct = f"{report.other_packages_size / sp_total * 100:.1f}%" if sp_total else "-"
            pkg_table.add_row(
                "-",
                f"其他（{report.other_packages_count} 个包）",
                "",
                fmt_bytes(report.other_packages_size),
                other_pct,
                "",
                style="dim",
            )
        console.rich.print(pkg_table)

    return report
