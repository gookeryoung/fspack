"""``dep_analyzer`` 二进制依赖分析单元测试.

覆盖：

- :func:`_parse_pe_imports` 纯 Python PE 导入表解析（构造最小 PE 文件）
- :func:`_parse_objdump_deps` / :func:`_parse_otool_deps` 输出解析（mock subprocess）
- :func:`analyze_binary_dependencies` 依赖图构建（含工具缺失回退）
- :func:`find_unused_binaries` BFS 可达性
- :func:`strip_unused_binaries` 删除与字节统计
- :func:`_analyze_binary_dependencies` 阶段函数集成（BuildContext）
- 辅助函数：``_dep_basename``/``_is_system_dep``/``_detect_platform_from_path``/
  ``_iter_binary_files``/``_identify_entries``
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType, BuildConfig, BuildOptions, MirrorConfig, ProjectInfo
from fspack.packaging.dep_analyzer import (
    BinaryInfo,
    DepGraph,
    _collect_loader_entries,
    _dep_basename,
    _detect_platform_from_path,
    _identify_entries,
    _is_system_dep,
    _iter_binary_files,
    _parse_objdump_deps,
    _parse_otool_deps,
    _parse_pe_imports,
    _read_ascii_string,
    analyze_binary_dependencies,
    find_unused_binaries,
    strip_unused_binaries,
)
from fspack.packaging.pipeline.stages import BuildContext, _analyze_binary_dependencies
from fspack.platform import Platform
from fspack.progress import BuildTracker


@dataclass
class _SubprocessResult:
    """subprocess.run 返回值桩，用于 mock objdump/otool 调用."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


# ---------- 辅助：构造最小 PE 文件 ----------


def _make_minimal_pe(deps: list[str]) -> bytes:
    """构造包含指定导入 DLL 名的最小 PE32 文件.

    布局：
    - 0x000: DOS header（64 字节，e_lfanew=0x40）
    - 0x040: PE signature + COFF header（24 字节）
    - 0x058: Optional header PE32（224 字节，含 16 个 DataDirectory）
    - 0x138: Section header（40 字节）
    - 0x200: Section data（import descriptors + DLL 名称字符串）

    单 section ``.rdata``：VirtualAddress=0x1000, PointerToRawData=0x200。
    DataDirectory[1]（Import Table）RVA 指向 section 起始。
    """
    num_sections = 1
    opt_header_size = 224  # PE32: 96 + 16*8
    section_data_offset = 0x200
    section_virtual_addr = 0x1000

    # DOS header
    dos_header = bytearray(64)
    struct.pack_into("<I", dos_header, 0x3C, 0x40)

    # PE signature + COFF header
    pe_sig = b"PE\x00\x00"
    coff_header = bytearray(20)
    struct.pack_into("<H", coff_header, 0, 0x14C)  # Machine: I386
    struct.pack_into("<H", coff_header, 2, num_sections)
    struct.pack_into("<H", coff_header, 16, opt_header_size)
    struct.pack_into("<H", coff_header, 18, 0x0102)  # EXECUTABLE_IMAGE | 32BIT_MACHINE

    # Optional header (PE32)
    opt_header = bytearray(opt_header_size)
    struct.pack_into("<H", opt_header, 0, 0x10B)  # Magic: PE32
    # DataDirectory[1] = Import Table @ dd_offset + 8
    import_rva = section_virtual_addr
    struct.pack_into("<I", opt_header, 96 + 8, import_rva)
    struct.pack_into("<I", opt_header, 96 + 12, 0)  # Size 字段不影响解析

    # Section header
    section_header = bytearray(40)
    section_header[0:8] = b".rdata\x00\x00"
    struct.pack_into("<I", section_header, 12, section_virtual_addr)
    struct.pack_into("<I", section_header, 16, 0x200)  # SizeOfRawData
    struct.pack_into("<I", section_header, 20, section_data_offset)  # PointerToRawData

    # Section data：import descriptors + DLL 名称
    descriptors = bytearray()
    names_data = bytearray()
    names_offset_in_section = (len(deps) + 1) * 20  # descriptors 总大小（含终止符）
    names_rva_start = section_virtual_addr + names_offset_in_section

    for dep in deps:
        name_rva = names_rva_start + len(names_data)
        # IMAGE_IMPORT_DESCRIPTOR：OriginalFirstThunk/TimeDateStamp/ForwarderChain/
        # Name/FirstThunk，DLL 名在 Name 字段（第 4 字段，offset 12）
        descriptors.extend(struct.pack("<IIIII", 0, 0, 0, name_rva, 0))
        names_data.extend(dep.encode("ascii") + b"\x00")
    descriptors.extend(b"\x00" * 20)  # 终止 descriptor

    section_data = bytes(descriptors) + bytes(names_data)

    # 组装
    result = bytes(dos_header) + pe_sig + bytes(coff_header) + bytes(opt_header) + bytes(section_header)
    result += b"\x00" * (section_data_offset - len(result))  # 填充到 0x200
    result += section_data
    return result


# ---------- _parse_pe_imports ----------


class TestParsePeImports:
    """PE 导入表纯 Python 解析."""

    def test_parses_single_dll(self, tmp_path: Path) -> None:
        """单个导入 DLL 正确解析."""
        pe = _make_minimal_pe(["Qt6Core.dll"])
        path = tmp_path / "test.dll"
        path.write_bytes(pe)
        deps = _parse_pe_imports(path)
        assert deps == ["Qt6Core.dll"]

    def test_parses_multiple_dlls(self, tmp_path: Path) -> None:
        """多个导入 DLL 顺序保留."""
        pe = _make_minimal_pe(["Qt6Core.dll", "Qt6Gui.dll", "KERNEL32.dll"])
        path = tmp_path / "test.dll"
        path.write_bytes(pe)
        deps = _parse_pe_imports(path)
        assert deps == ["Qt6Core.dll", "Qt6Gui.dll", "KERNEL32.dll"]

    def test_empty_imports_when_no_deps(self, tmp_path: Path) -> None:
        """无导入项的 PE 返回空列表（import_rva=0 分支）."""
        # 构造 import_rva=0 的 PE：复用 _make_minimal_pe 但手动改 Import Table RVA
        pe = bytearray(_make_minimal_pe(["Qt6Core.dll"]))
        # DataDirectory[1] RVA 在 opt_header 偏移 96+8=104，文件偏移 88+104=192
        struct.pack_into("<I", pe, 192, 0)
        path = tmp_path / "empty.dll"
        path.write_bytes(bytes(pe))
        assert _parse_pe_imports(path) == []

    def test_returns_none_for_too_small_file(self, tmp_path: Path) -> None:
        """小于 64 字节返回 None."""
        path = tmp_path / "tiny.dll"
        path.write_bytes(b"\x00" * 10)
        assert _parse_pe_imports(path) is None

    def test_returns_none_for_bad_pe_signature(self, tmp_path: Path) -> None:
        """PE signature 错误返回 None."""
        pe = bytearray(_make_minimal_pe(["Qt6Core.dll"]))
        # 破坏 PE signature（offset 0x40）
        pe[0x40:0x44] = b"XX\x00\x00"
        path = tmp_path / "bad.dll"
        path.write_bytes(bytes(pe))
        assert _parse_pe_imports(path) is None

    def test_returns_none_for_bad_optional_magic(self, tmp_path: Path) -> None:
        """Optional header magic 未知返回 None."""
        pe = bytearray(_make_minimal_pe(["Qt6Core.dll"]))
        # Optional header magic 在 offset 88
        struct.pack_into("<H", pe, 88, 0x999)
        path = tmp_path / "badmagic.dll"
        path.write_bytes(bytes(pe))
        assert _parse_pe_imports(path) is None

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        """文件不存在（OSError）返回 None."""
        path = tmp_path / "nonexistent.dll"
        assert _parse_pe_imports(path) is None

    def test_returns_none_when_pe_offset_beyond_file(self, tmp_path: Path) -> None:
        """e_lfanew 指向超出文件末尾返回 None."""
        pe = bytearray(64)
        struct.pack_into("<I", pe, 0x3C, 0xFFFF)  # e_lfanew 远超文件
        path = tmp_path / "badoffset.dll"
        path.write_bytes(bytes(pe))
        assert _parse_pe_imports(path) is None

    def test_returns_none_when_optional_header_truncated(self, tmp_path: Path) -> None:
        """Optional header 被截断（opt_offset + opt_header_size > len）返回 None."""
        pe = bytearray(_make_minimal_pe(["Qt6Core.dll"]))
        # 把 SizeOfOptionalHeader 改大，超出文件末尾
        struct.pack_into("<H", pe, 0x40 + 4 + 16, 0xFFFF)
        path = tmp_path / "truncated.dll"
        path.write_bytes(bytes(pe))
        assert _parse_pe_imports(path) is None

    def test_returns_none_when_data_directory_truncated(self, tmp_path: Path) -> None:
        """DataDirectory 区域被截断返回 None."""
        pe = bytearray(_make_minimal_pe(["Qt6Core.dll"]))
        # 截断文件到 dd_offset 之前（dd_offset = opt_offset + 96 = 88 + 96 = 184）
        path = tmp_path / "truncated_dd.dll"
        path.write_bytes(bytes(pe[:180]))
        assert _parse_pe_imports(path) is None

    def test_returns_none_when_section_header_truncated(self, tmp_path: Path) -> None:
        """Section header 区域被截断返回 None."""
        pe = bytearray(_make_minimal_pe(["Qt6Core.dll"]))
        # 截断到 sections_offset 之后但不足一个 section header
        # sections_offset = 0x138 = 312
        path = tmp_path / "truncated_sec.dll"
        path.write_bytes(bytes(pe[:330]))
        assert _parse_pe_imports(path) is None

    def test_returns_none_when_import_rva_unresolvable(self, tmp_path: Path) -> None:
        """Import Table RVA 无法映射到文件偏移返回 None."""
        pe = bytearray(_make_minimal_pe(["Qt6Core.dll"]))
        # 把 Import Table RVA 改为不在任何 section 内的值
        # DataDirectory[1] RVA 在文件偏移 192
        struct.pack_into("<I", pe, 192, 0xDEAD_BEEF)
        path = tmp_path / "bad_import_rva.dll"
        path.write_bytes(bytes(pe))
        assert _parse_pe_imports(path) is None

    def test_returns_none_when_name_rva_unresolvable(self, tmp_path: Path) -> None:
        """descriptor Name RVA 无法映射时跳过该依赖，返回已解析部分."""
        pe = bytearray(_make_minimal_pe(["good.dll", "bad.dll"]))
        # 第二个 descriptor 的 Name 字段（offset 12）改为无效值
        # descriptors 起始 @ 0x200，每个 20 字节
        # 第二个 descriptor 的 name_rva @ 0x200 + 20 + 12 = 0x220
        struct.pack_into("<I", pe, 0x220, 0xDEAD_BEEF)
        path = tmp_path / "bad_name_rva.dll"
        path.write_bytes(bytes(pe))
        deps = _parse_pe_imports(path)
        # good.dll 正常解析，bad.dll 因 RVA 无效被跳过
        assert deps == ["good.dll"]

    def test_pe32_plus_magic_parsed(self, tmp_path: Path) -> None:
        """PE32+（0x20b）magic 正确解析（DataDirectory 偏移 112）."""
        pe = bytearray(_make_minimal_pe(["Qt6Core.dll"]))
        # 改 Optional header magic 为 PE32+ (0x20b)
        struct.pack_into("<H", pe, 88, 0x20B)
        # PE32+ 的 DataDirectory 起始偏移是 112（而非 96）
        # 把原 DataDirectory[1]（@ 192=88+96+8）的数据移到 88+112+8=208
        old_dd1 = bytes(pe[192:200])
        # 清空旧位置，写入新位置
        pe[192:200] = b"\x00" * 8
        pe[208:216] = old_dd1
        path = tmp_path / "pe32plus.dll"
        path.write_bytes(bytes(pe))
        deps = _parse_pe_imports(path)
        assert deps == ["Qt6Core.dll"]

    def test_empty_name_skipped(self, tmp_path: Path) -> None:
        """Name RVA 解析出空字符串时跳过."""
        pe = bytearray(_make_minimal_pe(["good.dll"]))
        # 把 good.dll 名称字符串的第一个字节改为 NUL，使 _read_ascii_string 返回空
        # names_data 起始：descriptors 总大小 = (1+1)*20 = 40，@ 0x200 + 40 = 0x228
        pe[0x228] = 0x00
        path = tmp_path / "empty_name.dll"
        path.write_bytes(bytes(pe))
        deps = _parse_pe_imports(path)
        assert deps == []

    def test_real_pe_name_field_offset_regression(self, tmp_path: Path) -> None:
        """真实 PE 端到端回归：DLL 名取自 descriptor Name 字段（第 4 字段，offset 12）.

        在测试内用 struct 构造最小合法 PE（DOS 头 + PE 头 + 1 个 section + 导入表
        含 2 个 DLL 名），descriptor 的 OriginalFirstThunk/FirstThunk 填非零 thunk
        RVA（指向无字符串区域）。若解析器误取第 1 字段（OriginalFirstThunk），
        会把 thunk RVA 当作 DLL 名偏移解析出乱码/空串，本测试即失败——覆盖
        「依赖解析乱码 → strip_unused_binaries 删光 dist DLL」回归点。
        """
        num_sections = 1
        opt_header_size = 224
        section_data_offset = 0x200
        section_virtual_addr = 0x1000

        # DOS header：e_lfanew = 0x40
        dos_header = bytearray(64)
        struct.pack_into("<I", dos_header, 0x3C, 0x40)

        # PE signature + COFF header（Machine=I386，1 section，OptionalHeader=224）
        coff_header = bytearray(20)
        struct.pack_into("<H", coff_header, 0, 0x14C)
        struct.pack_into("<H", coff_header, 2, num_sections)
        struct.pack_into("<H", coff_header, 16, opt_header_size)

        # Optional header PE32：DataDirectory[1]（Import Table）RVA 指向 section 起始
        opt_header = bytearray(opt_header_size)
        struct.pack_into("<H", opt_header, 0, 0x10B)
        struct.pack_into("<I", opt_header, 96 + 8, section_virtual_addr)

        # Section header：.rdata VA=0x1000，PointerToRawData=0x200
        section_header = bytearray(40)
        section_header[0:8] = b".rdata\x00\x00"
        struct.pack_into("<I", section_header, 12, section_virtual_addr)
        struct.pack_into("<I", section_header, 16, 0x200)
        struct.pack_into("<I", section_header, 20, section_data_offset)

        # 导入表 section data：
        # - 2 个 descriptor + 1 个全零终止符（3 * 20 = 60 字节，RVA 0x1000 起）
        # - 之后是 DLL 名字符串（RVA 0x103C 起）
        # OriginalFirstThunk/FirstThunk 填 thunk 区域 RVA（0x1030，无字符串），
        # Name 字段（第 4 字段）分别指向两个 DLL 名
        dll_names = [b"USER32.dll\x00", b"Qt6Widgets.dll\x00"]
        name_rva_base = section_virtual_addr + 60
        thunk_rva = section_virtual_addr + 20  # 指向 descriptors 区内（非字符串区域）
        # 布局：OriginalFirstThunk / TimeDateStamp / ForwarderChain / Name / FirstThunk
        descriptors = bytearray()
        descriptors.extend(struct.pack("<IIIII", thunk_rva, 0, 0, name_rva_base, thunk_rva))
        descriptors.extend(struct.pack("<IIIII", thunk_rva, 0, 0, name_rva_base + len(dll_names[0]), thunk_rva))
        descriptors.extend(b"\x00" * 20)  # 终止 descriptor
        section_data = bytes(descriptors) + b"".join(dll_names)

        pe = bytes(dos_header) + b"PE\x00\x00" + bytes(coff_header) + bytes(opt_header) + bytes(section_header)
        pe += b"\x00" * (section_data_offset - len(pe))
        pe += section_data

        path = tmp_path / "real_layout.dll"
        path.write_bytes(pe)
        deps = _parse_pe_imports(path)
        assert deps == ["USER32.dll", "Qt6Widgets.dll"]

    @pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 存在 kernel32.dll")
    def test_parses_real_kernel32_dll(self) -> None:
        """真实系统 kernel32.dll 实测：结果含 ntdll.dll 且全部条目为可打印 ASCII."""
        dll_path = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "kernel32.dll"
        deps = _parse_pe_imports(dll_path)
        assert deps is not None
        assert "ntdll.dll" in [d.lower() for d in deps]
        assert all(d and all(32 <= ord(c) < 127 for c in d) for d in deps)


class TestReadAsciiString:
    """``_read_ascii_string`` NUL 结尾字符串读取."""

    def test_reads_until_null(self) -> None:
        """读取到 NUL 结尾."""
        assert _read_ascii_string(b"hello\x00world", 0) == "hello"

    def test_returns_empty_when_no_null(self) -> None:
        """无 NUL 时返回空字符串."""
        assert _read_ascii_string(b"hello", 0) == ""


# ---------- _parse_objdump_deps / _parse_otool_deps ----------


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> _SubprocessResult:
    """构造 subprocess.run 成功返回值桩."""
    return _SubprocessResult(stdout=stdout, stderr=stderr, returncode=returncode)


class TestParseObjdumpDeps:
    """objdump 输出解析（Linux ELF）."""

    def test_parses_needed_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """NEEDED 行正确解析为依赖名列表."""
        stdout = "\n".join(
            [
                "test.so:     file format elf64-x86-64",
                "",
                "Dynamic Section:",
                "  NEEDED libfoo.so.6",
                "  NEEDED libbar.so.1",
                "  INIT 0x1000",
            ]
        )
        monkeypatch.setattr(
            "fspack.packaging.dep_analyzer.subprocess.run",
            lambda *a, **k: _make_completed(stdout=stdout),
        )
        deps = _parse_objdump_deps(tmp_path / "test.so")
        assert deps == ["libfoo.so.6", "libbar.so.1"]

    def test_returns_empty_when_no_needed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """无 NEEDED 行返回空列表."""
        monkeypatch.setattr(
            "fspack.packaging.dep_analyzer.subprocess.run",
            lambda *a, **k: _make_completed(stdout="Dynamic Section:\n  INIT 0x1000"),
        )
        assert _parse_objdump_deps(tmp_path / "test.so") == []

    def test_returns_none_when_objdump_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """objdump 不存在（FileNotFoundError）返回 None."""

        def raise_fnf(*a: Any, **k: Any) -> Any:
            raise FileNotFoundError("objdump")

        monkeypatch.setattr("fspack.packaging.dep_analyzer.subprocess.run", raise_fnf)
        assert _parse_objdump_deps(tmp_path / "test.so") is None

    def test_returns_none_when_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """objdump 超时返回 None."""

        def raise_timeout(*a: Any, **k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="objdump", timeout=10)

        monkeypatch.setattr("fspack.packaging.dep_analyzer.subprocess.run", raise_timeout)
        assert _parse_objdump_deps(tmp_path / "test.so") is None

    def test_returns_none_when_nonzero_returncode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """objdump 返回非 0（解析失败）返回 None."""
        monkeypatch.setattr(
            "fspack.packaging.dep_analyzer.subprocess.run",
            lambda *a, **k: _make_completed(stdout="", returncode=1),
        )
        assert _parse_objdump_deps(tmp_path / "test.so") is None


class TestParseOtoolDeps:
    """otool 输出解析（macOS Mach-O）."""

    def test_parses_dylib_lines(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """otool -L 输出解析为依赖路径列表（跳过首行文件自身）."""
        stdout = "\n".join(
            [
                "/path/to/foo.dylib:",
                "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1311.0.0)",
                "\t@rpath/libfoo.dylib (compatibility version 1.0.0, current version 1.0.0)",
                "\t@loader_path/libbar.dylib (compatibility version 1.0.0)",
            ]
        )
        monkeypatch.setattr(
            "fspack.packaging.dep_analyzer.subprocess.run",
            lambda *a, **k: _make_completed(stdout=stdout),
        )
        deps = _parse_otool_deps(tmp_path / "foo.dylib")
        assert deps == [
            "/usr/lib/libSystem.B.dylib",
            "@rpath/libfoo.dylib",
            "@loader_path/libbar.dylib",
        ]

    def test_returns_empty_when_no_deps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """仅有自身行（无依赖）返回空列表."""
        monkeypatch.setattr(
            "fspack.packaging.dep_analyzer.subprocess.run",
            lambda *a, **k: _make_completed(stdout="/path/to/foo.dylib:"),
        )
        assert _parse_otool_deps(tmp_path / "foo.dylib") == []

    def test_skips_blank_lines(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """otool 输出中的空行被跳过."""
        stdout = "\n".join(
            [
                "/path/to/foo.dylib:",
                "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)",
                "",
                "\t@rpath/libfoo.dylib (compatibility version 1.0.0)",
                "   ",
            ]
        )
        monkeypatch.setattr(
            "fspack.packaging.dep_analyzer.subprocess.run",
            lambda *a, **k: _make_completed(stdout=stdout),
        )
        deps = _parse_otool_deps(tmp_path / "foo.dylib")
        assert deps == ["/usr/lib/libSystem.B.dylib", "@rpath/libfoo.dylib"]

    def test_returns_none_when_otool_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """otool 不存在返回 None."""

        def raise_fnf(*a: Any, **k: Any) -> Any:
            raise FileNotFoundError("otool")

        monkeypatch.setattr("fspack.packaging.dep_analyzer.subprocess.run", raise_fnf)
        assert _parse_otool_deps(tmp_path / "foo.dylib") is None

    def test_returns_none_when_nonzero_returncode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """otool 返回非 0 返回 None."""
        monkeypatch.setattr(
            "fspack.packaging.dep_analyzer.subprocess.run",
            lambda *a, **k: _make_completed(returncode=1),
        )
        assert _parse_otool_deps(tmp_path / "foo.dylib") is None


# ---------- 辅助函数 ----------


class TestDepBasename:
    """``_dep_basename`` 各平台 basename 提取."""

    def test_windows_returns_as_is(self) -> None:
        """Windows PE 依赖名已是 basename，原样返回."""
        assert _dep_basename("Qt6Core.dll", Platform.WINDOWS) == "Qt6Core.dll"

    def test_linux_takes_basename(self) -> None:
        """Linux objdump NEEDED 后取 basename."""
        assert _dep_basename("libfoo.so.6", Platform.LINUX) == "libfoo.so.6"
        assert _dep_basename("/usr/lib/libfoo.so.6", Platform.LINUX) == "libfoo.so.6"

    def test_macos_takes_basename(self) -> None:
        """macOS otool 路径取 basename."""
        assert _dep_basename("@rpath/libfoo.dylib", Platform.MACOS) == "libfoo.dylib"
        assert _dep_basename("/usr/lib/libSystem.B.dylib", Platform.MACOS) == "libSystem.B.dylib"

    def test_empty_returns_empty(self) -> None:
        """空字符串返回空."""
        assert _dep_basename("", Platform.WINDOWS) == ""


class TestIsSystemDep:
    """``_is_system_dep`` 系统库识别."""

    def test_windows_system_dlls(self) -> None:
        """Windows 系统 DLL 前缀识别."""
        assert _is_system_dep("KERNEL32.dll", Platform.WINDOWS)
        assert _is_system_dep("api-ms-win-core-l1-1-0.dll", Platform.WINDOWS)
        assert _is_system_dep("USER32.dll", Platform.WINDOWS)
        assert _is_system_dep("msvcrt.dll", Platform.WINDOWS)

    def test_windows_non_system(self) -> None:
        """非系统 DLL 不识别为系统库."""
        assert not _is_system_dep("Qt6Core.dll", Platform.WINDOWS)
        assert not _is_system_dep("python311.dll", Platform.WINDOWS)

    def test_linux_system_paths(self) -> None:
        """Linux 系统路径前缀识别."""
        assert _is_system_dep("/usr/lib/libfoo.so", Platform.LINUX)
        assert _is_system_dep("/lib/libc.so.6", Platform.LINUX)
        assert not _is_system_dep("libfoo.so.6", Platform.LINUX)

    def test_macos_system_paths(self) -> None:
        """macOS 系统路径前缀识别."""
        assert _is_system_dep("/System/Library/Frameworks/Python.framework/Python", Platform.MACOS)
        assert _is_system_dep("/usr/lib/libSystem.B.dylib", Platform.MACOS)
        assert not _is_system_dep("@rpath/libfoo.dylib", Platform.MACOS)

    def test_empty_returns_true(self) -> None:
        """空依赖视为系统库（不参与剥离判定）."""
        assert _is_system_dep("", Platform.WINDOWS)


class TestDetectPlatformFromPath:
    """``_detect_platform_from_path`` 按扩展名推断平台."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("foo.dll", Platform.WINDOWS),
            ("foo.pyc", Platform.LINUX),  # 未知扩展名默认 Linux
            ("foo.pyd", Platform.WINDOWS),
            ("foo.exe", Platform.WINDOWS),
            ("foo.so", Platform.LINUX),
            ("foo.dylib", Platform.MACOS),
        ],
    )
    def test_detects_by_extension(self, filename: str, expected: Platform) -> None:
        assert _detect_platform_from_path(Path(filename)) is expected


class TestIterBinaryFiles:
    """``_iter_binary_files`` 递归扫描."""

    def test_scans_by_extension(self, tmp_path: Path) -> None:
        """按扩展名递归扫描，返回排序后路径."""
        (tmp_path / "a.dll").write_bytes(b"")
        (tmp_path / "b.txt").write_bytes(b"")  # 非二进制
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.dll").write_bytes(b"")
        (tmp_path / "sub" / "d.pyc").write_bytes(b"")  # 不在扩展名集合

        exts = frozenset({".dll"})
        result = _iter_binary_files(tmp_path, exts)
        names = [p.name for p in result]
        assert names == ["a.dll", "c.dll"]

    def test_returns_empty_for_no_matches(self, tmp_path: Path) -> None:
        """无匹配文件返回空列表."""
        (tmp_path / "a.txt").write_bytes(b"")
        assert _iter_binary_files(tmp_path, frozenset({".dll"})) == []

    def test_excludes_release_and_build_dirs(self, tmp_path: Path) -> None:
        """dist 根下 release/build 子树被排除（用户安装包/中间产物不参与剥离）."""
        (tmp_path / "a.dll").write_bytes(b"")
        (tmp_path / "release").mkdir()
        (tmp_path / "release" / "app-1.0-windows-slim.zip").write_bytes(b"")  # 用户安装包
        (tmp_path / "release" / "nested").mkdir()
        (tmp_path / "release" / "nested" / "deep.dll").write_bytes(b"")
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "intermediate.dll").write_bytes(b"")
        # 深层同名目录不排除（仅 dist 根下一级）
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "release").mkdir()
        (tmp_path / "src" / "release" / "inner.dll").write_bytes(b"")

        result = _iter_binary_files(tmp_path, frozenset({".dll", ".zip", ".exe"}))
        names = [str(p.relative_to(tmp_path)).replace(os.sep, "/") for p in result]
        assert names == ["a.dll", "src/release/inner.dll"]


# ---------- analyze_binary_dependencies ----------


def _patch_pe_parse(monkeypatch: pytest.MonkeyPatch, deps_map: dict[str, list[str] | None]) -> None:
    """patch ``_parse_pe_imports`` 按 basename 返回预设依赖."""

    def fake_parse(path: Path) -> list[str] | None:
        return deps_map.get(path.name)

    monkeypatch.setattr("fspack.packaging.dep_analyzer._parse_pe_imports", fake_parse)


class TestAnalyzeBinaryDependencies:
    """依赖图构建."""

    def test_windows_builds_graph(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows 平台构建依赖图，含入口识别与未解析收集."""
        # 布局：
        # dist/
        #   loader.exe        → 依赖 Qt6Core.dll
        #   Qt6Core.dll       → 依赖 Qt6Gui.dll, ICU.dll
        #   Qt6Gui.dll        → 依赖 KERNEL32.dll（系统库）
        #   ICU.dll           → 依赖（无）
        #   unused.dll        → 依赖（无）  ← 不可达
        #   runtime/python.exe → 依赖（无）
        (tmp_path / "loader.exe").write_bytes(b"")
        (tmp_path / "Qt6Core.dll").write_bytes(b"")
        (tmp_path / "Qt6Gui.dll").write_bytes(b"")
        (tmp_path / "ICU.dll").write_bytes(b"")
        (tmp_path / "unused.dll").write_bytes(b"")
        (tmp_path / "runtime").mkdir()
        (tmp_path / "runtime" / "python.exe").write_bytes(b"")

        _patch_pe_parse(
            monkeypatch,
            {
                "loader.exe": ["Qt6Core.dll"],
                "Qt6Core.dll": ["Qt6Gui.dll", "ICU.dll"],
                "Qt6Gui.dll": ["KERNEL32.dll"],
                "ICU.dll": [],
                "unused.dll": [],
                "python.exe": [],
            },
        )

        graph = analyze_binary_dependencies(tmp_path, Platform.WINDOWS)
        # 6 个二进制全部扫描
        assert len(graph.binaries) == 6
        # 入口：loader.exe + python.exe + 所有 .pyd（无）
        entry_names = {p.name for p in graph.entries}
        assert "loader.exe" in entry_names
        assert "python.exe" in entry_names
        # KERNEL32.dll 是系统库，不计入 unresolved
        assert "KERNEL32.dll" not in graph.unresolved

    def test_empty_dist_returns_empty_graph(self, tmp_path: Path) -> None:
        """空 dist 目录返回空图."""
        graph = analyze_binary_dependencies(tmp_path, Platform.WINDOWS)
        assert not graph.binaries
        assert not graph.entries

    def test_tool_missing_returns_empty_graph(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """解析工具缺失（_parse_dependencies 返回 None）跳过该文件，图可能为空."""
        (tmp_path / "test.dll").write_bytes(b"")
        # _parse_pe_imports 返回 None 模拟解析失败
        monkeypatch.setattr("fspack.packaging.dep_analyzer._parse_pe_imports", lambda p: None)
        graph = analyze_binary_dependencies(tmp_path, Platform.WINDOWS)
        assert not graph.binaries

    def test_unresolved_collects_missing_deps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """依赖图中声明但 dist 内未找到的依赖名收集到 unresolved."""
        (tmp_path / "loader.exe").write_bytes(b"")
        _patch_pe_parse(monkeypatch, {"loader.exe": ["Qt6Core.dll", "VCRUNTIME140.dll"]})

        graph = analyze_binary_dependencies(tmp_path, Platform.WINDOWS)
        # Qt6Core.dll 与 VCRUNTIME140.dll 都不在 dist 内，且非系统库
        assert "Qt6Core.dll" in graph.unresolved
        assert "VCRUNTIME140.dll" in graph.unresolved

    def test_runtime_dir_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """runtime_dir=None 时默认 dist_dir/runtime."""
        (tmp_path / "loader.exe").write_bytes(b"")
        (tmp_path / "runtime").mkdir()
        (tmp_path / "runtime" / "python.exe").write_bytes(b"")
        _patch_pe_parse(monkeypatch, {"loader.exe": [], "python.exe": []})

        graph = analyze_binary_dependencies(tmp_path, Platform.WINDOWS, runtime_dir=None)
        entry_names = {p.name for p in graph.entries}
        assert "python.exe" in entry_names


# ---------- find_unused_binaries ----------


class TestFindUnusedBinaries:
    """BFS 可达性分析."""

    def test_returns_unreachable(self, tmp_path: Path) -> None:
        """不可达的二进制（无入口引用）被识别."""
        loader = tmp_path / "loader.exe"
        used = tmp_path / "used.dll"
        unused = tmp_path / "unused.dll"

        graph = DepGraph()
        graph.binaries = {
            loader: BinaryInfo(path=loader, deps=("used.dll",)),
            used: BinaryInfo(path=used, deps=()),
            unused: BinaryInfo(path=unused, deps=()),
        }
        graph.entries = [loader]

        result = find_unused_binaries(graph)
        assert result == [unused]

    def test_transitive_dependency_reachable(self, tmp_path: Path) -> None:
        """传递依赖可达：loader → a → b，b 不可剥离."""
        loader = tmp_path / "loader.exe"
        a = tmp_path / "a.dll"
        b = tmp_path / "b.dll"

        graph = DepGraph()
        graph.binaries = {
            loader: BinaryInfo(path=loader, deps=("a.dll",)),
            a: BinaryInfo(path=a, deps=("b.dll",)),
            b: BinaryInfo(path=b, deps=()),
        }
        graph.entries = [loader]

        assert find_unused_binaries(graph) == []

    def test_empty_graph_returns_empty(self) -> None:
        """空图返回空列表."""
        graph = DepGraph()
        assert find_unused_binaries(graph) == []

    def test_no_entries_returns_empty(self, tmp_path: Path) -> None:
        """无入口返回空列表（保守不剥离）."""
        path = tmp_path / "a.dll"
        graph = DepGraph()
        graph.binaries = {path: BinaryInfo(path=path, deps=())}
        graph.entries = []
        assert find_unused_binaries(graph) == []

    def test_cycle_does_not_loop(self, tmp_path: Path) -> None:
        """依赖循环（a → b → a）不导致无限循环."""
        a = tmp_path / "a.dll"
        b = tmp_path / "b.dll"
        graph = DepGraph()
        graph.binaries = {
            a: BinaryInfo(path=a, deps=("b.dll",)),
            b: BinaryInfo(path=b, deps=("a.dll",)),
        }
        graph.entries = [a]
        # 两个都可达，无可剥离
        assert find_unused_binaries(graph) == []

    def test_entry_not_in_binaries_skipped(self, tmp_path: Path) -> None:
        """入口不在 binaries 中（已被剥离）不报错."""
        loader = tmp_path / "loader.exe"
        used = tmp_path / "used.dll"
        graph = DepGraph()
        graph.binaries = {used: BinaryInfo(path=used, deps=())}
        graph.entries = [loader]  # loader 不在 binaries
        # 无入口可达，但 used 本身不可达应被剥离
        result = find_unused_binaries(graph)
        assert used in result

    def test_case_insensitive_matching(self, tmp_path: Path) -> None:
        """依赖名大小写不敏感匹配（Windows DLL 命名差异）."""
        loader = tmp_path / "loader.exe"
        # 文件名是 Qt6Core.dll，依赖名是 qt6core.dll
        qt6core = tmp_path / "Qt6Core.dll"

        graph = DepGraph()
        graph.binaries = {
            loader: BinaryInfo(path=loader, deps=("qt6core.dll",)),  # 小写
            qt6core: BinaryInfo(path=qt6core, deps=()),
        }
        graph.entries = [loader]

        # qt6core.dll 应匹配 Qt6Core.dll，可达不剥离
        assert find_unused_binaries(graph) == []

    def test_duplicate_entry_not_revisited(self, tmp_path: Path) -> None:
        """入口重复出现在 entries 列表时不重复访问."""
        loader = tmp_path / "loader.exe"
        used = tmp_path / "used.dll"
        graph = DepGraph()
        graph.binaries = {
            loader: BinaryInfo(path=loader, deps=("used.dll",)),
            used: BinaryInfo(path=used, deps=()),
        }
        graph.entries = [loader, loader]  # 重复入口
        assert find_unused_binaries(graph) == []

    def test_empty_dep_basename_skipped(self, tmp_path: Path) -> None:
        """空依赖名（_dep_basename 返回 ''）跳过不匹配."""
        loader = tmp_path / "loader.exe"
        graph = DepGraph()
        graph.binaries = {loader: BinaryInfo(path=loader, deps=("",))}
        graph.entries = [loader]
        # 无有效依赖，仅 loader 可达，无剥离
        assert find_unused_binaries(graph) == []


class TestBinaryInfoNameLower:
    """``BinaryInfo.name_lower`` 属性."""

    def test_returns_lowercase_name(self, tmp_path: Path) -> None:
        """返回小写文件名."""
        info = BinaryInfo(path=tmp_path / "Qt6Core.DLL", deps=())
        assert info.name_lower == "qt6core.dll"


# ---------- strip_unused_binaries ----------


class TestStripUnusedBinaries:
    """未引用二进制剥离."""

    def test_deletes_files_and_returns_saved_bytes(self, tmp_path: Path) -> None:
        """删除文件并返回累计字节数."""
        a = tmp_path / "a.dll"
        b = tmp_path / "b.dll"
        a.write_bytes(b"AAAA")  # 4 bytes
        b.write_bytes(b"BBBBBB")  # 6 bytes

        saved = strip_unused_binaries([a, b])
        assert saved == 10
        assert not a.exists()
        assert not b.exists()

    def test_empty_list_returns_zero(self) -> None:
        """空列表返回 0."""
        assert strip_unused_binaries([]) == 0

    def test_missing_file_logged_not_raised(self, tmp_path: Path) -> None:
        """文件已不存在（OSError）记录 warning 不抛异常，跳过累加."""
        missing = tmp_path / "nonexistent.dll"
        # 不创建文件，直接传入
        saved = strip_unused_binaries([missing])
        assert saved == 0


# ---------- _identify_entries / _collect_loader_entries ----------


class TestIdentifyEntries:
    """入口识别."""

    def test_windows_loader_exe_and_pyd(self, tmp_path: Path) -> None:
        """Windows 入口：dist 根 loader.exe + runtime python.exe + .pyd."""
        loader = tmp_path / "loader.exe"
        pyd = tmp_path / "module.pyd"
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        python_exe = runtime / "python.exe"
        # 创建实际文件（_collect_loader_entries 用 glob 扫描文件系统）
        for p in (loader, pyd, python_exe):
            p.write_bytes(b"\x00")

        binaries = {
            loader: BinaryInfo(path=loader, deps=()),
            pyd: BinaryInfo(path=pyd, deps=()),
            python_exe: BinaryInfo(path=python_exe, deps=()),
        }
        entries = _identify_entries(tmp_path, runtime, Platform.WINDOWS, binaries)
        entry_set = set(entries)
        assert loader in entry_set
        assert pyd in entry_set
        assert python_exe in entry_set

    def test_linux_loader_no_suffix_and_so(self, tmp_path: Path) -> None:
        """Linux 入口：dist 根无后缀 loader + runtime python/bin/python3.X + .so."""
        loader = tmp_path / "myapp"  # 无后缀
        so = tmp_path / "module.so"
        runtime = tmp_path / "runtime"
        python_bin = runtime / "python" / "bin"
        python_bin.mkdir(parents=True)
        python_bin_exe = python_bin / "python3.11"
        # 创建实际文件（_collect_loader_entries 用 iterdir/glob 扫描文件系统）
        for p in (loader, so, python_bin_exe):
            p.write_bytes(b"\x00")

        binaries = {
            loader: BinaryInfo(path=loader, deps=()),
            so: BinaryInfo(path=so, deps=()),
            python_bin_exe: BinaryInfo(path=python_bin_exe, deps=()),
        }
        entries = _identify_entries(tmp_path, runtime, Platform.LINUX, binaries)
        entry_set = set(entries)
        assert loader in entry_set
        assert so in entry_set
        assert python_bin_exe in entry_set

    def test_skips_non_binary_loader(self, tmp_path: Path) -> None:
        """dist 根的 exe 文件不在 binaries 中不作为入口."""
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        # loader.exe 存在但不在 binaries 字典中
        (tmp_path / "loader.exe").write_bytes(b"")

        binaries: dict[Path, BinaryInfo] = {}
        entries = _collect_loader_entries(tmp_path, runtime, Platform.WINDOWS, binaries)
        assert not entries

    def test_macos_loader_no_suffix_and_dylib(self, tmp_path: Path) -> None:
        """macOS 入口：dist 根无后缀 loader + runtime python/bin/python3.X + .so.

        注意：``.dylib`` 在 ``_BINARY_EXTS`` 但不在 ``_ENTRY_EXTS``（仅 ``.pyd``/``.so``），
        故 ``.dylib`` 不作为入口（仅被依赖图引用）。
        """
        loader = tmp_path / "myapp"  # 无后缀
        so = tmp_path / "module.so"
        runtime = tmp_path / "runtime"
        python_bin = runtime / "python" / "bin"
        python_bin.mkdir(parents=True)
        python_bin_exe = python_bin / "python3.11"
        for p in (loader, so, python_bin_exe):
            p.write_bytes(b"\x00")

        binaries = {
            loader: BinaryInfo(path=loader, deps=()),
            so: BinaryInfo(path=so, deps=()),
            python_bin_exe: BinaryInfo(path=python_bin_exe, deps=()),
        }
        entries = _identify_entries(tmp_path, runtime, Platform.MACOS, binaries)
        entry_set = set(entries)
        assert loader in entry_set
        assert so in entry_set  # .so 在 _ENTRY_EXTS
        assert python_bin_exe in entry_set

    def test_runtime_python_bin_not_dir_skipped(self, tmp_path: Path) -> None:
        """runtime/python/bin 不是目录时不报错（Linux/macOS 分支）."""
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        # 不创建 python/bin 目录
        binaries: dict[Path, BinaryInfo] = {}
        entries = _collect_loader_entries(tmp_path, runtime, Platform.LINUX, binaries)
        assert not entries


class TestParseDependenciesDispatch:
    """``_parse_dependencies`` 平台分发."""

    def test_windows_calls_pe_parser(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows 目标调用 _parse_pe_imports."""
        called = False

        def fake_pe(path: Path) -> list[str] | None:
            nonlocal called
            called = True
            return []

        monkeypatch.setattr("fspack.packaging.dep_analyzer._parse_pe_imports", fake_pe)
        from fspack.packaging.dep_analyzer import _parse_dependencies

        _parse_dependencies(tmp_path / "test.dll", Platform.WINDOWS)
        assert called

    def test_macos_calls_otool(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS 目标调用 _parse_otool_deps."""
        monkeypatch.setattr(
            "fspack.packaging.dep_analyzer.subprocess.run",
            lambda *a, **k: _make_completed(stdout="/path/to/foo.dylib:"),
        )
        from fspack.packaging.dep_analyzer import _parse_dependencies

        result = _parse_dependencies(tmp_path / "foo.dylib", Platform.MACOS)
        assert result == []

    def test_linux_calls_objdump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Linux 目标调用 _parse_objdump_deps."""
        monkeypatch.setattr(
            "fspack.packaging.dep_analyzer.subprocess.run",
            lambda *a, **k: _make_completed(stdout="Dynamic Section:\n"),
        )
        from fspack.packaging.dep_analyzer import _parse_dependencies

        result = _parse_dependencies(tmp_path / "foo.so", Platform.LINUX)
        assert result == []


# ---------- _analyze_binary_dependencies 阶段函数集成 ----------


def _make_ctx(
    tmp_path: Path,
    dist_dir: Path,
    target: Platform,
    analyze_deps: bool = True,
) -> BuildContext:
    """构造最小 BuildContext 用于阶段函数测试."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')

    info = ProjectInfo(
        name="p",
        version="0",
        src_dir=project_dir,
        entry_module="p",
        entry_file=project_dir / "p.py",
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.11.9",
    )
    cfg = BuildConfig(
        project_dir=project_dir,
        dist_dir=dist_dir,
        embed_cache_dir=tmp_path / "embed",
        mirror=MirrorConfig(name="t", python_base="https://x/py", pypi_index="https://x/s"),
        target=target,
    )
    opts = BuildOptions(analyze_deps=analyze_deps)
    return BuildContext(
        tracker=BuildTracker(),
        info=info,
        cfg=cfg,
        opts=opts,
        runtime_dir=dist_dir / "runtime",
    )


class TestAnalyzeBinaryDependenciesStage:
    """``_analyze_binary_dependencies`` 阶段函数集成."""

    def test_strips_unused_and_records_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """剥离未引用二进制，节省字节数写入 tracker."""
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "runtime").mkdir()

        loader = dist / "loader.exe"
        used = dist / "used.dll"
        unused = dist / "unused.dll"
        loader.write_bytes(b"X" * 10)
        used.write_bytes(b"Y" * 20)
        unused.write_bytes(b"Z" * 30)

        _patch_pe_parse(
            monkeypatch,
            {
                "loader.exe": ["used.dll"],
                "used.dll": [],
                "unused.dll": [],
                "python.exe": [],
            },
        )

        ctx = _make_ctx(tmp_path, dist, Platform.WINDOWS)
        saved = _analyze_binary_dependencies(ctx)

        assert saved == 30
        assert not unused.exists()
        assert loader.exists()
        assert used.exists()

    def test_no_binaries_returns_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """无二进制文件返回 0，stage 标记跳过."""
        dist = tmp_path / "dist"
        dist.mkdir()
        # 无任何 .dll/.pyd/.exe 文件
        monkeypatch.setattr("fspack.packaging.dep_analyzer._parse_pe_imports", lambda p: [])
        ctx = _make_ctx(tmp_path, dist, Platform.WINDOWS)
        assert _analyze_binary_dependencies(ctx) == 0

    def test_all_reachable_returns_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """全部二进制可达返回 0，不删除任何文件."""
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "runtime").mkdir()

        loader = dist / "loader.exe"
        used = dist / "used.dll"
        loader.write_bytes(b"X")
        used.write_bytes(b"Y")

        _patch_pe_parse(
            monkeypatch,
            {"loader.exe": ["used.dll"], "used.dll": [], "python.exe": []},
        )
        ctx = _make_ctx(tmp_path, dist, Platform.WINDOWS)
        assert _analyze_binary_dependencies(ctx) == 0
        assert loader.exists()
        assert used.exists()


# ---------- 跨平台综合场景 ----------


class TestEndToEndWindows:
    """Windows 端到端：PE 解析 + 图构建 + BFS + 剥离."""

    def test_full_pipeline_strips_orphan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """完整流程：构造含孤立项的 dist，分析后剥离无引用 DLL."""
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "runtime").mkdir()

        # 用真实 PE 文件（构造最小 PE，含导入表）
        # loader.dll 依赖 used.dll（loader.dll 作为 .pyd 入口）
        # orphan.dll 无引用
        loader_pe = _make_minimal_pe(["used.dll"])
        used_pe = _make_minimal_pe([])
        orphan_pe = _make_minimal_pe([])

        (dist / "loader.pyd").write_bytes(loader_pe)
        (dist / "used.dll").write_bytes(used_pe)
        (dist / "orphan.dll").write_bytes(orphan_pe)

        graph = analyze_binary_dependencies(dist, Platform.WINDOWS, runtime_dir=dist / "runtime")
        unused = find_unused_binaries(graph)
        # orphan.dll 不可达
        assert any(p.name == "orphan.dll" for p in unused)

        saved = strip_unused_binaries(unused)
        # orphan.dll 被删除，saved > 0
        assert saved > 0
        assert not (dist / "orphan.dll").exists()
        assert (dist / "loader.pyd").exists()
        assert (dist / "used.dll").exists()
