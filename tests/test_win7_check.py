"""win7_check 模块测试：合成 PE fixture + 内置 shim 实测.

合成 PE 构造器 `_build_pe` 生成单 .rdata 节的最小镜像（PE32+/PE32 双形态），
函数级导入表与导出表均可控，保证测试封闭（不依赖 refs/ 下 gitignored 样本）。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

import fspack
from fspack.packaging.win7_check import PeParseError, check_win7_imports, main

# 内置 api-ms-win-core-path shim（随 fspack 分发，LGPL-2.1）
_ASSETS_SHIM = Path(fspack.__file__).parent / "assets" / "runtime" / "api-ms-win-core-path-l1-1-0.dll"

_FILE_ALIGN = 0x200
_RDATA_RVA = 0x1000


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


class _Blob:
    """测试用 .rdata 节内容构造器：顺序追加内容并返回对应 RVA."""

    def __init__(self, rva_base: int) -> None:
        self._rva_base = rva_base
        self.data = bytearray()

    def add(self, content: bytes) -> int:
        """追加原始字节，返回其 RVA."""
        rva = self._rva_base + len(self.data)
        self.data.extend(content)
        return rva

    def add_str(self, text: str) -> int:
        """追加 NUL 结尾 ASCII 字符串，返回其 RVA."""
        return self.add(text.encode("ascii") + b"\x00")


def _append_import_table(blob: _Blob, imports: dict[str, list[str]], thunk_fmt: str, ord_flag: int) -> int:
    """向 blob 追加导入表（含按名/按序号导入），返回导入目录 RVA，无导入时 0."""
    descriptors: list[tuple[int, int]] = []
    for dll, funcs in imports.items():
        name_rva = blob.add_str(dll)
        values: list[int] = []
        for func in funcs:
            if func.startswith("#"):
                values.append(ord_flag | int(func[1:]))
            else:
                values.append(blob.add(b"\x00\x00" + func.encode("ascii") + b"\x00"))
        thunk = bytearray()
        for value in values:
            thunk += struct.pack(thunk_fmt, value)
        thunk += struct.pack(thunk_fmt, 0)
        descriptors.append((name_rva, blob.add(bytes(thunk))))

    if not descriptors:
        return 0
    table = bytearray()
    for name_rva, thunk_rva in descriptors:
        table += struct.pack("<IIIII", thunk_rva, 0, 0, name_rva, thunk_rva)
    table += b"\x00" * 20
    return blob.add(bytes(table))


def _append_export_table(blob: _Blob, exports: list[str]) -> int:
    """向 blob 追加导出表（按名导出），返回导出目录 RVA，无导出时 0."""
    if not exports:
        return 0
    name_rvas = [blob.add_str(name) for name in exports]
    functions_rva = blob.add(b"".join(struct.pack("<I", r) for r in name_rvas))
    names_rva = blob.add(b"".join(struct.pack("<I", r) for r in name_rvas))
    ordinals_rva = blob.add(b"\x00\x00" * len(name_rvas))
    dll_name_rva = blob.add_str("fixture.dll")
    return blob.add(
        struct.pack(
            "<IIHHIIIIIII",
            0,
            0,
            0,
            0,
            dll_name_rva,
            1,
            len(exports),
            len(exports),
            functions_rva,
            names_rva,
            ordinals_rva,
        )
    )


def _optional_header(pe32plus: bool, image_size: int, raw_ptr: int, dirs: list[tuple[int, int]]) -> bytes:
    """构造可选头（PE32/PE32+ 标准字段 + 16 个数据目录）."""
    if pe32plus:
        standard = struct.pack(
            "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
            0x20B,
            14,
            44,
            0,
            0,
            0,
            0,
            _RDATA_RVA,
            0x140000000,
            0x1000,
            _FILE_ALIGN,
            6,
            1,
            0,
            0,
            6,
            1,
            0,
            image_size,
            raw_ptr,
            0,
            3,
            0,
            0x100000,
            0x1000,
            0x100000,
            0x1000,
            0,
            16,
        )
    else:
        standard = struct.pack(
            "<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII",
            0x10B,
            14,
            44,
            0,
            0,
            0,
            0,
            _RDATA_RVA,
            0,
            0x10000000,
            0x1000,
            _FILE_ALIGN,
            6,
            1,
            0,
            0,
            6,
            1,
            0,
            image_size,
            raw_ptr,
            0,
            3,
            0,
            0x100000,
            0x1000,
            0x100000,
            0x1000,
            0,
            16,
        )
    return standard + b"".join(struct.pack("<II", *entry) for entry in dirs)


def _build_pe(
    imports: dict[str, list[str]] | None = None,
    exports: list[str] | None = None,
    *,
    pe32plus: bool = True,
    machine: int = 0x8664,
    import_rva_override: int | None = None,
) -> bytes:
    """构造最小 PE 镜像：单 .rdata 节承载导入表/导出表（解析测试 fixture）.

    imports 中函数名以 ``#`` 开头表示按序号导入（如 ``"#15"``）；
    import_rva_override 用于构造 RVA 越界样本。
    """
    thunk_fmt = "<Q" if pe32plus else "<I"
    ord_flag = 1 << 63 if pe32plus else 1 << 31
    blob = _Blob(_RDATA_RVA)
    import_rva = _append_import_table(blob, imports or {}, thunk_fmt, ord_flag)
    export_rva = _append_export_table(blob, exports or [])
    if import_rva_override is not None:
        import_rva = import_rva_override

    opt_size = 240 if pe32plus else 224
    raw_ptr = _align(0x40 + 24 + opt_size + 40, _FILE_ALIGN)
    raw_size = _align(len(blob.data), _FILE_ALIGN)
    dirs: list[tuple[int, int]] = [(0, 0)] * 16
    if export_rva:
        dirs[0] = (export_rva, 40)
    if import_rva:
        dirs[1] = (import_rva, 0x200)

    dos = bytearray(0x40)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)
    headers = bytes(dos) + b"PE\x00\x00"
    headers += struct.pack("<HHIIIHH", machine, 1, 0, 0, 0, opt_size, 0x2022)
    headers += _optional_header(pe32plus, _RDATA_RVA + max(raw_size, _FILE_ALIGN), raw_ptr, dirs)
    headers += struct.pack(
        "<8sIIIIIIHHI", b".rdata\0\0", len(blob.data), _RDATA_RVA, raw_size, raw_ptr, 0, 0, 0, 0, 0x40000040
    )

    image = bytearray(headers)
    image += b"\x00" * (raw_ptr - len(image))
    image += blob.data
    return bytes(image)


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    target = tmp_path / name
    target.write_bytes(data)
    return target


# ---------------------------------------------------------------------------
# 判定逻辑
# ---------------------------------------------------------------------------


def test_clean_imports_pass(tmp_path: Path) -> None:
    """Win7 可用的 kernel32/实体 DLL 导入应通过且无附加提示."""
    dll = _write(
        tmp_path,
        "python313.dll",
        _build_pe({"KERNEL32.dll": ["GetModuleHandleW", "CreateFileW"], "VCRUNTIME140.dll": ["memcpy"]}),
    )
    result = check_win7_imports(dll)
    assert result.ok
    assert result.violations == ()
    assert result.shim_dlls == ()


def test_win8_api_blocked(tmp_path: Path) -> None:
    """kernel32 的 Win8/Win8.1 黑名单 API 应逐条拦截并标明最低系统."""
    dll = _write(
        tmp_path,
        "python313.dll",
        _build_pe({"KERNEL32.dll": ["CopyFile2", "PssCaptureSnapshot", "GetSystemTimePreciseAsFileTime"]}),
    )
    result = check_win7_imports(dll)
    assert not result.ok
    targets = [v.target for v in result.violations]
    assert targets == [
        "KERNEL32.dll!CopyFile2",
        "KERNEL32.dll!GetSystemTimePreciseAsFileTime",
        "KERNEL32.dll!PssCaptureSnapshot",
    ]
    reasons = {v.target: v.reason for v in result.violations}
    assert "Win8+" in reasons["KERNEL32.dll!CopyFile2"]
    assert "Win8.1+" in reasons["KERNEL32.dll!PssCaptureSnapshot"]


def test_kernelbase_checked(tmp_path: Path) -> None:
    """kernelbase 与 kernel32 同样按函数黑名单校验."""
    dll = _write(tmp_path, "a.dll", _build_pe({"KERNELBASE.dll": ["WaitOnAddress"]}))
    result = check_win7_imports(dll)
    assert [v.target for v in result.violations] == ["KERNELBASE.dll!WaitOnAddress"]


def test_path_apiset_requires_shim(tmp_path: Path) -> None:
    """api-ms-win-core-path-* 应判"需 shim"而非违规."""
    dll = _write(
        tmp_path,
        "python313.dll",
        _build_pe({"api-ms-win-core-path-l1-1-0.dll": ["PathCchCanonicalizeEx", "PathCchCombineEx"]}),
    )
    result = check_win7_imports(dll)
    assert result.ok
    assert result.shim_dlls == ("api-ms-win-core-path-l1-1-0.dll",)
    assert any("shim" in note for note in result.notes)


def test_crt_apiset_note(tmp_path: Path) -> None:
    """api-ms-win-crt-* 应通过并附 UCRT 前提提示."""
    dll = _write(tmp_path, "a.dll", _build_pe({"api-ms-win-crt-runtime-l1-1-0.dll": ["exit"]}))
    result = check_win7_imports(dll)
    assert result.ok
    assert any("UCRT" in note for note in result.notes)


@pytest.mark.parametrize("dll_name", ["api-ms-win-core-synch-l1-2-0.dll", "ext-ms-win-x-y.dll"])
def test_unknown_apiset_blocked(tmp_path: Path, dll_name: str) -> None:
    """未知 api-ms-*/ext-ms-* API Set 应判违规（无 shim 可用）."""
    dll = _write(tmp_path, "a.dll", _build_pe({dll_name: ["WaitOnAddress"]}))
    result = check_win7_imports(dll)
    assert not result.ok
    assert result.violations[0].target == dll_name


def test_win8_system_dlls(tmp_path: Path) -> None:
    """SHCORE.dll/combase.dll 为 Win8+ 系统库，DLL 级违规."""
    dll = _write(
        tmp_path, "a.dll", _build_pe({"SHCORE.dll": ["GetDpiForMonitor"], "combase.dll": ["CoIncrementMTAUsage"]})
    )
    result = check_win7_imports(dll)
    assert {v.target for v in result.violations} == {"SHCORE.dll", "combase.dll"}


def test_ordinal_import_noted(tmp_path: Path) -> None:
    """按序号导入无法按名校验，应通过并提示."""
    dll = _write(tmp_path, "a.dll", _build_pe({"KERNEL32.dll": ["#15"]}))
    result = check_win7_imports(dll)
    assert result.ok
    assert any("#15" in note for note in result.notes)


# ---------------------------------------------------------------------------
# shim 覆盖校验与内置 shim 实测
# ---------------------------------------------------------------------------


def test_shim_coverage_missing(tmp_path: Path) -> None:
    """shim 缺少被导入函数时判失败并列出缺失项."""
    shim = _write(tmp_path, "shim.dll", _build_pe(exports=["PathCchCanonicalizeEx"]))
    dll = _write(
        tmp_path,
        "python313.dll",
        _build_pe({"api-ms-win-core-path-l1-1-0.dll": ["PathCchCanonicalizeEx", "PathCchCombineEx"]}),
    )
    result = check_win7_imports(dll, shim=shim)
    assert not result.ok
    assert result.shim_missing == ("PathCchCombineEx",)
    assert result.violations[0].target == "shim!PathCchCombineEx"


def test_real_shim_covers_path_apiset(tmp_path: Path) -> None:
    """内置 shim 导出应覆盖 CPython 实际导入的 PathCch* 函数."""
    assert _ASSETS_SHIM.is_file(), "内置 shim 应随 fspack 分发"
    dll = _write(
        tmp_path,
        "python313.dll",
        _build_pe(
            {"api-ms-win-core-path-l1-1-0.dll": ["PathCchCanonicalizeEx", "PathCchCombineEx", "PathCchSkipRoot"]}
        ),
    )
    result = check_win7_imports(dll, shim=_ASSETS_SHIM)
    assert result.ok
    assert result.shim_missing == ()


def test_bundled_shim_win7_clean() -> None:
    """内置 shim 自身导入表应为 Win7 干净（回归实测结论）."""
    result = check_win7_imports(_ASSETS_SHIM)
    assert result.ok


# ---------------------------------------------------------------------------
# 解析器边界
# ---------------------------------------------------------------------------


def test_pe32_name_imports_parsed(tmp_path: Path) -> None:
    """PE32（32 位）镜像的按名导入同样解析并参与黑名单判定."""
    dll = _write(tmp_path, "python312.dll", _build_pe({"KERNEL32.dll": ["CopyFile2"]}, pe32plus=False, machine=0x14C))
    result = check_win7_imports(dll)
    assert [v.target for v in result.violations] == ["KERNEL32.dll!CopyFile2"]


def test_no_imports_no_exports(tmp_path: Path) -> None:
    """无导入无导出的镜像应通过."""
    dll = _write(tmp_path, "a.dll", _build_pe())
    result = check_win7_imports(dll)
    assert result.ok


def test_parse_errors(tmp_path: Path) -> None:
    """非 PE / 签名损坏 / 魔数损坏 / 截断 / RVA 越界均抛 PeParseError."""
    with pytest.raises(PeParseError, match="非 MZ"):
        check_win7_imports(_write(tmp_path, "bad1.dll", b"XX" * 128))
    with pytest.raises(PeParseError, match="非 MZ"):
        check_win7_imports(_write(tmp_path, "bad1b.dll", b""))
    corrupted = bytearray(_build_pe({"KERNEL32.dll": ["CopyFile2"]}))
    corrupted[0x40:0x44] = b"XXXX"
    with pytest.raises(PeParseError, match="PE 签名缺失"):
        check_win7_imports(_write(tmp_path, "bad2.dll", bytes(corrupted)))
    corrupted = bytearray(_build_pe({"KERNEL32.dll": ["CopyFile2"]}))
    corrupted[0x58:0x5A] = b"\x99\x99"
    with pytest.raises(PeParseError, match="魔数"):
        check_win7_imports(_write(tmp_path, "bad3.dll", bytes(corrupted)))
    with pytest.raises(PeParseError, match="截断"):
        check_win7_imports(_write(tmp_path, "bad4.dll", _build_pe({"KERNEL32.dll": ["CopyFile2"]})[:0x150]))
    bad_rva = _write(tmp_path, "bad5.dll", _build_pe({"KERNEL32.dll": ["x"]}, import_rva_override=0x900000))
    with pytest.raises(PeParseError, match="RVA 越界"):
        check_win7_imports(bad_rva)


def test_as_dict_json_serializable(tmp_path: Path) -> None:
    """as_dict 输出应可直接 JSON 序列化."""
    dll = _write(tmp_path, "a.dll", _build_pe({"KERNEL32.dll": ["CopyFile2"]}))
    payload: dict[str, Any] = check_win7_imports(dll).as_dict()
    assert payload["ok"] is False
    assert payload["violations"][0]["target"] == "KERNEL32.dll!CopyFile2"
    json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_text_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """文本模式逐文件报告，存在违规时退出码 1."""
    good = _write(tmp_path, "good.dll", _build_pe({"KERNEL32.dll": ["GetModuleHandleW"]}))
    bad = _write(tmp_path, "bad.dll", _build_pe({"KERNEL32.dll": ["CopyFile2"]}))
    rc = main([str(good), str(bad)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[通过] good.dll" in out
    assert "[失败] bad.dll" in out
    assert "CopyFile2" in out


def test_cli_json_with_shim(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--json + --shim 模式输出可解析结构，覆盖校验通过时退出码 0."""
    good = _write(tmp_path, "good.dll", _build_pe({"api-ms-win-core-path-l1-1-0.dll": ["PathCchCanonicalizeEx"]}))
    rc = main([str(good), "--shim", str(_ASSETS_SHIM), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["results"][0]["shim_dlls"] == ["api-ms-win-core-path-l1-1-0.dll"]


def test_cli_parse_failure_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """解析失败计入失败并输出原因，退出码 1."""
    bad = _write(tmp_path, "bad.dll", b"\x00" * 64)
    rc = main([str(bad)])
    assert rc == 1
    assert "解析失败" in capsys.readouterr().out
