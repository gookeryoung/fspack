"""Win7 PE 导入表原地改名补丁：把 Win8+ API 导入改写为 Win7 原生等价函数.

背景
----
kernel32 / kernelbase 是 KnownDLL，无法用同名 DLL 遮蔽。第三方 wheel 的
扩展（pydantic-core 2.18+、psycopg binary 自带的 libpq 等）静态导入
``kernel32!GetSystemTimePreciseAsFileTime``（Win8+ 引入），Win7 SP1 加载时
loader 按名解析失败，报 ERROR_PROC_NOT_FOUND(127)——用户可见的文案即
"找不到指定的程序"，表现为 ``ImportError: DLL load failed while
importing _pydantic_core``。

技术路线（原地改名，不重建导入表）
--------------------------------
PE 按名导入在加载时由 loader 读 INT/IAT thunk 指向的 Hint/Name 字符串，
到目标 DLL 导出表里查找。因此只要把该字符串**原地改写**为同签名、语义
兼容的 Win7 原生导出名（新名更短，尾部 \\0 填充，RVA 不变）即可：

- IAT 槽位 RVA 一个都不动，代码里 ``call [rip+disp]`` 的引用全部有效；
- 无需新增 shim DLL、无需扩 section / SizeOfImage；
- 幂等：改写后字符串已变成新名，再次运行不命中、不改写。

曾评估并否决的路线——把函数从 kernel32 描述符移入自研 shim DLL 后重建
.idata——**结构性不可行**：x64 代码按固定 RVA 引用 IAT 槽位，重建必然
挪动 IAT（实测 pydantic-core cp313：kernel32 IAT 原始 RVA 0x383000 →
重建后 0x4efe88），除被重定向函数外其余全部导入的调用点悬空，产物可被
pefile 正常解析但一调用 kernel32 即崩溃。原地改名是唯一安全的就地修复。

精度说明
--------
GetSystemTimeAsFileTime 分辨率约 15.6ms（系统定时器 tick），低于
GetSystemTimePreciseAsFileTime 的亚微秒级。pydantic-core（Rust
``SystemTime::now`` 时间戳）与 libpq（连接/语句计时）对此时均不敏感。

同时覆盖普通导入（DataDirectory[1]）与延迟加载导入（DataDirectory[13]），
后者在 Rust/MSVC 扩展中偶见。

CLI::

    python -m fspack.packaging.win7.patch <target.pyd> [--output <out.pyd>]

退出码：0 改写成功；1 无匹配（无需改写）；2 解析/写回失败。
"""

from __future__ import annotations

import argparse
import logging
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fspack.packaging.win7.scan import iter_pe_files

__all__ = [
    "RENAMED_IMPORTS",
    "PEPatchError",
    "PatchResult",
    "RenameRule",
    "main",
    "patch_bytes",
    "patch_dist_win7",
    "patch_file",
]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 改名规则表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenameRule:
    """一条 Win8+ API → Win7 原生等价函数的原地改名规则.

    Attributes:
        src_dll: 源 DLL 名（小写规范；kernel32/kernelbase 是 KnownDLL）。
        src_func: Win7 上不存在的 Win8+ 函数名（导入表中的名字）。
        dst_func: 替换后的 Win7 原生函数名。**必须与 src_func 同签名**，且
            长度不得超过 src_func（原地覆盖、尾部 NUL 填充）。
        reason: 改名原因（日志/报告用）。
    """

    src_dll: str
    src_func: str
    dst_func: str
    reason: str = ""


# 当前支持的改名表；扩展新条目前须人工核对签名兼容性（参数/调用约定/语义）。
RENAMED_IMPORTS: tuple[RenameRule, ...] = (
    RenameRule(
        src_dll="kernel32.dll",
        src_func="GetSystemTimePreciseAsFileTime",
        dst_func="GetSystemTimeAsFileTime",
        reason="Win8+ API；同签名 Win7 原生函数，仅时钟分辨率降低（≈15.6ms）",
    ),
)

# 模块加载期校验：dst 必须不长于 src（原地覆盖的前提），签名核对靠评审。
for _rule in RENAMED_IMPORTS:
    assert len(_rule.dst_func.encode("ascii")) <= len(_rule.src_func.encode("ascii")), (
        f"改名规则 {_rule.src_func} → {_rule.dst_func} 长度不满足原地覆盖前提"
    )

# (src_dll 小写, src_func) -> 规则，O(1) 查询
_RENAME_INDEX: dict[tuple[str, str], RenameRule] = {
    (r.src_dll.lower(), r.src_func): r for r in RENAMED_IMPORTS
}


class PEPatchError(RuntimeError):
    """PE 解析失败 / 改写失败（非 PE 镜像、截断、RVA 越界等）。"""


# ---------------------------------------------------------------------------
# PE 解析：收集导入函数名 → Hint/Name 字符串的文件偏移
# ---------------------------------------------------------------------------
# 与 check.py 同风格的纯标准库解析（不依赖 pefile）。patch 需要的是「哪个
# 文件偏移上的字符串要改」，check 只需要名字列表，故不直接复用其私有解析。


_PE32_MAGIC = 0x10B
_PE32PLUS_MAGIC = 0x20B
_MAX_IMPORT_DLLS = 256
_MAX_THUNKS = 4096
_MAX_DELAY_DESCS = 256
# dlattrRva：延迟加载描述符字段为 RVA（否则为 VA，需减 ImageBase）
_DLATTR_RVA = 0x1


@dataclass(frozen=True)
class _NameRef:
    """一个按名导入的定位信息.

    dll_raw 保留文件里的原始大小写（报告展示用），查找用小写。
    offset 指向函数名首字节（跳过 2 字节 hint）；capacity 为从 offset 到
    字符串 NUL 的字节数，即原地可安全覆盖的最大长度。
    """

    dll_raw: str
    dll_lower: str
    func: str
    offset: int
    capacity: int


@dataclass(frozen=True)
class _PeLayout:
    """PE 头解析产物：thunk 格式与 RVA→文件偏移换算."""

    thunk_fmt: str  # thunk 条目 struct 格式（PE32 "<I" / PE32+ "<Q"）
    thunk_size: int
    ord_flag: int  # 按序号导入标志位（PE32 bit31 / PE32+ bit63）
    image_base: int  # 延迟加载描述符 VA→RVA 换算用
    rva2off: Callable[[int], int]


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _pe_layout(data: bytes) -> tuple[int, _PeLayout]:
    """解析 PE 头，返回 (数据目录偏移, 布局对象).

    布局对象的 rva2off 把 RVA 换算为文件偏移（RVA 越界抛 PEPatchError）。
    """
    if len(data) < 64 or data[:2] != b"MZ":
        raise PEPatchError("非 MZ 文件")
    pe_offset = _u32(data, 0x3C)
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        raise PEPatchError("PE 签名缺失")
    num_sections = _u16(data, pe_offset + 6)
    opt_size = _u16(data, pe_offset + 20)
    opt_offset = pe_offset + 24
    if opt_offset + opt_size > len(data):
        raise PEPatchError("可选头越界")

    magic = _u16(data, opt_offset)
    if magic == _PE32_MAGIC:
        dd_offset, thunk_fmt, thunk_size, ord_flag, image_base_off = (
            opt_offset + 96,
            "<I",
            4,
            1 << 31,
            opt_offset + 28,
        )
    elif magic == _PE32PLUS_MAGIC:
        dd_offset, thunk_fmt, thunk_size, ord_flag, image_base_off = (
            opt_offset + 112,
            "<Q",
            8,
            1 << 63,
            opt_offset + 24,
        )
    else:
        raise PEPatchError(f"未知 PE 可选头魔数: {magic:#x}")
    if dd_offset + 16 * 14 > len(data):  # 至少要到 DataDirectory[13]（延迟导入）
        raise PEPatchError("数据目录越界")
    image_base = _u32(data, image_base_off) if magic == _PE32_MAGIC else struct.unpack_from("<Q", data, image_base_off)[0]

    sec_offset = opt_offset + opt_size
    sections: list[tuple[int, int, int]] = []
    for i in range(num_sections):
        base = sec_offset + i * 40
        sections.append((_u32(data, base + 12), _u32(data, base + 16), _u32(data, base + 20)))

    def rva2off(rva: int) -> int:
        for va, raw_size, raw_ptr in sections:
            if va <= rva < va + raw_size:
                return rva - va + raw_ptr
        raise PEPatchError(f"RVA 越界: {rva:#x}")

    return dd_offset, _PeLayout(
        thunk_fmt=thunk_fmt,
        thunk_size=thunk_size,
        ord_flag=ord_flag,
        image_base=image_base,
        rva2off=rva2off,
    )


def _read_cstr_span(data: bytes, offset: int) -> tuple[str, int]:
    """读 NUL 结尾 ASCII 字符串，返回 (字符串, 从 offset 起到 NUL 的字节数)."""
    end = data.find(b"\x00", offset)
    if end == -1:
        raise PEPatchError("字符串无 NUL 终止符")
    return data[offset:end].decode("ascii", errors="replace"), end - offset


def _collect_name_refs(
    data: bytes,
    thunk_table_off: int,
    dll_raw: str,
    layout: _PeLayout,
) -> list[_NameRef]:
    """遍历一个 thunk 数组（INT 或 IAT），收集按名导入的 _NameRef."""
    refs: list[_NameRef] = []
    cursor = thunk_table_off
    for _ in range(_MAX_THUNKS):
        (value,) = struct.unpack_from(layout.thunk_fmt, data, cursor)
        if value == 0:
            break
        if not value & layout.ord_flag:
            name_off = layout.rva2off(value & 0x7FFFFFFF) + 2  # 跳过 2 字节 hint
            func, capacity = _read_cstr_span(data, name_off)
            refs.append(_NameRef(dll_raw=dll_raw, dll_lower=dll_raw.lower(), func=func, offset=name_off, capacity=capacity))
        cursor += layout.thunk_size
    return refs


def _import_name_refs(data: bytes) -> list[_NameRef]:
    """收集普通导入（DataDirectory[1]）全部按名导入条目.

    同时遍历 OFT（OriginalFirstThunk）与 FT（FirstThunk/IAT）两个数组——
    未绑定导入两者通常指向同一 Hint/Name 字符串，但绑定工具或特殊链接器
    可能产生独立副本，逐条收集后按 offset 去重。
    """
    dd_offset, layout = _pe_layout(data)
    import_rva = _u32(data, dd_offset + 8)
    if not import_rva:
        return []
    refs: list[_NameRef] = []
    cursor = layout.rva2off(import_rva)
    for _ in range(_MAX_IMPORT_DLLS):
        oft, _ts, _fc, name_rva, ft = struct.unpack_from("<IIIII", data, cursor)
        if oft == 0 and name_rva == 0 and ft == 0:
            break
        dll_raw, _ = _read_cstr_span(data, layout.rva2off(name_rva))
        for thunk_rva in (oft, ft):
            if thunk_rva:
                refs.extend(_collect_name_refs(data, layout.rva2off(thunk_rva), dll_raw, layout))
        cursor += 20
    return refs


def _delayload_name_refs(data: bytes) -> list[_NameRef]:
    """收集延迟加载导入（DataDirectory[13]）全部按名导入条目."""
    dd_offset, layout = _pe_layout(data)
    delay_rva = _u32(data, dd_offset + 13 * 8)
    if not delay_rva:
        return []
    refs: list[_NameRef] = []
    cursor = layout.rva2off(delay_rva)
    for _ in range(_MAX_DELAY_DESCS):
        gr_attrs, sz_name, _phmod, p_iat, p_int, *_ = struct.unpack_from("<IIIIIIII", data, cursor)
        if gr_attrs == 0 and sz_name == 0 and p_iat == 0 and p_int == 0:
            break
        name_addr = sz_name if gr_attrs & _DLATTR_RVA else sz_name - layout.image_base
        dll_raw, _ = _read_cstr_span(data, layout.rva2off(name_addr))
        int_addr = p_int or p_iat
        if int_addr:
            thunk_addr = int_addr if gr_attrs & _DLATTR_RVA else int_addr - layout.image_base
            refs.extend(_collect_name_refs(data, layout.rva2off(thunk_addr), dll_raw, layout))
        cursor += 32
    return refs


# ---------------------------------------------------------------------------
# 改写
# ---------------------------------------------------------------------------


def patch_bytes(data: bytes) -> tuple[bytes, tuple[tuple[str, str, str], ...]]:
    """对 PE 字节流执行原地改名，返回 (新字节流, 改名记录元组).

    改名记录形如 ``(DLL 名, 原函数名, 新函数名)``。无命中时原样返回、
    记录为空；幂等——对已改写的输入再次运行不产生任何变化。

    Raises:
        PEPatchError: 非 PE 镜像 / 结构损坏。
    """
    refs = _import_name_refs(data) + _delayload_name_refs(data)

    # 同一字符串可能被 OFT/FT 多个 thunk 引用，按文件偏移去重
    hits: dict[int, tuple[_NameRef, RenameRule]] = {}
    for ref in refs:
        rule = _RENAME_INDEX.get((ref.dll_lower, ref.func))
        if rule is not None:
            hits[ref.offset] = (ref, rule)

    if not hits:
        return bytes(data), ()

    buf = bytearray(data)
    renamed: list[tuple[str, str, str]] = []
    for offset, (ref, rule) in sorted(hits.items()):
        dst = rule.dst_func.encode("ascii") + b"\x00"
        if len(dst) > ref.capacity:  # 规则表已保证，双保险
            raise PEPatchError(
                f"{ref.dll_raw}!{ref.func} 替换名超出原字符串容量（{len(dst)} > {ref.capacity}）"
            )
        buf[offset : offset + ref.capacity] = dst + b"\x00" * (ref.capacity - len(dst))
        renamed.append((ref.dll_raw, rule.src_func, rule.dst_func))
        _logger.info("导入改名: %s!%s → %s（@文件偏移 0x%X）", ref.dll_raw, rule.src_func, rule.dst_func, offset)

    return bytes(buf), tuple(renamed)


# ---------------------------------------------------------------------------
# 文件级 / dist 级入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatchResult:
    """一次 PE 改名的结果摘要，用于日志 / 报告.

    Attributes:
        target_path: 输入文件路径。
        output_path: 输出文件路径（未改写时等于 target_path）。
        renamed: 改名记录 (DLL, 原名, 新名) 元组。
        changed: 是否发生了改写（False = 无命中，文件未动）。
    """

    target_path: Path
    output_path: Path
    renamed: tuple[tuple[str, str, str], ...] = ()
    changed: bool = False


def patch_file(target: Path, *, output: Path | None = None) -> PatchResult:
    """对一个 PE 文件执行导入表原地改名.

    Args:
        target: 待改写的 .pyd / .dll / .exe 路径。
        output: 输出路径；None 表示原地覆盖。无命中时不写任何文件。

    Returns:
        PatchResult 摘要。

    Raises:
        PEPatchError: 目标不存在 / 非 PE / 改写失败。
    """
    target = Path(target)
    if not target.is_file():
        raise PEPatchError(f"目标文件不存在: {target}")

    data = target.read_bytes()
    patched, renamed = patch_bytes(data)
    if not renamed:
        _logger.info("目标文件无匹配的 Win8+ 导入，无需改写: %s", target)
        return PatchResult(target_path=target, output_path=target, renamed=(), changed=False)

    out_path = Path(output) if output else target
    out_path.write_bytes(patched)
    _logger.info("已写回 %s（%d 条改名）", out_path, len(renamed))
    return PatchResult(target_path=target, output_path=out_path, renamed=renamed, changed=True)


def patch_dist_win7(dist_dir: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """遍历 dist 全部 PE 文件执行原地改名，返回 (相对路径, 改名记录) 列表.

    只改写命中改名规则的文件；非 PE 文件（解析失败）静默跳过——由
    :func:`fspack.packaging.win7.scan.scan_dist_win7` 统一记入 skipped。
    必须在 :func:`scan_dist_win7` **之前**调用，扫描报告才反映改写后的
    最终兼容状态。

    Returns:
        ((相对路径, (("KERNEL32.dll!旧名→新名", ...)), ...) 元组；空元组
        表示 dist 内无文件需要改写。
    """
    results: list[tuple[str, tuple[str, ...]]] = []
    for path in iter_pe_files(dist_dir):
        try:
            data = path.read_bytes()
            patched, renamed = patch_bytes(data)
        except PEPatchError as exc:
            _logger.debug("跳过非 PE 文件 %s: %s", path, exc)
            continue
        if not renamed:
            continue
        path.write_bytes(patched)
        rel = path.relative_to(dist_dir).as_posix()
        records = tuple(f"{dll}!{old}→{new}" for dll, old, new in renamed)
        _logger.info("Win7 导入改名 %s: %s", rel, "；".join(records))
        results.append((rel, records))
    return tuple(results)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """命令行入口：对指定 PE 文件执行导入表原地改名."""
    parser = argparse.ArgumentParser(
        prog="python -m fspack.packaging.win7.patch",
        description="PE 导入表原地改名：kernel32 Win8+ API → Win7 原生等价函数",
    )
    parser.add_argument("target", type=str, help="待改写的 PE 文件（.pyd / .dll / .exe）")
    parser.add_argument("--output", "-o", type=str, default=None, help="输出路径（默认覆盖原文件）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    target = Path(args.target)
    try:
        result = patch_file(target, output=Path(args.output) if args.output else None)
    except PEPatchError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    if not result.changed:
        print(f"[skip] {target}: 无匹配导入，无需改写")
        return 1

    print(f"[ok] {target} → {result.output_path}")
    for dll, old, new in result.renamed:
        print(f"  {dll}!{old} → {new}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
