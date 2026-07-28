"""二进制依赖分析：解析 .dll/.so/.dylib 依赖树，剥离无引用文件.

跨平台策略（无新依赖）：

- **Windows**：纯 Python 解析 PE 导入表（避免引入 ``pefile`` 依赖）。
  读 DOS header → PE header → DataDirectory[1] Import Table →
  IMAGE_IMPORT_DESCRIPTOR 数组，每个 descriptor 的 Name 字段是 RVA，
  经 section table 转为文件偏移读取 ASCII DLL 名。
- **Linux**：``objdump -p <file>``（binutils 自带，静态解析，支持交叉构建）。
  输出 ``NEEDED libfoo.so.6`` 行。
- **macOS**：``otool -L <file>``（Xcode 自带）。输出每行一个 dylib 路径。

核心流程：

1. :func:`analyze_binary_dependencies` 扫描 ``dist`` 下所有 ``.dll``/``.so``/
   ``.dylib``/``.pyd``，构建依赖图 ``{二进制路径: [依赖名列表]}``
2. :func:`find_unused_binaries` 从入口（loader exe + 所有 .pyd/.so）BFS，
   返回不可达的 ``.dll``/``.so``/``.dylib`` 列表
3. :func:`strip_unused_binaries` 删除未引用二进制，返回节省字节数

入口定义：

- loader 可执行文件（dist 根目录的 exe/无后缀文件）
- 所有 ``.pyd``/``.so``（Python 通过 import 机制加载，不在 PE/ELF 依赖图中）
- 运行时 python 解释器（``runtime/python.exe`` / ``runtime/python/bin/python3.X``）

剥离保守策略：

- 仅剥离**同级或下级目录**的未引用 DLL（避免误删系统 DLL 引用）
- ``@rpath``/``@loader_path`` 引用按 rpath 解析后匹配
- 工具缺失（``objdump``/``otool`` 未安装）跳过该平台分析，不阻断主流程
- ``--analyze-deps`` 默认关闭，仅 CLI 显式启用时执行

公共 API：

- :func:`analyze_binary_dependencies` — 扫描 dist 目录构建依赖图
- :func:`find_unused_binaries` — 返回无引用二进制路径列表
- :func:`strip_unused_binaries` — 删除未引用二进制，返回节省字节数
- :class:`DepGraph` — 依赖图数据类
"""

from __future__ import annotations

import logging
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from fspack.platform import Platform

__all__ = [
    "BinaryInfo",
    "DepGraph",
    "analyze_binary_dependencies",
    "find_unused_binaries",
    "strip_unused_binaries",
]

_logger = logging.getLogger(__name__)

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
    """依赖图：所有扫描到的二进制及其依赖关系.

    ``entries`` 是图入口（loader + .pyd/.so），``binaries`` 是所有扫描到的
    二进制（含入口与被引用的 .dll/.so/.dylib）。``unresolved`` 是依赖图中
    声明但 dist 内未找到的依赖名（系统库或缺失库，不影响剥离判定）。
    """

    binaries: dict[Path, BinaryInfo] = field(default_factory=dict)
    """所有扫描到的二进制：``{绝对路径: BinaryInfo}``。"""

    entries: list[Path] = field(default_factory=list)
    """入口二进制路径列表（loader + 所有 .pyd/.so）。"""

    unresolved: list[str] = field(default_factory=list)
    """声明但 dist 内未找到的依赖名（系统库或缺失库）。"""


def analyze_binary_dependencies(
    dist_dir: Path,
    target: Platform,
    *,
    runtime_dir: Path | None = None,
) -> DepGraph:
    """扫描 ``dist_dir`` 下所有二进制文件，构建依赖图.

    Args:
        dist_dir: dist 目录路径
        target: 目标平台（决定用 PE/objdump/otool 解析）
        runtime_dir: runtime 目录路径（``dist/runtime``），用于识别入口 python
            解释器；None 时默认 ``dist_dir/runtime``

    Returns:
        :class:`DepGraph` 依赖图

    工具缺失（objdump/otool 未安装）时返回空图，不抛异常（不阻断主流程）。
    """
    dist_dir = Path(dist_dir).resolve()
    runtime_dir = Path(runtime_dir).resolve() if runtime_dir else dist_dir / "runtime"

    graph = DepGraph()
    exts = _BINARY_EXTS[target]

    # 1. 扫描所有二进制文件
    for path in _iter_binary_files(dist_dir, exts):
        deps = _parse_dependencies(path, target)
        if deps is None:
            # 工具缺失或解析失败，跳过该文件（不影响其他文件分析）
            continue
        graph.binaries[path] = BinaryInfo(path=path, deps=tuple(deps))

    if not graph.binaries:
        return graph

    # 2. 识别入口：loader 可执行文件 + 所有 .pyd/.so + runtime python 解释器
    graph.entries = _identify_entries(dist_dir, runtime_dir, target, graph.binaries)

    # 3. 收集未解析依赖（dist 内未找到的依赖名）
    dist_basenames = {p.name.lower() for p in graph.binaries}
    for info in graph.binaries.values():
        for dep in info.deps:
            dep_basename = _dep_basename(dep, target)
            if dep_basename and dep_basename.lower() not in dist_basenames and not _is_system_dep(dep, target):
                graph.unresolved.append(dep_basename)

    return graph


def find_unused_binaries(graph: DepGraph) -> list[Path]:
    """从入口 BFS 可达集合，返回不可达的二进制路径列表.

    不可达的二进制（如 Qt6Core.dll 依赖的 ICU 未使用时）可安全剥离。
    系统库（``/usr/lib``/``/System/Library`` 等）不参与可达性判定。
    """
    if not graph.binaries or not graph.entries:
        return []

    # 构建 basename → 路径 映射（大小写不敏感，匹配 Windows DLL 命名）
    by_basename: dict[str, Path] = {}
    for path in graph.binaries:
        by_basename[path.name.lower()] = path

    visited: set[Path] = set()
    queue: list[Path] = [p for p in graph.entries if p in graph.binaries]

    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        info = graph.binaries.get(current)
        if info is None:
            continue
        for dep in info.deps:
            dep_basename = _dep_basename(dep, _detect_platform_from_path(current))
            if not dep_basename:
                continue
            dep_path = by_basename.get(dep_basename.lower())
            if dep_path is not None and dep_path not in visited:
                queue.append(dep_path)

    return [p for p in graph.binaries if p not in visited]


def strip_unused_binaries(unused: list[Path]) -> int:
    """删除未引用二进制文件，返回节省字节数.

    Args:
        unused: 未引用二进制路径列表（由 :func:`find_unused_binaries` 返回）

    Returns:
        删除文件累计字节数
    """
    saved = 0
    for path in unused:
        try:
            size = path.stat().st_size
            path.unlink()
            saved += size
            _logger.info("依赖分析剥离: %s (%d bytes)", path.name, size)
        except OSError as e:
            _logger.warning("依赖分析剥离失败: %s (%s)", path, e)
    return saved


# ---------- 内部辅助：文件扫描 ----------


def _iter_binary_files(dist_dir: Path, exts: frozenset[str]) -> list[Path]:
    """递归扫描 dist_dir 下指定扩展名的文件，返回排序后路径列表.

    排序保证扫描顺序确定（便于测试与日志稳定）。
    """
    result: list[Path] = []
    for path in dist_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in exts:
            result.append(path)
    return sorted(result)


def _identify_entries(
    dist_dir: Path,
    runtime_dir: Path,
    target: Platform,
    binaries: dict[Path, BinaryInfo],
) -> list[Path]:
    """识别依赖图入口：loader 可执行文件 + 所有 .pyd/.so + runtime python.

    入口定义：

    - dist 根目录下的 loader 可执行文件（Windows=.exe，Linux/macOS=无后缀）
    - 所有 ``.pyd``（Windows）/``.so``（Linux/macOS）：Python import 加载
    - runtime python 解释器（``runtime/python.exe`` / ``runtime/python/bin/python3.X``）
    """
    entries: list[Path] = []

    # 1. 所有 .pyd/.so（Python import 加载）
    for path in binaries:
        if path.suffix.lower() in _ENTRY_EXTS:
            entries.append(path)

    # 2. dist 根目录下的 loader 可执行文件 + 3. runtime python 解释器
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
        # Linux/macOS：dist 根目录下无后缀的可执行文件（loader）
        for path in dist_dir.iterdir():
            if path.is_file() and path.suffix == "" and path in binaries:
                entries.append(path)
        # runtime python 解释器
        python_bin = runtime_dir / "python" / "bin"
        if python_bin.is_dir():
            for py in python_bin.glob("python3.*"):
                if py in binaries:
                    entries.append(py)
    return entries


# ---------- 内部辅助：依赖名解析 ----------


def _dep_basename(dep: str, target: Platform) -> str:
    """从依赖名提取 basename，用于跨二进制匹配.

    - Windows PE：依赖名已是 basename（如 ``Qt6Core.dll``），直接返回
    - Linux objdump：``NEEDED libfoo.so.6`` 后的 basename
    - macOS otool：``@rpath/libfoo.dylib`` 或 ``/usr/lib/libSystem.B.dylib`` 取 basename
    """
    if not dep:
        return ""
    if target is Platform.WINDOWS:
        # PE 导入表存储的已是 basename（如 "Qt6Core.dll"）
        return dep.strip()
    # Linux/macOS：取 basename（去路径前缀）
    return Path(dep.strip()).name


def _is_system_dep(dep: str, target: Platform) -> bool:
    """判断依赖是否为系统库（不参与 dist 内剥离判定）."""
    if not dep:
        return True
    if target is Platform.WINDOWS:
        # Windows 系统 DLL（KERNEL32/USER32/api-ms-win-* 等）
        upper = dep.upper()
        return upper.startswith(
            ("API-MS-WIN-", "KERNEL32", "USER32", "GDI32", "ADVAPI32", "SHELL32", "OLE32", "OLEAUT32", "MSVCRT", "NTDLL", "WS2_32")
        )
    # Linux/macOS：检查路径前缀
    return any(dep.startswith(p) for p in _SYSTEM_PREFIXES)


def _detect_platform_from_path(path: Path) -> Platform:
    """根据文件扩展名推断目标平台（用于 BFS 时确定依赖名解析方式）."""
    suffix = path.suffix.lower()
    if suffix in (".dll", ".pyd", ".exe"):
        return Platform.WINDOWS
    if suffix == ".dylib":
        return Platform.MACOS
    return Platform.LINUX


# ---------- 内部辅助：PE 解析（Windows，无 pefile 依赖） ----------


def _parse_pe_imports(path: Path) -> list[str] | None:  # noqa: PLR0911, PLR0912
    """纯 Python 解析 PE 文件导入表，返回依赖 DLL basename 列表.

    解析流程：

    1. 读 DOS header（64 字节），从 ``e_lfanew``（偏移 0x3C）获取 PE header 偏移
    2. 校验 PE signature（``PE\\0\\0``）
    3. 读 COFF header（20 字节），获取 ``NumberOfSections`` 与 Optional header 大小
    4. 读 Optional header magic（0x10b=PE32 / 0x20b=PE32+），定位 DataDirectory[1]
       （Import Table RVA + Size）
    5. 读 Section headers（每个 40 字节），构建 RVA → 文件偏移映射
    6. 遍历 Import Table 的 IMAGE_IMPORT_DESCRIPTOR 数组（每个 20 字节），
       读 Name 字段（RVA → 文件偏移 → ASCII 字符串）作为依赖 DLL basename

    Returns:
        依赖 DLL basename 列表；解析失败返回 None
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None

    if len(data) < 64:
        return None

    # 1. DOS header：e_lfanew @ 0x3C
    try:
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    except struct.error:
        return None

    if pe_offset + 24 > len(data):
        return None

    # 2. PE signature + COFF header
    if data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        return None

    # COFF header（20 字节）：Machine(2) NumberOfSections(2) TimeDateStamp(4)
    # PointerToSymbolTable(4) NumberOfSymbols(4) SizeOfOptionalHeader(2) Characteristics(2)
    try:
        num_sections = struct.unpack_from("<H", data, pe_offset + 6)[0]
        opt_header_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
    except struct.error:
        return None

    opt_offset = pe_offset + 24
    if opt_offset + opt_header_size > len(data):
        return None

    # 3. Optional header magic：PE32=0x10b, PE32+=0x20b
    try:
        magic = struct.unpack_from("<H", data, opt_offset)[0]
    except struct.error:
        return None

    # DataDirectory 起始偏移：PE32=96+16*0, PE32+=112+16*0
    # DataDirectory[1] = Import Table (RVA, Size)
    if magic == 0x10B:  # PE32
        dd_offset = opt_offset + 96
    elif magic == 0x20B:  # PE32+
        dd_offset = opt_offset + 112
    else:
        return None

    # 每个 DataDirectory 条目 8 字节：RVA(4) + Size(4)
    # DataDirectory[1] 在 dd_offset + 8
    if dd_offset + 16 > len(data):
        return None

    try:
        import_rva = struct.unpack_from("<I", data, dd_offset + 8)[0]
    except struct.error:
        return None

    if import_rva == 0:
        # 无导入表（极少数纯本地代码 DLL）
        return []

    # 4. Section headers：紧跟 Optional header，每个 40 字节
    sections_offset = opt_offset + opt_header_size
    sections: list[tuple[int, int, int]] = []  # (virtual_address, size_of_raw, pointer_to_raw)
    for i in range(num_sections):
        sec_offset = sections_offset + i * 40
        if sec_offset + 40 > len(data):
            return None
        try:
            virtual_addr = struct.unpack_from("<I", data, sec_offset + 12)[0]
            size_of_raw = struct.unpack_from("<I", data, sec_offset + 16)[0]
            pointer_to_raw = struct.unpack_from("<I", data, sec_offset + 20)[0]
        except struct.error:
            return None
        sections.append((virtual_addr, size_of_raw, pointer_to_raw))

    def rva_to_offset(rva: int) -> int | None:
        """RVA → 文件偏移转换：找包含该 RVA 的 section."""
        for vaddr, sraw, praw in sections:
            if vaddr <= rva < vaddr + sraw:
                return rva - vaddr + praw
        return None

    # 5. 遍历 IMAGE_IMPORT_DESCRIPTOR 数组（每个 20 字节，全 0 表示结束）
    import_offset = rva_to_offset(import_rva)
    if import_offset is None:
        return None

    deps: list[str] = []
    cursor = import_offset
    while cursor + 20 <= len(data):
        try:
            name_rva, _, _, _, _ = struct.unpack_from("<IIIII", data, cursor)
        except struct.error:
            break

        if name_rva == 0:
            # 数组终止符
            break

        name_offset = rva_to_offset(name_rva)
        if name_offset is not None:
            name = _read_ascii_string(data, name_offset)
            if name:
                deps.append(name)

        cursor += 20

    return deps


def _read_ascii_string(data: bytes, offset: int) -> str:
    """从 ``offset`` 读取 NUL 结尾的 ASCII 字符串."""
    end = data.find(b"\x00", offset)
    if end == -1:
        return ""
    try:
        return data[offset:end].decode("ascii", errors="ignore")
    except (ValueError, IndexError):
        return ""


# ---------- 内部辅助：ELF/Mach-O 解析（objdump/otool） ----------


def _parse_objdump_deps(path: Path) -> list[str] | None:
    """用 ``objdump -p`` 解析 ELF 依赖（NEEDED 条目）.

    输出格式::

        Dynamic Section:
          NEEDED libfoo.so.6
          NEEDED libbar.so.1

    Returns:
        依赖名列表（basename 或完整 soname）；objdump 缺失或解析失败返回 None
    """
    try:
        result = subprocess.run(
            ["objdump", "-p", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    deps: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("NEEDED "):
            deps.append(line[len("NEEDED ") :].strip())
    return deps


def _parse_otool_deps(path: Path) -> list[str] | None:
    """用 ``otool -L`` 解析 Mach-O 依赖.

    输出格式::

        /path/to/foo.dylib:
            /usr/lib/libSystem.B.dylib (compatibility version ...)
            @rpath/libfoo.dylib (compatibility version ...)

    Returns:
        依赖名列表（完整路径或 @rpath/@loader_path 引用）；
        otool 缺失或解析失败返回 None
    """
    try:
        result = subprocess.run(
            ["otool", "-L", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    deps: list[str] = []
    lines = result.stdout.splitlines()
    # 第一行是文件自身路径，跳过
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        # 每行格式："<path> (compatibility version ...)"
        # 取首个空格前的部分作为依赖名
        dep = line.split(" ", 1)[0].strip()
        if dep:
            deps.append(dep)
    return deps


def _parse_dependencies(path: Path, target: Platform) -> list[str] | None:
    """按平台分发依赖解析.

    Returns:
        依赖名列表；解析失败或工具缺失返回 None
    """
    if target is Platform.WINDOWS:
        return _parse_pe_imports(path)
    if target is Platform.MACOS:
        return _parse_otool_deps(path)
    return _parse_objdump_deps(path)
