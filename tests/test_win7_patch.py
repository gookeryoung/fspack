"""Win7 patch 模块单元测试 — PE 导入表原地改名.

fixture 为纯标准库构造的最小 PE64（含普通导入 + 可选延迟加载导入），
不依赖 pefile 与本机 site-packages；另有一个可选的真实 pydantic_core
端到端用例（本机存在才执行）。
"""

from __future__ import annotations

import shutil
import struct
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

from fspack.packaging.win7 import patch
from fspack.packaging.win7.check import _parse_pe, check_win7_imports
from fspack.packaging.win7.patch import PEPatchError, patch_bytes, patch_dist_win7, patch_file
from fspack.packaging.win7.scan import render_win7_report, scan_dist_win7

# ---------------------------------------------------------------------------
# 合成 PE64 构造器
# ---------------------------------------------------------------------------

_SEC_BASE = 0x200  # SectionAlignment = FileAlignment = 0x200，RVA 与文件偏移一致


def _align_up(value: int, alignment: int = 0x200) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _build_pe64(  # noqa: PLR0912 — PE 构造需按描述符/名字/INT/IAT/HintName 多步布局
    imports: dict[str, list[str | int]],
    *,
    delay_imports: dict[str, list[str | int]] | None = None,
) -> bytes:
    """构造最小合法 PE64，imports 值为函数名列表或 int（按序号导入）.

    产出可通过 fspack.packaging.win7.check / patch 的解析，但不做真实
    加载——测试只覆盖文件级解析与改写。
    """
    sec = bytearray()

    def emit(b: bytes) -> int:
        rva = _SEC_BASE + len(sec)
        sec.extend(b)
        return rva

    def align(n: int) -> None:
        while len(sec) % n:
            sec.append(0)

    # -- 普通导入：描述符表（含终止符）→ DLL 名 → INT → IAT(FT) → Hint/Name --
    desc_off = len(sec)
    sec.extend(b"\x00" * (20 * (len(imports) + 1)))

    name_rvas: dict[str, int] = {}
    int_rvas: dict[str, int] = {}
    ft_rvas: dict[str, int] = {}
    hintname_rvas: dict[tuple[str, int], int] = {}

    for dll, _funcs in imports.items():
        align(2)
        name_rvas[dll] = emit(dll.encode() + b"\x00")
    for dll, funcs in imports.items():
        align(8)
        int_rvas[dll] = emit(b"\x00" * (8 * (len(funcs) + 1)))
    for dll, funcs in imports.items():
        align(8)
        ft_rvas[dll] = emit(b"\x00" * (8 * (len(funcs) + 1)))
    for dll, funcs in imports.items():
        for i, func in enumerate(funcs):
            if isinstance(func, int):
                continue
            align(2)
            hintname_rvas[(dll, i)] = emit(struct.pack("<H", 0) + func.encode() + b"\x00")

    def thunk_bytes(dll: str, funcs: list[str | int], table: dict[tuple[str, int], int]) -> bytes:
        return (
            b"".join(
                struct.pack("<Q", 0x8000000000000000 | func)
                if isinstance(func, int)
                else struct.pack("<Q", table[(dll, i)])
                for i, func in enumerate(funcs)
            )
            + b"\x00" * 8
        )

    for dll, funcs in imports.items():
        vals = thunk_bytes(dll, funcs, hintname_rvas)
        off = int_rvas[dll] - _SEC_BASE
        sec[off : off + len(vals)] = vals
        off = ft_rvas[dll] - _SEC_BASE
        sec[off : off + len(vals)] = vals
    for idx, dll in enumerate(imports):
        struct.pack_into("<IIIII", sec, desc_off + idx * 20, int_rvas[dll], 0, 0, name_rvas[dll], ft_rvas[dll])

    # -- 延迟加载导入（DataDirectory[13]，描述符 32 字节，grAttrs=dlattrRva）--
    ddesc_rva = 0
    if delay_imports:
        align(8)
        ddesc_off = len(sec)
        ddesc_rva = _SEC_BASE + ddesc_off
        sec.extend(b"\x00" * (32 * (len(delay_imports) + 1)))
        dname_rvas: dict[str, int] = {}
        dint_rvas: dict[str, int] = {}
        dhint_rvas: dict[tuple[str, int], int] = {}
        for dll, _funcs in delay_imports.items():
            align(2)
            dname_rvas[dll] = emit(dll.encode() + b"\x00")
        for dll, funcs in delay_imports.items():
            align(8)
            dint_rvas[dll] = emit(b"\x00" * (8 * (len(funcs) + 1)))
        for dll, funcs in delay_imports.items():
            for i, func in enumerate(funcs):
                if isinstance(func, int):
                    continue
                align(2)
                dhint_rvas[(dll, i)] = emit(struct.pack("<H", 0) + func.encode() + b"\x00")
        for dll, funcs in delay_imports.items():
            vals = thunk_bytes(dll, funcs, dhint_rvas)
            off = dint_rvas[dll] - _SEC_BASE
            sec[off : off + len(vals)] = vals
        for idx, dll in enumerate(delay_imports):
            struct.pack_into(
                "<IIIIIIII",
                sec,
                ddesc_off + idx * 32,
                0x1,  # grAttrs = dlattrRva
                dname_rvas[dll],
                0,  # phmod
                dint_rvas[dll],  # pIAT
                dint_rvas[dll],  # pINT
                0,
                0,
                0,
            )

    # -- 头部 --
    dos = bytearray(64)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 240, 0x2022)
    opt = bytearray(240)
    struct.pack_into("<H", opt, 0, 0x20B)  # PE32+
    struct.pack_into("<Q", opt, 24, 0x180000000)  # ImageBase
    struct.pack_into("<I", opt, 32, 0x200)  # SectionAlignment
    struct.pack_into("<I", opt, 36, 0x200)  # FileAlignment
    struct.pack_into("<H", opt, 40, 6)  # MajorOSVer（6.1 = Win7）
    struct.pack_into("<H", opt, 42, 1)
    struct.pack_into("<I", opt, 56, _align_up(_SEC_BASE + len(sec)))  # SizeOfImage
    struct.pack_into("<I", opt, 60, 0x200)  # SizeOfHeaders
    struct.pack_into("<H", opt, 68, 3)  # Subsystem = console
    struct.pack_into("<I", opt, 108, 16)  # NumberOfRvaAndSizes
    import_rva = _SEC_BASE + desc_off
    struct.pack_into("<II", opt, 112 + 1 * 8, import_rva, 20 * (len(imports) + 1))
    if delay_imports:
        struct.pack_into("<II", opt, 112 + 13 * 8, ddesc_rva, 32 * (len(delay_imports) + 1))
    sec_hdr = struct.pack(
        "<8sIIIIIIHHI",
        b".idata",
        len(sec),
        _SEC_BASE,
        _align_up(len(sec)),
        _SEC_BASE,
        0,
        0,
        0,
        0,
        0x40000040,
    )

    file_data = bytes(dos) + b"PE\x00\x00" + coff + bytes(opt) + sec_hdr
    file_data += b"\x00" * (_SEC_BASE - len(file_data))
    file_data += bytes(sec)
    file_data += b"\x00" * (_align_up(len(sec)) - len(sec))
    return file_data


_KERNEL32_IMPORTS: dict[str, list[str | int]] = {
    "KERNEL32.dll": ["GetSystemTimePreciseAsFileTime", "GetSystemTimeAsFileTime", "GetCurrentThreadId"],
    "api-ms-win-crt-heap-l1-1-0.dll": [1],  # 按序号导入，须原样保留
    "python313.dll": ["PyImport_AppendInittab"],
}

_KERNEL32_SAFE_IMPORTS: dict[str, list[str | int]] = {
    "KERNEL32.dll": ["GetSystemTimeAsFileTime", "GetCurrentThreadId"],
}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _imports_of(data: bytes) -> dict[str, list[str]]:
    """{DLL 名: 函数名列表}（复用 check 模块解析，保证与门禁同口径）."""
    return _parse_pe(data).imports


def _write(tmp_dir: Path, data: bytes, name: str = "synthetic.dll") -> Path:
    """字节流落盘供 check 模块解析（check 只接受路径）."""
    f = tmp_dir / name
    f.write_bytes(data)
    return f


# ---------------------------------------------------------------------------
# 改名规则表
# ---------------------------------------------------------------------------


class TestRenamedImports:
    """RENAMED_IMPORTS 注册表."""

    def test_contains_precise_rename(self) -> None:
        pairs = {(r.src_dll, r.src_func, r.dst_func) for r in patch.RENAMED_IMPORTS}
        assert ("kernel32.dll", "GetSystemTimePreciseAsFileTime", "GetSystemTimeAsFileTime") in pairs

    def test_dst_not_longer_than_src(self) -> None:
        for r in patch.RENAMED_IMPORTS:
            assert len(r.dst_func) <= len(r.src_func), "原地覆盖要求新名不长于原名"


# ---------------------------------------------------------------------------
# patch_bytes 核心改写
# ---------------------------------------------------------------------------


class TestPatchBytes:
    def test_renames_precise_in_place(self, tmp_path: Path) -> None:
        data = _build_pe64(_KERNEL32_IMPORTS)
        patched, renamed = patch_bytes(data)

        assert len(renamed) == 1
        dll, old, new = renamed[0]
        assert dll == "KERNEL32.dll"
        assert (old, new) == ("GetSystemTimePreciseAsFileTime", "GetSystemTimeAsFileTime")
        assert len(patched) == len(data), "原地改写不得改变文件大小"

        # 改写后 kernel32 导入表里 Precise → GetSystemTimeAsFileTime（重名合法），
        # 序号导入原样保留，check 门禁通过
        imports = _imports_of(patched)
        assert imports["KERNEL32.dll"] == [
            "GetSystemTimeAsFileTime",
            "GetSystemTimeAsFileTime",
            "GetCurrentThreadId",
        ]
        assert imports["api-ms-win-crt-heap-l1-1-0.dll"] == ["#1"]
        assert check_win7_imports(_write(tmp_path, patched)).ok

    def test_string_region_padded_with_nul(self) -> None:
        data = _build_pe64(_KERNEL32_IMPORTS)
        refs = {r.offset: r for r in patch._import_name_refs(data) if r.func == "GetSystemTimePreciseAsFileTime"}
        assert len(refs) == 1
        offset, ref = next(iter(refs.items()))
        patched, _ = patch_bytes(data)
        region = patched[offset : offset + ref.capacity]
        assert region == b"GetSystemTimeAsFileTime\x00" + b"\x00" * (ref.capacity - 24)

    def test_all_thunk_offsets_preserved(self) -> None:
        """改名前后所有 Hint/Name 条目的文件偏移不变（IAT 槽位不动的根本保证）."""
        data = _build_pe64(_KERNEL32_IMPORTS)
        before = {(r.offset, r.func) for r in patch._import_name_refs(data)}
        patched, _ = patch_bytes(data)
        after = {(r.offset, r.func) for r in patch._import_name_refs(patched)}
        assert {off for off, _ in before} == {off for off, _ in after}, "所有条目偏移不变"
        assert len(before - after) == 1, "只有 Precise 一条的名字发生变化"

    def test_delay_load_imports_patched(self, tmp_path: Path) -> None:
        data = _build_pe64(_KERNEL32_SAFE_IMPORTS, delay_imports={"KERNEL32.dll": ["GetSystemTimePreciseAsFileTime"]})
        patched, renamed = patch_bytes(data)
        assert len(renamed) == 1
        assert check_win7_imports(_write(tmp_path, patched)).ok

    def test_no_match_returns_unchanged(self) -> None:
        data = _build_pe64(_KERNEL32_SAFE_IMPORTS)
        patched, renamed = patch_bytes(data)
        assert renamed == ()
        assert patched == data

    def test_idempotent(self) -> None:
        once, _ = patch_bytes(_build_pe64(_KERNEL32_IMPORTS))
        twice, renamed = patch_bytes(once)
        assert renamed == ()
        assert twice == once

    def test_non_pe_raises(self) -> None:
        with pytest.raises(PEPatchError, match="非 MZ"):
            patch_bytes(b"not a pe file at all........")


# ---------------------------------------------------------------------------
# patch_file 文件级
# ---------------------------------------------------------------------------


class TestPatchFile:
    def test_overwrite_in_place(self, tmp_path: Path) -> None:
        work = tmp_path / "target.pyd"
        work.write_bytes(_build_pe64(_KERNEL32_IMPORTS))
        result = patch_file(work)
        assert result.changed
        assert result.output_path == work
        assert check_win7_imports(work).ok

    def test_output_to_different_path_keeps_original(self, tmp_path: Path) -> None:
        work = tmp_path / "original.pyd"
        out = tmp_path / "patched.pyd"
        original = _build_pe64(_KERNEL32_IMPORTS)
        work.write_bytes(original)

        result = patch_file(work, output=out)
        assert result.changed
        assert out.read_bytes() != original
        assert work.read_bytes() == original, "指定 output 时原文件不动"

    def test_no_match_writes_nothing(self, tmp_path: Path) -> None:
        work = tmp_path / "safe.dll"
        work.write_bytes(_build_pe64(_KERNEL32_SAFE_IMPORTS))
        out = tmp_path / "out.dll"

        result = patch_file(work, output=out)
        assert not result.changed
        assert not out.exists(), "无命中不应产生输出文件"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(PEPatchError, match="目标文件不存在"):
            patch_file(tmp_path / "does-not-exist.pyd")


# ---------------------------------------------------------------------------
# patch_dist_win7 dist 级 + 与 scan 集成
# ---------------------------------------------------------------------------


class TestPatchDistWin7:
    def test_patches_only_matching_files(self, tmp_path: Path) -> None:
        hit = tmp_path / "site-packages" / "pydantic_core" / "_pydantic_core.cp313-win_amd64.pyd"
        safe = tmp_path / "runtime" / "python313.dll"
        note = tmp_path / "site-packages" / "README.txt"
        hit.parent.mkdir(parents=True)
        safe.parent.mkdir(parents=True)
        hit.write_bytes(_build_pe64(_KERNEL32_IMPORTS))
        safe_bytes = _build_pe64(_KERNEL32_SAFE_IMPORTS)
        safe.write_bytes(safe_bytes)
        note.write_text("not a pe")

        results = patch_dist_win7(tmp_path)

        assert len(results) == 1
        rel, records = results[0]
        assert rel == "site-packages/pydantic_core/_pydantic_core.cp313-win_amd64.pyd"
        assert records == ("KERNEL32.dll!GetSystemTimePreciseAsFileTime→GetSystemTimeAsFileTime",)
        assert safe.read_bytes() == safe_bytes, "无命中文件不得被改写"
        assert check_win7_imports(hit).ok

    def test_scan_report_after_patch_is_clean(self, tmp_path: Path) -> None:
        """端到端：改名 → 扫描 → 报告含改写段且无违规（对应 cndb 3.13 场景）."""
        hit = tmp_path / "site-packages" / "pydantic_core" / "_pydantic_core.cp313-win_amd64.pyd"
        hit.parent.mkdir(parents=True)
        hit.write_bytes(_build_pe64(_KERNEL32_IMPORTS))

        # 改名前扫描：pydantic_core 违规
        before = scan_dist_win7(tmp_path)
        assert not before.ok

        patched = patch_dist_win7(tmp_path)
        report = dc_replace(scan_dist_win7(tmp_path), patched=patched)

        assert report.ok
        text = render_win7_report(report)
        assert "[已改写]" in text
        assert "GetSystemTimePreciseAsFileTime→GetSystemTimeAsFileTime" in text
        assert "[违规]" not in text

    def test_empty_dist(self, tmp_path: Path) -> None:
        assert patch_dist_win7(tmp_path) == ()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_exit_0_on_success(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        work = tmp_path / "target.pyd"
        work.write_bytes(_build_pe64(_KERNEL32_IMPORTS))
        rc = patch.main([str(work)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[ok]" in out
        assert "GetSystemTimePreciseAsFileTime" in out

    def test_exit_1_on_no_match(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        work = tmp_path / "safe.dll"
        work.write_bytes(_build_pe64(_KERNEL32_SAFE_IMPORTS))
        rc = patch.main([str(work)])
        assert rc == 1
        assert "skip" in capsys.readouterr().out

    def test_exit_2_on_missing_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = patch.main([str(tmp_path / "nope.pyd")])
        assert rc == 2
        assert "FAIL" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 真实文件可选端到端（本机 site-packages 有 pydantic_core cp313 才执行）
# ---------------------------------------------------------------------------

_PYDANTIC_CORE_CANDIDATES = [
    Path(
        r"C:\Users\zhou\AppData\Roaming\Python\Python313\site-packages\pydantic_core\_pydantic_core.cp313-win_amd64.pyd"
    ),
]


def _find_real_target() -> Path | None:
    for p in _PYDANTIC_CORE_CANDIDATES:
        if not p.is_file():
            continue
        try:
            result = check_win7_imports(p)
        except Exception:  # 探测阶段任何解析异常都视为无目标
            continue
        if any("GetSystemTimePreciseAsFileTime" in v.target for v in result.violations):
            return p
    return None


class TestRealPydanticCore:
    def test_patch_real_pyd(self, tmp_path: Path) -> None:
        target = _find_real_target()
        if target is None:
            pytest.skip("本机未找到含 GetSystemTimePreciseAsFileTime 的 pydantic_core cp313")

        work = tmp_path / "target.pyd"
        shutil.copy2(target, work)
        size_before = work.stat().st_size

        result = patch_file(work)
        assert result.changed
        assert any(old == "GetSystemTimePreciseAsFileTime" for _, old, _ in result.renamed)
        assert work.stat().st_size == size_before, "原地改名不得改变文件大小"
        assert check_win7_imports(work).ok
