"""Win7 PE 导入表重定向：把 kernel32 的 Win8+ API 导入改写为 shim DLL.

背景
----
kernel32 / kernelbase 是 KnownDLL，无法用同名 DLL 遮蔽。但第三方 .pyd
和 wheel 扩展（如 pydantic-core）不是 KnownDLL，可以在打包期改写其 PE
导入表——把 kernel32.dll!GetSystemTimePreciseAsFileTime 等 Win8+ 函数
从 kernel32 的导入描述符中移走，改为从一个随包分发的 shim DLL（
``api-ms-win-core-kernel32-shim.dll``）导入。

本模块实现这一改写逻辑，配合 :mod:`fspack.packaging.win7.shim_build`
提供的 shim 产物使用。

技术路线
--------
PE 导入表（IMAGE_IMPORT_DESCRIPTOR 数组 + 各 DLL 的 INT/IAT + Hint/Name
表）全部位于 ``.idata`` section。改写流程：

1. ``pefile.PE`` 读取目标 PE，解析现有导入表。
2. 根据 :data:`REDIRECTED_IMPORTS` 映射表，找出需要重定向的函数。
3. 生成一份「原导入表 - 被重定向条目」+「新增 shim DLL 导入描述符」的
   完整导入表字节流。
4. 替换 .idata section 的内容，更新 ``DATA_DIRECTORY[1]``（Import Table）
   的 RVA 和 Size。
5. 写回文件（覆盖原文件或新文件）。

PE loader 的 IAT 指针本身（在 .idata 里）也需要重新定位到新 IAT 数组的
对应 slot——这由重建的 FirstThunk 值保证，运行时 loader 会填充新的 IAT。

CLI::

    python -m fspack.packaging.win7.patch <target.pyd> [--output <out.pyd>]

退出码：0 改写成功；1 目标无匹配函数（无需改写）；2 解析失败。
"""

from __future__ import annotations

import argparse
import logging
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, cast

import pefile

__all__ = [
    "REDIRECTED_IMPORTS",
    "SHIM_DLL_NAME",
    "PEPatchError",
    "ShimImport",
    "build_patched_pe",
    "main",
    "patch_file",
]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量：需要从 kernel32 重定向到 shim DLL 的 Win8+ API
# ---------------------------------------------------------------------------


# shim DLL 文件名（不带路径；打包时随产物分发到同目录）
SHIM_DLL_NAME = "api-ms-win-core-kernel32-shim.dll"


@dataclass(frozen=True)
class ShimImport:
    """一条需要从 kernel32/kernelbase 重定向到 shim DLL 的导入.

    Attributes:
        src_dll: 原 DLL 名（KERNEL32.dll / KERNELBASE.dll，大小写不敏感）。
        func_name: 函数名。
        reason: 重定向原因（用于日志/报告）。
    """

    src_dll: str
    func_name: str
    reason: str = ""


# 当前支持的重定向表；后续扩展（如 CopyFile2 / Pss*）只需追加条目。
REDIRECTED_IMPORTS: tuple[ShimImport, ...] = (
    ShimImport(
        src_dll="KERNEL32.dll",
        func_name="GetSystemTimePreciseAsFileTime",
        reason="Win8+ API，Python 3.13 / pydantic-core 导入",
    ),
    # 预留：后续 Win8+ kernel32 API 追加在此（需要 shim 源码同步扩展）
    # ShimImport("KERNEL32.dll", "CopyFile2", "Win8+ file copy with progress"),
    # ShimImport("KERNEL32.dll", "PssCaptureSnapshot", "Win8+ process snapshot"),
)

# (src_dll_lower, func_name) -> ShimImport 索引，O(1) 查询
_REDIRECT_INDEX: dict[tuple[str, str], ShimImport] = {
    (si.src_dll.lower(), si.func_name): si for si in REDIRECTED_IMPORTS
}


# 被重定向的函数对应的 shim DLL 统一导出名（当前 src_name == shim_name）
def _shim_export_name(_src_dll: str, func_name: str) -> str:
    """返回 shim DLL 中应导出的函数名；默认与原函数名相同."""
    return func_name


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class PEPatchError(RuntimeError):
    """PE 解析失败 / 导入表重建出错 / section 溢出等."""


# ---------------------------------------------------------------------------
# 数据结构：描述一个 PE 的导入表重建结果
# ---------------------------------------------------------------------------


class _ImportFunc(NamedTuple):
    """单个函数导入的轻量描述."""

    dll_name: str  # 原 DLL 名（大写规范化）
    func_name: str | None  # 函数名（None 表示 ordinal 导入）
    ordinal: int | None


@dataclass
class _BuildImport:
    """重建导入表时一个 DLL 描述符的工作对象."""

    dll_name: str
    funcs: list[_ImportFunc] = field(default_factory=list)  # 按原顺序

    @property
    def needs_shim(self) -> bool:
        return self.dll_name.lower() == SHIM_DLL_NAME.lower()


# ---------------------------------------------------------------------------
# 核心：重建 .idata
# ---------------------------------------------------------------------------


def _collect_imports(pe: pefile.PE) -> list[_ImportFunc]:
    """从 pefile 对象提取所有导入函数，保持原顺序."""
    results: list[_ImportFunc] = []
    _pe = cast(Any, pe)
    if not hasattr(_pe, "DIRECTORY_ENTRY_IMPORT"):
        return results
    for entry in _pe.DIRECTORY_ENTRY_IMPORT:
        dll = entry.dll.decode().upper()
        for imp in entry.imports:
            name = imp.name.decode() if imp.name else None
            results.append(_ImportFunc(dll_name=dll, func_name=name, ordinal=imp.ordinal))
    return results


def _find_idata_section(pe: pefile.PE) -> pefile.SectionStructure | None:
    """定位 .idata section（优先按名字匹配，回退按 Import Directory 地址找）."""
    _pe = cast(Any, pe)
    for sec in _pe.sections:
        if sec.Name.rstrip(b"\x00") == b".idata":
            return sec
    # 回退：按 Import Directory 的 RVA 定位所属 section
    oph = cast(Any, _pe.OPTIONAL_HEADER)
    assert oph is not None, "PE 缺少 OPTIONAL_HEADER"
    import_dir = oph.DATA_DIRECTORY[1]
    if import_dir.VirtualAddress == 0:
        return None
    for sec in pe.sections:
        start = sec.VirtualAddress
        end = start + max(sec.Misc_VirtualSize, sec.SizeOfRawData)
        if start <= import_dir.VirtualAddress < end:
            return sec
    return None


def _rebuild_idata(  # noqa: PLR0912 — PE .idata 重建需要多步分支处理，无法再拆
    pe: pefile.PE,
    imports: list[_ImportFunc],
    *,
    base_rva: int,
    shim_dll_name: str,
    shim_exports: list[str],
) -> bytes:
    """根据重建后的导入列表，生成完整的 .idata 二进制字节.

    布局（x64）：
      [ IMAGE_IMPORT_DESCRIPTOR 数组 | IMAGE_IMPORT_DESCRIPTOR 终止符 |
        所有 DLL 名字（\\0 结尾）|
        各 DLL 的 OriginalFirstThunk 数组（8 字节/条目）|
        各 DLL 的 FirstThunk（IAT）数组（8 字节/条目）|
        Hint/Name 表（每个函数 2 字节 hint + N 字节名 + \\0）]

    Args:
        pe: 原始 PE 对象（用于获取 Machine / Bits 判断 32/64）。
        imports: 重定向后的完整导入列表。
        shim_dll_name: shim DLL 名字。
        shim_exports: shim DLL 要导出的函数名（只包含被重定向过来的）。

    Returns:
        新的 .idata section 原始字节流。
    """
    # shim_dll_name / shim_exports 当前版本未在重建逻辑中直接引用（imports 列表已包含重定向结果），
    # 保留参数是为了未来扩展（如按 shim 导出做一致性校验），这里显式引用消除 lint 警告。
    _ = shim_dll_name, shim_exports

    _pe = cast(Any, pe)
    oph = cast(Any, _pe.OPTIONAL_HEADER)
    assert oph is not None, "PE 缺少 OPTIONAL_HEADER"
    is_64 = oph.Magic == 0x20B  # PE32+

    # 第一步：按 DLL 分组，去掉被完全清空的 DLL（原 kernel32 只剩 Win8+ 函数被重定向、
    # 所有 Win7 原生函数也被误处理？——不会，因为我们只把匹配到的函数移到 shim）。
    # 关键：原始 .idata 里一个 DLL 描述符的 INT/IAT 数组长度固定，我们不能「置 0 中间一项」，
    # 否则 loader 在首个 0 处截断。所以这里直接把被重定向的函数从原 DLL 的 funcs 列表
    # 里**物理删除**，让数组变短，重建 INT/IAT 时不会留 hole。

    # 但不能直接原地删——多个线程/迭代可能并发使用。先构建副本分组。
    grouped: dict[str, list[_ImportFunc]] = {}
    for imp in imports:
        grouped.setdefault(imp.dll_name, []).append(imp)

    # 第二步：为每组分配空间并生成字节。采用「预计算偏移 + 填充」的通用做法：
    #   (a) 遍历所有 DLL，收集名字/INT/IAT/HintName 的字节大小
    #   (b) 计算 IMAGE_IMPORT_DESCRIPTOR 数组总大小 = 20 * (N+1)（+1 是终止符）
    #   (c) 依次安排各字段的 RVA（相对于新 .idata 起始）
    #   (d) 填充字节
    #
    # 为简化，我们用一种更直接的方法：Python 列表累加每个 chunk 的偏移/字节，
    # 然后一次拼接。

    # 先处理每个 DLL 的名字、HintName 表内容（这些是不依赖 RVA 计算的「纯字节」）
    # 我们需要知道：每个 DLL 有多少函数、每个函数的 hint+name 字节
    chunk_name: dict[str, bytes] = {}  # DLL name → b"name\x00"
    chunk_hintname: dict[str, list[bytes]] = {}  # dll_name → [b"\\x00\\x00funcname\x00", ...]
    chunk_int: dict[str, list[int]] = {}  # dll_name → [original_first_thunk_entry_rva, ...]  (先占位，后面填)
    chunk_iat: dict[str, list[int]] = {}  # dll_name → [iat_entry_rva, ...]  (先占位，后面填)
    dll_order: list[str] = []  # 保持原 DLL 顺序，shim DLL 追加在最后

    for dll_name, funcs in grouped.items():
        dll_order.append(dll_name)
        chunk_name[dll_name] = dll_name.encode("ascii") + b"\x00"
        hintname_list: list[bytes] = []
        for f in funcs:
            if f.func_name is None:
                # ordinal 导入（63 位置 1 表示 ordinal，低 15 位是序号）
                hintname_list.append(b"\x00\x00")  # ordinal 函数没有 hint+name 表
            else:
                hintname_list.append(b"\x00\x00" + f.func_name.encode("ascii") + b"\x00")
        chunk_hintname[dll_name] = hintname_list
        # 每个 DLL 的 INT/IAT 数组末尾必须有 0 QWORD 终止符（Windows loader
        # 和 pefile 都靠这个终止符定位 thunk table 末端），所以多分配 1 个 slot。
        chunk_int[dll_name] = [0] * (len(funcs) + 1)
        chunk_iat[dll_name] = [0] * (len(funcs) + 1)

    # 第三步：计算各 chunk 相对于新 .idata 起点的偏移
    # 偏移 0: IMAGE_IMPORT_DESCRIPTOR 数组（20 * (N+1) 字节，N = len(dll_order)）
    descriptor_count = len(dll_order)
    descriptor_table_size = 20 * (descriptor_count + 1)  # +1 终止符

    current_offset = descriptor_table_size  # 跳过 Descriptor 数组

    # 先放 DLL names（按 dll_order 顺序）
    dll_name_offset: dict[str, int] = {}
    for dll_name in dll_order:
        dll_name_offset[dll_name] = current_offset
        current_offset += len(chunk_name[dll_name])
        # 4 字节对齐
        current_offset = (current_offset + 3) & ~3

    # 再放每个 DLL 的 INT 数组（x64 每个 8 字节，x86 每个 4 字节）
    int_entry_size = 8 if is_64 else 4
    dll_int_offset: dict[str, int] = {}
    dll_iat_offset: dict[str, int] = {}
    for dll_name in dll_order:
        dll_int_offset[dll_name] = current_offset
        current_offset += len(chunk_int[dll_name]) * int_entry_size
        # 4 字节对齐
        current_offset = (current_offset + 3) & ~3

    # 然后 IAT 数组（与 INT 等长等结构）
    for dll_name in dll_order:
        dll_iat_offset[dll_name] = current_offset
        current_offset += len(chunk_iat[dll_name]) * int_entry_size
        current_offset = (current_offset + 3) & ~3

    # 最后 Hint/Name 表
    # 每个函数的 Hint+Name 表条目是独立的，我们按 DLL 分组放置
    dll_hintname_offset: dict[str, list[int]] = {}  # dll_name → [offset 列表，与 funcs 对齐]
    for dll_name in dll_order:
        offsets: list[int] = []
        for hn in chunk_hintname[dll_name]:
            offsets.append(current_offset)
            current_offset += len(hn)
        dll_hintname_offset[dll_name] = offsets
    total_size = current_offset

    # 第四步：填充 INT 和 IAT 的实际 RVA（上面只算了 DLL 在 .idata 内的起始 offset，
    # 每个条目需要填充的是该条目对应的 hintname 偏移或 ordinal 标记）。
    # 末尾额外的 0 QWORD 作为 thunk table 终止符（offset 计算阶段已预分配 +1 slot）。
    for dll_name in dll_order:
        funcs = grouped[dll_name]
        int_list: list[int] = []
        iat_list: list[int] = []
        for idx, f in enumerate(funcs):
            iat_entry: int
            if f.func_name is None:
                # ordinal 导入：bit 63 (x64) 或 bit 31 (x86) 置 1
                ordinal = f.ordinal or 0
                if is_64:
                    iat_entry = 0x8000000000000000 | ordinal
                else:
                    iat_entry = 0x80000000 | ordinal
                int_list.append(iat_entry)
                iat_list.append(iat_entry)
            else:
                # name 导入：INT/IAT 条目是 Hint+Name 表的**绝对 RVA**
                # （不是相对于 .idata 起始的偏移），这里加 base_rva。
                hn_offset = dll_hintname_offset[dll_name][idx] + base_rva
                int_list.append(hn_offset)
                iat_list.append(hn_offset)
        # 追加 0 QWORD 终止符（offset 计算阶段已为每个 thunk table 预分配了这个 slot）
        int_list.append(0)
        iat_list.append(0)
        chunk_int[dll_name] = int_list
        chunk_iat[dll_name] = iat_list

    # 第五步：按偏移拼接最终字节
    buf = bytearray(total_size)

    # 5a. IMAGE_IMPORT_DESCRIPTOR 数组
    # 注意：IMAGE_IMPORT_DESCRIPTOR 不管 x86/x64 都是 20 字节 5 个 DWORD；
    # 区别只在 FirstThunk 指向的 IAT 数组条目大小（x86=4B, x64=8B）。
    # 每个 Descriptor 的 INT/Name/IAT 都是**绝对 RVA**（加 base_rva）。
    for idx, dll_name in enumerate(dll_order):
        off = idx * 20
        INT_off = dll_int_offset[dll_name] + base_rva
        IAT_off = dll_iat_offset[dll_name] + base_rva
        NAME_off = dll_name_offset[dll_name] + base_rva
        buf[off : off + 20] = struct.pack("<IIIII", INT_off, 0, 0, NAME_off, IAT_off)
    # 终止符（全 0）
    off = descriptor_count * 20
    buf[off : off + 20] = b"\x00" * 20

    # 5b. DLL 名字区
    for dll_name in dll_order:
        start = dll_name_offset[dll_name]
        name_bytes = chunk_name[dll_name]
        buf[start : start + len(name_bytes)] = name_bytes

    # 5c. INT 数组
    fmt = "<Q" if is_64 else "<I"
    for dll_name in dll_order:
        start = dll_int_offset[dll_name]
        vals = chunk_int[dll_name]
        for i, v in enumerate(vals):
            buf[start + i * int_entry_size : start + (i + 1) * int_entry_size] = struct.pack(fmt, v)

    # 5d. IAT 数组
    for dll_name in dll_order:
        start = dll_iat_offset[dll_name]
        vals = chunk_iat[dll_name]
        for i, v in enumerate(vals):
            buf[start + i * int_entry_size : start + (i + 1) * int_entry_size] = struct.pack(fmt, v)

    # 5e. Hint+Name 表
    for dll_name in dll_order:
        for idx, hn_bytes in enumerate(chunk_hintname[dll_name]):
            start = dll_hintname_offset[dll_name][idx]
            buf[start : start + len(hn_bytes)] = hn_bytes

    return bytes(buf)


# ---------------------------------------------------------------------------
# 公共 API：改写一个 PE 文件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatchResult:
    """一次 PE patch 的结果摘要，用于日志 / 报告."""

    target_path: Path
    redirected: list[tuple[str, str, str]]  # (src_dll, func_name, shim_dll)
    shim_dll_added: bool
    output_path: Path


# OptionalHeader 内部字段偏移（相对于 OptionalHeader 起始）
# PE32+ 和 PE32 共享 SectionAlignment / FileAlignment / SizeOfImage /
# DataDirectory 的 offset——区别在于 ImageBase 大小（8 vs 4 bytes）导致
# 后续字段偏移不同，但上面这些字段恰好在偏移相同的位置。
_OPT_SOI_OFF = 56  # SizeOfImage（PE32+ = 56, PE32 = 52，但 PE32 下 +4 会错）
_OPT_SOI_OFF_PE32 = 60  # 保险起见两个都硬编码
_OPT_SEC_ALIGN_OFF = 32  # SectionAlignment
_OPT_FILE_ALIGN_OFF = 36  # FileAlignment
_OPT_DIRS_BASE_PE32P = 112  # DataDirectory 起始（PE32+）
_OPT_DIRS_BASE_PE32 = 96  # DataDirectory 起始（PE32）

# Section header 内部字段偏移（相对于 section header 起始，每项 40 bytes）
_SH_MISC_VS = 8  # Misc_VirtualSize
_SH_VA = 12  # VirtualAddress
_SH_SIZE_RAW = 16  # SizeOfRawData


def _align(value: int, alignment: int) -> int:
    """向上对齐到 alignment 的整数倍（alignment 必须是 2 的幂）."""
    return (value + alignment - 1) & ~(alignment - 1)


def build_patched_pe(
    pe: pefile.PE,
    *,
    shim_dll_name: str = SHIM_DLL_NAME,
) -> PatchResult:
    """改写 pefile 对象（原地修改 ``pe.__data__`` bytearray），返回改写摘要.

    重要：pefile 的 ctypes 字段赋值（如 ``pe.OPTIONAL_HEADER.SizeOfImage = X``）
    **不会**回写到 ``pe.__data__`` bytearray，因此所有 PE 结构修改必须用
    ``struct.pack_into`` 直接操作 bytearray 字节。``pe.write()`` 内部重建
    数据也**不读** ``pe.__data__``，调用方必须在拿到结果后直接写 bytearray
    到文件。本函数内不负责写盘。

    Args:
        pe: 已加载的 PE 对象，会被原地修改其 ``__data__`` bytearray。
        shim_dll_name: 目标 shim DLL 名字。

    Returns:
        PatchResult 摘要。

    Raises:
        PEPatchError: .idata section 缺失 / 新 .idata 溢出当前 section / 解析失败。
    """
    _pe = cast(Any, pe)
    oph = cast(Any, _pe.OPTIONAL_HEADER)
    assert oph is not None, "PE 缺少 OPTIONAL_HEADER"
    is_64 = oph.Magic == 0x20B
    e_lfanew: int = pe.DOS_HEADER.e_lfanew  # pyrefly: ignore[missing-attribute]

    # ---- PE 头部各关键偏移一次性算好 ----
    opt_hdr_start = e_lfanew + 4 + 20  # PE sig (4) + COFF File Header (20)
    num_sections = struct.unpack_from("<H", _pe.__data__, e_lfanew + 4 + 2)[0]
    size_of_opt_hdr = struct.unpack_from("<H", _pe.__data__, e_lfanew + 4 + 16)[0]
    sec_table_off = opt_hdr_start + size_of_opt_hdr
    last_sec_hdr_off = sec_table_off + (num_sections - 1) * 40

    soi_field_off = _OPT_SOI_OFF if is_64 else _OPT_SOI_OFF_PE32
    soi_abs_off = opt_hdr_start + soi_field_off

    dirs_base = opt_hdr_start + (_OPT_DIRS_BASE_PE32P if is_64 else _OPT_DIRS_BASE_PE32)
    import_dir_rva_off = dirs_base + 1 * 8  # DataDirectory[1] = Import Directory
    import_dir_size_off = import_dir_rva_off + 4

    # 从 bytearray 里读 SectionAlignment / FileAlignment（两种架构偏移相同）
    data = _pe.__data__
    if not isinstance(data, bytearray):
        data = bytearray(data)
        _pe.__data__ = data
    sec_align = struct.unpack_from("<I", data, opt_hdr_start + _OPT_SEC_ALIGN_OFF)[0]
    file_align = struct.unpack_from("<I", data, opt_hdr_start + _OPT_FILE_ALIGN_OFF)[0]

    idata_sec = _find_idata_section(pe)
    if idata_sec is None:
        raise PEPatchError("未找到 .idata section，无法重建导入表")

    # 1. 提取现有导入
    all_imports = _collect_imports(pe)
    if not all_imports:
        raise PEPatchError("目标文件无导入，无需改写")

    # 2. 找出需要重定向的条目，从 all_imports 中**物理移除**（重建 INT/IAT
    #    时不会留下 hole），同时把重定向后的新条目追加到 shim DLL
    redirected: list[tuple[str, str, str]] = []
    remaining: list[_ImportFunc] = []
    for imp in all_imports:
        key = (imp.dll_name.lower(), imp.func_name or "")
        if imp.func_name is not None and key in _REDIRECT_INDEX:
            redirected.append((imp.dll_name, imp.func_name, shim_dll_name))
            remaining.append(
                _ImportFunc(
                    dll_name=shim_dll_name.upper(),
                    func_name=_shim_export_name(imp.dll_name, imp.func_name),
                    ordinal=None,
                )
            )
        else:
            remaining.append(imp)

    if not redirected:
        _logger.info("目标文件无匹配的 Win8+ kernel32 导入，无需改写")
        return PatchResult(
            target_path=Path("unknown"),
            redirected=[],
            shim_dll_added=False,
            output_path=Path("unknown"),
        )

    import_dir = oph.DATA_DIRECTORY[1]
    idata_rva = import_dir.VirtualAddress

    # 3. 判断重建后的 .idata 能否就地覆盖原位置
    idata_sec_any = cast(Any, idata_sec)
    idata_sec_va = idata_sec_any.VirtualAddress
    idata_sec_end_va = idata_sec_va + max(idata_sec_any.Misc_VirtualSize, idata_sec_any.SizeOfRawData)
    available_here = idata_sec_end_va - idata_rva

    new_idata_tmp = _rebuild_idata(
        pe,
        remaining,
        base_rva=idata_rva,
        shim_dll_name=shim_dll_name,
        shim_exports=[r[1] for r in redirected],
    )
    new_size = len(new_idata_tmp)

    if new_size <= available_here:
        # ---- 路径 A：就地覆盖（RVA 不变，只需更新 Size）----
        new_idata = new_idata_tmp
        raw_offset = idata_sec_any.PointerToRawData + (idata_rva - idata_sec_va)
        data[raw_offset : raw_offset + new_size] = new_idata
        # Import Directory Size 直接改 bytearray
        struct.pack_into("<I", data, import_dir_size_off, new_size)
        _logger.info(
            "PE patch [就地覆盖]: new .idata %d bytes @ RVA=0x%X (可用 %d)", new_size, idata_rva, available_here
        )
    else:
        # ---- 路径 B：追加到文件末尾 + 扩展最后一个 section ----
        # 关键：最后一个 section 的 VA 末端用 max(VSZ, RSZ) 算——VSZ 是内存
        # 中 section 的实际大小（可 < RSZ），RSZ 是文件中大小。扩展后的
        # new_idata_va 必须对齐到 SectionAlignment。
        last_sec = cast(Any, _pe.sections[-1])
        last_va: int = last_sec.VirtualAddress
        last_vsz: int = max(last_sec.Misc_VirtualSize, last_sec.SizeOfRawData)
        last_va_end: int = last_va + last_vsz
        new_idata_va = _align(last_va_end, sec_align)

        new_idata = _rebuild_idata(
            pe,
            remaining,
            base_rva=new_idata_va,
            shim_dll_name=shim_dll_name,
            shim_exports=[r[1] for r in redirected],
        )
        new_size = len(new_idata)

        last_rsz: int = last_sec.SizeOfRawData
        # new_idata_va 在 section 内部的 raw 偏移 = new_idata_va - last_va
        # 如果这个偏移 > last_rsz，说明在原 raw 区末端和新数据之间有 gap
        import_dir_in_sec_offset = new_idata_va - last_va
        gap_size = import_dir_in_sec_offset - last_rsz  # ≥ 0，对齐产生的 gap
        # raw_increment 是整个追加区（gap + new_idata + trailing）对齐后的总增量
        needed_raw_after_gap = new_size  # gap_size 已经算在 import_dir_in_sec_offset 里了
        raw_increment = _align(gap_size + needed_raw_after_gap, file_align)

        # 1. SizeOfImage：对齐到 SectionAlignment
        needed_soi = _align(new_idata_va + new_size, sec_align)
        current_soi = struct.unpack_from("<I", data, soi_abs_off)[0]
        if needed_soi > current_soi:
            struct.pack_into("<I", data, soi_abs_off, needed_soi)
            _logger.info("扩展 SizeOfImage: 0x%X → 0x%X", current_soi, needed_soi)

        # 2. Import Directory（DATA_DIRECTORY[1]）— RVA + Size
        struct.pack_into("<I", data, import_dir_rva_off, new_idata_va)
        struct.pack_into("<I", data, import_dir_size_off, new_size)

        # 3. 最后一个 section header — SizeOfRawData + Misc_VirtualSize
        new_rsz = last_rsz + raw_increment
        new_vsz_raw = new_idata_va - last_va + new_size
        new_vsz = _align(new_vsz_raw, sec_align)
        struct.pack_into("<I", data, last_sec_hdr_off + _SH_SIZE_RAW, new_rsz)
        struct.pack_into("<I", data, last_sec_hdr_off + _SH_MISC_VS, new_vsz)
        _logger.info(
            "扩展最后一个 section: RSZ=%d(0x%X), VSZ=0x%X (追加 %d bytes, gap=%d)",
            new_rsz,
            new_rsz,
            new_vsz,
            raw_increment,
            gap_size,
        )

        # 4. 追加 gap padding + 新 .idata + trailing FileAlignment padding 到文件末尾
        if gap_size:
            data.extend(b"\x00" * gap_size)
        data.extend(new_idata)
        trailing = raw_increment - gap_size - new_size
        if trailing:
            data.extend(b"\x00" * trailing)

        _logger.info(
            "PE patch [文件末尾追加]: new .idata %d bytes @ RVA=0x%X (file +%d bytes)",
            new_size,
            new_idata_va,
            raw_increment,
        )

    return PatchResult(
        target_path=Path("unknown"),
        redirected=redirected,
        shim_dll_added=True,
        output_path=Path("unknown"),
    )


def patch_file(
    target: Path,
    *,
    output: Path | None = None,
    shim_dll_name: str = SHIM_DLL_NAME,
) -> PatchResult:
    """对一个 PE 文件执行导入表重定向.

    Args:
        target: 待 patch 的 .pyd / .dll / .exe 路径。
        output: 输出路径；若为 None 则覆盖原文件。
        shim_dll_name: 目标 shim DLL 名字。

    Returns:
        PatchResult 摘要（target_path / output_path 已填充）。

    Raises:
        PEPatchError: 解析 / 重建 / 写回失败。
    """
    target = Path(target)
    if not target.is_file():
        raise PEPatchError(f"目标文件不存在: {target}")

    _logger.info("patch_file: 读取 %s", target)
    pe = pefile.PE(str(target))

    try:
        result = build_patched_pe(pe, shim_dll_name=shim_dll_name)
    except PEPatchError:
        raise
    except Exception as exc:
        raise PEPatchError(f"PE patch 内部错误: {exc!s}") from exc

    # 无匹配 → 不写回，原样返回
    if not result.redirected:
        result = PatchResult(
            target_path=target,
            redirected=[],
            shim_dll_added=False,
            output_path=target,
        )
        return result

    out_path = Path(output) if output else target
    _logger.info("patch_file: 写回 %s", out_path)
    # 直接写 bytearray（pe.write() 从 pefile ctypes 结构重建，不读 __data__）
    out_path.write_bytes(bytes(pe.__data__))

    return PatchResult(
        target_path=target,
        redirected=result.redirected,
        shim_dll_added=result.shim_dll_added,
        output_path=out_path,
    )


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """命令行入口：对指定 PE 文件执行导入表重定向."""
    parser = argparse.ArgumentParser(
        prog="python -m fspack.packaging.win7.patch",
        description="PE 导入表重定向：kernel32 Win8+ API → api-ms-win-core-kernel32-shim.dll",
    )
    parser.add_argument("target", type=str, help="待 patch 的 PE 文件（.pyd / .dll / .exe）")
    parser.add_argument("--output", "-o", type=str, default=None, help="输出路径（默认覆盖原文件）")
    parser.add_argument("--shim", type=str, default=SHIM_DLL_NAME, help="shim DLL 名字")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    target = Path(args.target)
    try:
        result = patch_file(target, output=Path(args.output) if args.output else None, shim_dll_name=args.shim)
    except PEPatchError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    if not result.redirected:
        print(f"[skip] {target}: 无匹配导入，无需 patch")
        return 1

    print(f"[ok] {target} → {result.output_path}")
    for src_dll, func, shim in result.redirected:
        print(f"  {src_dll}!{func} → {shim}!{func}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
