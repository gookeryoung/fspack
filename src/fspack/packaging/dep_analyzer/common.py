"""二进制依赖分析公共：数据模型 + 常量 + 扫描/入口识别/依赖名解析辅助.

dep_analyzer 子包公共层。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fspack.platform import Platform

# 并行 subprocess 阈值：低于此数走串行，避免线程池启动开销
_PARALLEL_THRESHOLD = 8

# 并行 subprocess 最大 worker 数
_MAX_WORKERS = 8

# 被视为二进制文件的扩展名（按平台筛选）
_BINARY_EXTS: dict[Platform, frozenset[str]] = {
    Platform.WINDOWS: frozenset({".dll", ".pyd", ".exe"}),
    Platform.LINUX: frozenset({".so"}),
    Platform.MACOS: frozenset({".dylib", ".so"}),
}

# 被视为"入口"的二进制扩展名（Python import 加载，不在依赖图中）
_ENTRY_EXTS: frozenset[str] = frozenset({".pyd", ".so"})

# 依赖分析跳过的系统路径前缀（Linux/macOS 系统库不参与剥离判定）
_SYSTEM_PREFIXES: tuple[str, ...] = (
    "/usr/lib",
    "/lib",
    "/System/Library",
    "/usr/local/lib",
)

# 扫描时排除的 dist 根下一级目录名（与 win7_scan._EXCLUDE_PARTS 一致）：
# release/ 是用户安装包与审计产物（SBOM/manifest/安装包），build/ 是打包中间
# 产物，二者均非运行时二进制，参与依赖分析会误删用户安装包
_EXCLUDE_PARTS = frozenset({"build", "release"})


@dataclass(frozen=True)
class BinaryInfo:
    """单个二进制文件信息."""

    path: Path
    """二进制文件绝对路径."""

    deps: tuple[str, ...]
    """依赖的 DLL/.so/.dylib 名称列表（basename 或完整路径，按平台解析）。"""

    @property
    def name_lower(self) -> str:
        """小写文件名（用于跨大小写匹配依赖名）。"""
        return self.path.name.lower()


@dataclass
class DepGraph:
    """依赖图：所有扫描到的二进制及其依赖关系."""

    binaries: dict[Path, BinaryInfo] = field(default_factory=dict)
    """所有扫描到的二进制：``{绝对路径: BinaryInfo}``。"""

    entries: list[Path] = field(default_factory=list)
    """入口二进制路径列表（loader + 所有 .pyd/.so）。"""

    unresolved: list[str] = field(default_factory=list)
    """声明但 dist 内未找到的依赖名（系统库或缺失库）。"""


# ---------------------------------------------------------------------------
# 内部辅助：文件扫描
# ---------------------------------------------------------------------------


def _iter_binary_files(dist_dir: Path, exts: frozenset[str]) -> list[Path]:
    """递归扫描 dist_dir 下指定扩展名的文件（排除 dist 根下 release/build 子树），排序后返回.

    排除规则与 :data:`win7_scan._EXCLUDE_PARTS` 一致：``release/`` 下的安装包与
    审计产物、``build/`` 下的打包中间文件不参与依赖分析，避免依赖剥离误删
    用户安装包（如 ``release/*.zip`` 内的 DLL）。
    """
    result: list[Path] = []
    for path in dist_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        if path.relative_to(dist_dir).parts[0] in _EXCLUDE_PARTS:
            continue
        result.append(path)
    return sorted(result)


def _identify_entries(
    dist_dir: Path,
    runtime_dir: Path,
    target: Platform,
    binaries: dict[Path, BinaryInfo],
) -> list[Path]:
    """识别依赖图入口：.pyd/.so + dist 根 loader + runtime python."""
    entries: list[Path] = []

    for path in binaries:
        if path.suffix.lower() in _ENTRY_EXTS:
            entries.append(path)

    entries.extend(_collect_loader_entries(dist_dir, runtime_dir, target, binaries))
    return entries


def _collect_loader_entries(
    dist_dir: Path,
    runtime_dir: Path,
    target: Platform,
    binaries: dict[Path, BinaryInfo],
) -> list[Path]:
    """收集 loader 可执行文件与 runtime python 解释器作为入口."""
    entries: list[Path] = []
    if target is Platform.WINDOWS:
        for exe in dist_dir.glob("*.exe"):
            if exe in binaries:
                entries.append(exe)
        for exe in runtime_dir.glob("python*.exe"):
            if exe in binaries:
                entries.append(exe)
    else:
        for path in dist_dir.iterdir():
            if path.is_file() and path.suffix == "" and path in binaries:
                entries.append(path)
        python_bin = runtime_dir / "python" / "bin"
        if python_bin.is_dir():
            for py in python_bin.glob("python3.*"):
                if py in binaries:
                    entries.append(py)
    return entries


# ---------------------------------------------------------------------------
# 内部辅助：依赖名解析
# ---------------------------------------------------------------------------


def _dep_basename(dep: str, target: Platform) -> str:
    """从依赖名提取 basename，跨二进制匹配用."""
    if not dep:
        return ""
    if target is Platform.WINDOWS:
        return dep.strip()
    return Path(dep.strip()).name


def _is_system_dep(dep: str, target: Platform) -> bool:
    """判断依赖是否为系统库（不参与 dist 内剥离判定）."""
    if not dep:
        return True
    if target is Platform.WINDOWS:
        upper = dep.upper()
        return upper.startswith(
            (
                "API-MS-WIN-",
                "KERNEL32",
                "USER32",
                "GDI32",
                "ADVAPI32",
                "SHELL32",
                "OLE32",
                "OLEAUT32",
                "MSVCRT",
                "NTDLL",
                "WS2_32",
            )
        )
    return any(dep.startswith(p) for p in _SYSTEM_PREFIXES)


def _detect_platform_from_path(path: Path) -> Platform:
    """根据文件扩展名推断目标平台（BFS 时确定依赖名解析方式）."""
    suffix = path.suffix.lower()
    if suffix in (".dll", ".pyd", ".exe"):
        return Platform.WINDOWS
    if suffix == ".dylib":
        return Platform.MACOS
    return Platform.LINUX


def _parse_deps_parallel(
    parse_fn: Callable[[Path, Platform], list[str] | None],
    paths: list[Path],
    target: Platform,
) -> list[list[str] | None]:
    """并行解析多个二进制的依赖，返回与 ``paths`` 同序的结果列表."""
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        return list(pool.map(lambda p: parse_fn(p, target), paths))
