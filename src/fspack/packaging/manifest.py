"""构建产物清单（manifest）生成与差异对比.

在构建完成后扫描 ``dist`` 目录，按相对路径记录每个文件的大小、SHA256
校验和、归属分类（runtime/site-packages/src/release 等），生成 JSON
到 ``dist/release/<name>-<version>-manifest.json``，便于版本间对比产物变化。

差异对比（``fsp manifest diff a.json b.json``）输出：
- 新增文件（added）
- 删除文件（removed）
- 内容变更文件（modified：大小或 sha256 不同）
- 分类汇总表（按 category 汇总新增/删除/变更的字节数）

公共 API：

- :func:`generate_manifest` — 扫描 dist 生成 manifest JSON 文件，返回路径
- :func:`collect_manifest` — 扫描 dist 返回结构化 manifest 数据（便于测试）
- :func:`diff_manifest` — 对比两份 manifest 数据返回差异字典
- :func:`print_manifest_diff` — 将差异字典格式化输出到控制台
- :class:`ManifestEntry` — 单个文件的 manifest 条目数据类
- :class:`ManifestDiff` — 差异结果数据类（含 added/removed/modified）

文件分类规则（``_categorize``）：
- ``runtime``：dist/runtime/ 下（含 embed/standalone 的 Python 运行时）
- ``site-packages``：dist/site-packages/ 下（第三方依赖与 stdlib 精简后剩余）
- ``src``：dist/src/ 下（用户源码与数据资源）
- ``release``：dist/release/ 下（SBOM/manifest/安装包等审计与发行产物）
- ``build``：dist/build/ 下（图标转换、NSIS 脚本等中间产物）
- ``other``：dist 根目录或其他路径（如 .build_failed 标记、python3xx._pth 等）
"""

from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fspack.config import ProjectInfo
from fspack.fsutil import atomic_write_text, scandir_tree

__all__ = [
    "ManifestDiff",
    "ManifestEntry",
    "collect_manifest",
    "diff_manifest",
    "generate_manifest",
    "load_manifest",
    "print_manifest_diff",
]

_logger = logging.getLogger(__name__)

# manifest 版本号：格式变更时递增，便于 diff 做兼容判断
_MANIFEST_VERSION = 1

# 分类：目录前缀 -> 分类名（dist 相对 POSIX 前缀匹配，按顺序优先匹配）
_CATEGORY_RULES: tuple[tuple[str, str], ...] = (
    ("runtime/", "runtime"),
    ("site-packages/", "site-packages"),
    ("src/", "src"),
    ("release/", "release"),
    ("build/", "build"),
)


@dataclass(frozen=True)
class ManifestEntry:
    """单个文件的 manifest 条目."""

    path: str  # 相对 dist 根的 POSIX 路径
    size: int  # 字节数
    sha256: str  # 64 字符 SHA256 十六进制
    category: str  # 分类：runtime/site-packages/src/release/build/other


@dataclass(frozen=True)
class ManifestDiff:
    """两份 manifest 的差异结果."""

    added: list[ManifestEntry]  # 仅新 manifest 存在的文件
    removed: list[ManifestEntry]  # 仅旧 manifest 存在的文件
    modified: list[tuple[ManifestEntry, ManifestEntry]]  # (old, new) 内容变化的文件

    @property
    def is_empty(self) -> bool:
        """是否无任何差异."""
        return not self.added and not self.removed and not self.modified


def collect_manifest(dist_dir: Path, info: ProjectInfo) -> dict[str, Any]:
    """扫描 dist 目录收集 manifest 数据，返回可序列化字典.

    遍历 ``dist_dir`` 下所有文件（通过 :func:`scandir_tree` 复用 stat
    缓存），计算每个文件的 SHA256，按相对路径排序（保证可重现性）。
    跳过 manifest JSON 自身（避免每次生成后校验和变化）。

    Args:
        dist_dir: dist 根目录（``dist/``）
        info: 项目元信息（用于 manifest 文档名与版本）

    Returns:
        manifest 字典（含 version/created/project/total_size/total_files/entries）
    """
    dist_dir = Path(dist_dir)
    dist_str = str(dist_dir)
    # manifest 自身文件名（用于扫描时跳过），与 generate_manifest 保持一致
    self_name = f"{info.name}-{info.version}-manifest.json"

    # 第一遍：物化目录条目并过滤（scandir_tree 为生成器，二次遍历会重复扫描）
    collected: list[tuple[str, os.DirEntry[str]]] = []
    for dir_entry in scandir_tree(dist_dir):
        try:
            rel = os.path.relpath(dir_entry.path, dist_str).replace(os.sep, "/")
        except ValueError:
            # 跨盘符等场景无法求相对路径时跳过（非典型场景）
            continue
        # 跳过 manifest 自身：避免每次生成后 sha256 变化
        if rel == f"release/{self_name}":
            continue
        # 跳过 release/ 下隐藏文件与 SBOM 文件：manifest 与 SBOM 并行生成时，
        # mkstemp 临时文件（".tmp_" 前缀）与半成品 SBOM 可能被扫到导致内容抖动
        if rel.startswith("release/.") or rel.endswith("-sbom.json"):
            continue
        collected.append((rel, dir_entry))

    # 第二遍：并行计算 SHA256（hashlib.sha256 释放 GIL 可真并行），
    # 结果按 scandir 顺序聚合保序；条目组装留在主线程（避免共享可变状态）。
    # symlink 条目不跟随链接计算哈希（size 已用 follow_symlinks=False），
    # sha256 记空串保持条目存在，修复口径不一致。
    entries: list[ManifestEntry] = []
    total_size = 0
    max_workers = min(8, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            None if dir_entry.is_symlink() else executor.submit(_sha256_file, Path(dir_entry.path))
            for _, dir_entry in collected
        ]
        for (rel, dir_entry), future in zip(collected, futures):
            try:
                size = dir_entry.stat(follow_symlinks=False).st_size
            except OSError:
                # 扫描后文件被并发删除/权限变化：跳过该条目
                continue
            sha256 = "" if future is None else future.result()
            entries.append(
                ManifestEntry(
                    path=rel,
                    size=size,
                    sha256=sha256,
                    category=_categorize(rel),
                )
            )
            total_size += size

    # 按相对路径排序：保证同一 dist 多次生成 manifest 字段顺序一致
    entries.sort(key=lambda e: e.path)

    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "manifestVersion": _MANIFEST_VERSION,
        "created": created,
        "project": {
            "name": info.name,
            "version": info.version,
            "py_version": info.py_version,
        },
        "summary": {
            "total_size": total_size,
            "total_files": len(entries),
            "category_count": _category_count(entries),
        },
        "entries": [asdict(e) for e in entries],
    }


def generate_manifest(dist_dir: Path, info: ProjectInfo) -> Path:
    """生成 manifest JSON 文件到 ``dist/release/<name>-<version>-manifest.json``.

    Args:
        dist_dir: dist 根目录
        info: 项目元信息

    Returns:
        生成的 manifest 文件路径
    """
    import json

    release_dir = dist_dir / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = release_dir / f"{info.name}-{info.version}-manifest.json"
    data = collect_manifest(dist_dir, info)
    atomic_write_text(manifest_path, json.dumps(data, ensure_ascii=False, indent=2))
    _logger.info(
        "产物清单已生成: %s（%d 个文件，共 %s）",
        manifest_path,
        data["summary"]["total_files"],
        _format_size(data["summary"]["total_size"]),
    )
    return manifest_path


def load_manifest(path: Path) -> dict[str, Any]:
    """从 JSON 文件加载 manifest 字典（``diff_manifest`` 的便捷入口）.

    Args:
        path: manifest JSON 路径

    Returns:
        ``collect_manifest`` 格式的字典

    Raises:
        ValueError: manifest 版本不兼容或结构非法
    """
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "entries" not in data or "manifestVersion" not in data:
        raise ValueError(f"非法 manifest 结构: {path}")
    if data["manifestVersion"] != _MANIFEST_VERSION:
        raise ValueError(f"manifest 版本不兼容: {data['manifestVersion']}（当前支持 v{_MANIFEST_VERSION}）")
    return data


def diff_manifest(old: dict[str, Any], new: dict[str, Any]) -> ManifestDiff:
    """对比两份 manifest 数据返回差异.

    通过 ``path`` 作为键匹配：
    - 仅新 manifest 有 → added
    - 仅旧 manifest 有 → removed
    - 两者都有但 size 或 sha256 不同 → modified（(old_entry, new_entry)）

    Args:
        old: 旧 manifest 字典（``collect_manifest`` 或 ``load_manifest`` 返回）
        new: 新 manifest 字典

    Returns:
        :class:`ManifestDiff` 差异结果（added/removed/modified 按 path 排序）
    """
    old_map = {e["path"]: _entry_from_dict(e) for e in old.get("entries", [])}
    new_map = {e["path"]: _entry_from_dict(e) for e in new.get("entries", [])}

    old_keys = set(old_map)
    new_keys = set(new_map)

    added_paths = sorted(new_keys - old_keys)
    removed_paths = sorted(old_keys - new_keys)
    common = sorted(old_keys & new_keys)

    added = [new_map[p] for p in added_paths]
    removed = [old_map[p] for p in removed_paths]
    modified: list[tuple[ManifestEntry, ManifestEntry]] = []
    for p in common:
        o = old_map[p]
        n = new_map[p]
        if o.size != n.size or o.sha256 != n.sha256:
            modified.append((o, n))

    return ManifestDiff(added=added, removed=removed, modified=modified)


def print_manifest_diff(diff: ManifestDiff) -> None:
    """将差异结果格式化输出到控制台（用 :mod:`fspack.console` 的 rich 控制台）.

    输出顺序：概览 → 新增 → 删除 → 修改 → 分类汇总字节数。
    无差异时只输出"无差异"。

    Args:
        diff: :func:`diff_manifest` 返回的差异结果
    """
    from fspack.console import console

    if diff.is_empty:
        console.success("两份 manifest 无差异")
        return

    added_count = len(diff.added)
    removed_count = len(diff.removed)
    modified_count = len(diff.modified)

    console.step(f"差异概览：新增 {added_count}，删除 {removed_count}，修改 {modified_count}")

    # 按分类汇总字节数：新增记 +size，删除记 -size，修改记 new-old（变化量）
    delta_by_cat: dict[str, int] = {}

    def _acc(cat: str, delta: int) -> None:
        delta_by_cat[cat] = delta_by_cat.get(cat, 0) + delta

    if diff.added:
        console.rich.print()
        console.step(f"新增文件（{added_count}）:")
        for e in diff.added:
            _acc(e.category, e.size)
            console.rich.print(f"  [green]+[/green] {e.path}  [dim]{_format_size(e.size)}  [{e.category}][/dim]")

    if diff.removed:
        console.rich.print()
        console.step(f"删除文件（{removed_count}）:")
        for e in diff.removed:
            _acc(e.category, -e.size)
            console.rich.print(f"  [red]-[/red] {e.path}  [dim]-{_format_size(e.size)}  [{e.category}][/dim]")

    if diff.modified:
        console.rich.print()
        console.step(f"修改文件（{modified_count}）:")
        for old_e, new_e in diff.modified:
            delta = new_e.size - old_e.size
            _acc(new_e.category, delta)
            size_change = f"{delta:+d}B" if abs(delta) < 1024 else f"{_format_size_delta(delta)}"
            sha_changed = "sha256 变更" if old_e.sha256 != new_e.sha256 else "仅大小变化"
            console.rich.print(
                f"  [yellow]~[/yellow] {new_e.path}  [dim]{size_change}  [{new_e.category}]  ({sha_changed})[/dim]"
            )

    if delta_by_cat:
        console.rich.print()
        console.step("分类汇总（字节变化量）:")
        total_delta = 0
        for cat in sorted(delta_by_cat):
            d = delta_by_cat[cat]
            total_delta += d
            fmt = _format_size_delta(d)
            color = "green" if d > 0 else ("red" if d < 0 else "")
            console.rich.print(f"  {cat:<14} {f'[{color}]{fmt}[/]' if color else fmt}")
        total_fmt = _format_size_delta(total_delta)
        color = "green" if total_delta > 0 else ("red" if total_delta < 0 else "")
        console.rich.print(f"  {'合计':<14} {f'[{color}]{total_fmt}[/]' if color else total_fmt}")


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """流式计算文件 SHA256（64 字符十六进制），避免大文件一次性读入内存."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        # 文件被并发删除或权限问题：返回空字符串（调用方可据此判断）
        return ""
    return h.hexdigest()


def _categorize(rel_posix: str) -> str:
    """按 dist 相对 POSIX 路径将文件归类.

    匹配顺序与规则见 :data:`_CATEGORY_RULES`，未命中时返回 ``other``。
    """
    for prefix, category in _CATEGORY_RULES:
        if rel_posix.startswith(prefix):
            return category
    return "other"


def _category_count(entries: list[ManifestEntry]) -> dict[str, dict[str, int]]:
    """按分类汇总文件数与总字节数（用于 manifest.summary.category_count）."""
    result: dict[str, dict[str, int]] = {}
    for e in entries:
        bucket = result.setdefault(e.category, {"files": 0, "size": 0})
        bucket["files"] += 1
        bucket["size"] += e.size
    return result


def _entry_from_dict(d: dict[str, Any]) -> ManifestEntry:
    """从字典构造 :class:`ManifestEntry`（兼容字段缺失场景）."""
    return ManifestEntry(
        path=str(d.get("path", "")),
        size=int(d.get("size", 0)),
        sha256=str(d.get("sha256", "")),
        category=str(d.get("category", "other")),
    )


def _format_size(n: int) -> str:
    """格式化为可读文件大小（B/KB/MB/GB），与 size_report 保持一致风格."""
    if n < 1024:
        return f"{n}B"
    units = ["KB", "MB", "GB", "TB"]
    val = n / 1024.0
    for u in units:
        if val < 1024.0:
            return f"{val:.2f}{u}"
        val /= 1024.0
    return f"{val:.2f}PB"


def _format_size_delta(delta: int) -> str:
    """带符号的大小格式化（正数 +，负数 -），用于差异输出."""
    if delta == 0:
        return _format_size(0)
    sign = "+" if delta > 0 else "-"
    return f"{sign}{_format_size(abs(delta))}"
