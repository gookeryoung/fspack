"""PE 导入表解析（Windows，纯 Python，无 pefile 依赖）.

解析流程：DOS header → PE signature → COFF header → Optional header →
DataDirectory[1] Import Table → Section table（RVA→文件偏移映射）→
遍历 IMAGE_IMPORT_DESCRIPTOR 数组，读取每个 DLL 名称。
"""

from __future__ import annotations

import struct
from pathlib import Path


def _parse_pe_imports(path: Path) -> list[str] | None:  # noqa: PLR0911, PLR0912
    """纯 Python 解析 PE 文件导入表，返回依赖 DLL basename 列表.

    依赖 DLL basename（如 ``Qt6Core.dll``）；解析失败返回 None。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None

    if len(data) < 64:
        return None

    try:
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    except struct.error:  # pragma: no cover
        return None

    if pe_offset + 24 > len(data):
        return None

    if data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        return None

    try:
        num_sections = struct.unpack_from("<H", data, pe_offset + 6)[0]
        opt_header_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
    except struct.error:  # pragma: no cover
        return None

    opt_offset = pe_offset + 24
    if opt_offset + opt_header_size > len(data):
        return None

    try:
        magic = struct.unpack_from("<H", data, opt_offset)[0]
    except struct.error:  # pragma: no cover
        return None

    if magic == 0x10B:  # PE32
        dd_offset = opt_offset + 96
    elif magic == 0x20B:  # PE32+
        dd_offset = opt_offset + 112
    else:
        return None

    if dd_offset + 16 > len(data):
        return None

    try:
        import_rva = struct.unpack_from("<I", data, dd_offset + 8)[0]
    except struct.error:  # pragma: no cover
        return None

    if import_rva == 0:
        return []

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
        except struct.error:  # pragma: no cover
            return None
        sections.append((virtual_addr, size_of_raw, pointer_to_raw))

    def rva_to_offset(rva: int) -> int | None:
        for vaddr, sraw, praw in sections:
            if vaddr <= rva < vaddr + sraw:
                return rva - vaddr + praw
        return None

    import_offset = rva_to_offset(import_rva)
    if import_offset is None:
        return None

    deps: list[str] = []
    cursor = import_offset
    while cursor + 20 <= len(data):
        try:
            name_rva, _, _, _, _ = struct.unpack_from("<IIIII", data, cursor)
        except struct.error:  # pragma: no cover
            break

        if name_rva == 0:
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
    except (ValueError, IndexError):  # pragma: no cover — errors="ignore" 保证不抛
        return ""
