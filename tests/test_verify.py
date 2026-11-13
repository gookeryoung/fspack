"""产物二进制格式/架构校验测试（PE / ELF / Mach-O）."""

from __future__ import annotations

import platform as host_platform
import struct
from collections.abc import Callable
from pathlib import Path

import pytest

import fspack.packaging.verify as verify_mod
from fspack import cli
from fspack.packaging.verify import BinaryInfo, detect_binary, host_arch, verify_artifact
from fspack.platform import Platform

PE_X86_64 = 0x8664
PE_ARM64 = 0xAA64
PE_I386 = 0x014C
PE_UNKNOWN = 0x9999

ELF_X86_64 = 62
ELF_ARM64 = 183
ELF_I386 = 3
ELF_UNKNOWN = 0xFFFF

MACHO_X86_64 = 0x01000007
MACHO_ARM64 = 0x0100000C
MACHO_UNKNOWN = 0x12345678


def _write_pe(path: Path, machine: int) -> Path:
    data = bytearray(0x100)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", data, 0x84, machine)
    path.write_bytes(data)
    return path


def _write_elf(path: Path, machine: int, *, little: bool = True) -> Path:
    data = bytearray(64)
    data[0:4] = b"\x7fELF"
    data[4] = 2  # 64 位
    data[5] = 1 if little else 2
    struct.pack_into("<H" if little else ">H", data, 18, machine)
    path.write_bytes(data)
    return path


def _write_macho(path: Path, cputype: int, *, is_64: bool = True) -> Path:
    data = bytearray(32)
    data[0:4] = b"\xcf\xfa\xed\xfe" if is_64 else b"\xce\xfa\xed\xfe"
    struct.pack_into("<I", data, 4, cputype)
    path.write_bytes(data)
    return path


def _write_macho_fat(path: Path, cputypes: tuple[int, ...]) -> Path:
    header = struct.pack(">II", 0xCAFEBABE, len(cputypes))
    entries = b"".join(struct.pack(">IIIII", cpu, 0, 0, 0, 0) for cpu in cputypes)
    path.write_bytes(header + entries)
    return path


@pytest.mark.parametrize(
    ("machine", "arch"),
    [(PE_X86_64, "x86_64"), (PE_ARM64, "arm64"), (PE_I386, "x86"), (PE_UNKNOWN, None)],
)
def test_detect_pe(tmp_path: Path, machine: int, arch: str | None) -> None:
    info = detect_binary(_write_pe(tmp_path / "app.exe", machine))
    assert info is not None
    assert info.format == "pe"
    assert info.raw_machines == (machine,)
    assert info.archs == ((arch,) if arch else ())


@pytest.mark.parametrize(
    ("machine", "little", "arch"),
    [
        (ELF_X86_64, True, "x86_64"),
        (ELF_ARM64, True, "arm64"),
        (ELF_I386, True, "x86"),
        (ELF_ARM64, False, "arm64"),
        (ELF_UNKNOWN, True, None),
    ],
)
def test_detect_elf(tmp_path: Path, machine: int, little: bool, arch: str | None) -> None:
    info = detect_binary(_write_elf(tmp_path / "app", machine, little=little))
    assert info is not None
    assert info.format == "elf"
    assert info.archs == ((arch,) if arch else ())


@pytest.mark.parametrize(
    ("cputype", "is_64", "arch"),
    [
        (MACHO_X86_64, True, "x86_64"),
        (MACHO_ARM64, True, "arm64"),
        (7, False, "x86"),
        (MACHO_UNKNOWN, True, None),
    ],
)
def test_detect_macho_thin(tmp_path: Path, cputype: int, is_64: bool, arch: str | None) -> None:
    info = detect_binary(_write_macho(tmp_path / "app", cputype, is_64=is_64))
    assert info is not None
    assert info.format == "macho"
    assert info.archs == ((arch,) if arch else ())


def test_detect_macho_fat(tmp_path: Path) -> None:
    info = detect_binary(_write_macho_fat(tmp_path / "app", (MACHO_X86_64, MACHO_ARM64)))
    assert info == BinaryInfo("macho", ("x86_64", "arm64"), (MACHO_X86_64, MACHO_ARM64))


def test_detect_macho_fat_truncated(tmp_path: Path) -> None:
    """fat 头声明 2 个 slice 但文件只够 1 个：解析已读到的 slice，不越界."""
    header = struct.pack(">II", 0xCAFEBABE, 2)
    entry = struct.pack(">IIIII", MACHO_ARM64, 0, 0, 0, 0)
    path = tmp_path / "app"
    path.write_bytes(header + entry)
    info = detect_binary(path)
    assert info is not None
    assert info.archs == ("arm64",)


def test_detect_macho_fat_no_slices(tmp_path: Path) -> None:
    """fat 头声明 1 个 slice 但没有任何 slice 条目：解析失败."""
    path = tmp_path / "app"
    path.write_bytes(struct.pack(">II", 0xCAFEBABE, 1))
    assert detect_binary(path) is None


def test_detect_macho_fat_unknown_machine(tmp_path: Path) -> None:
    """fat 中未知 cputype 被过滤，只保留已知架构."""
    info = detect_binary(_write_macho_fat(tmp_path / "app", (MACHO_UNKNOWN, MACHO_ARM64)))
    assert info is not None
    assert info.archs == ("arm64",)


def test_detect_binary_not_executable(tmp_path: Path) -> None:
    path = tmp_path / "readme.txt"
    path.write_text("hello", encoding="utf-8")
    assert detect_binary(path) is None


def test_detect_binary_truncated_headers(tmp_path: Path) -> None:
    pe = tmp_path / "a.exe"
    pe.write_bytes(b"MZ")
    elf = tmp_path / "b"
    elf.write_bytes(b"\x7fELF")
    macho = tmp_path / "c"
    macho.write_bytes(b"\xcf\xfa\xed\xfe\x00")
    fat = tmp_path / "d"
    fat.write_bytes(b"\xca\xfe\xba\xbe")
    assert detect_binary(pe) is None
    assert detect_binary(elf) is None
    assert detect_binary(macho) is None
    assert detect_binary(fat) is None


@pytest.mark.parametrize("pe_offset", [0xF0, 0x80])
def test_detect_pe_invalid_header(tmp_path: Path, pe_offset: int) -> None:
    """e_lfanew 越界或签名不是 PE\\0\\0 时识别失败（两种情况都覆盖）."""
    data = bytearray(0x100)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_offset)
    if pe_offset == 0x80:
        data[0x80:0x84] = b"PX\x00\x00"
    path = tmp_path / "app.exe"
    path.write_bytes(data)
    assert detect_binary(path) is None


def test_detect_binary_missing_file(tmp_path: Path) -> None:
    assert detect_binary(tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("writer", "machine", "target", "arch"),
    [
        (_write_pe, PE_X86_64, Platform.WINDOWS, "x86_64"),
        (_write_pe, PE_ARM64, Platform.WINDOWS, "arm64"),
        (_write_elf, ELF_X86_64, Platform.LINUX, "x86_64"),
        (_write_elf, ELF_ARM64, Platform.LINUX, "arm64"),
        (_write_macho, MACHO_X86_64, Platform.MACOS, "x86_64"),
        (_write_macho, MACHO_ARM64, Platform.MACOS, "arm64"),
    ],
)
def test_verify_artifact_passes(
    tmp_path: Path, writer: Callable[[Path, int], Path], machine: int, target: Platform, arch: str
) -> None:
    path = writer(tmp_path / "app", machine)
    assert verify_artifact(path, target, arch) == []


def test_verify_artifact_wrong_format(tmp_path: Path) -> None:
    path = _write_pe(tmp_path / "app.exe", PE_X86_64)
    problems = verify_artifact(path, Platform.LINUX, "x86_64")
    assert len(problems) == 1
    assert "格式不匹配" in problems[0]
    assert "PE" in problems[0] and "ELF" in problems[0]


def test_verify_artifact_wrong_arch(tmp_path: Path) -> None:
    path = _write_macho(tmp_path / "app", MACHO_X86_64)
    problems = verify_artifact(path, Platform.MACOS, "arm64")
    assert len(problems) == 1
    assert "架构不匹配" in problems[0]
    assert "x86_64" in problems[0]


def test_verify_artifact_unknown_machine_code(tmp_path: Path) -> None:
    path = _write_elf(tmp_path / "app", ELF_UNKNOWN)
    problems = verify_artifact(path, Platform.LINUX, "x86_64")
    assert len(problems) == 1
    assert str(ELF_UNKNOWN) in problems[0]


def test_verify_artifact_missing_file(tmp_path: Path) -> None:
    problems = verify_artifact(tmp_path / "missing", Platform.WINDOWS, "x86_64")
    assert problems == [f"文件不存在: {tmp_path / 'missing'}"]


def test_verify_artifact_not_binary(tmp_path: Path) -> None:
    path = tmp_path / "app"
    path.write_text("not a binary", encoding="utf-8")
    problems = verify_artifact(path, Platform.LINUX, "x86_64")
    assert len(problems) == 1
    assert "无法识别" in problems[0]


def test_host_arch_normalized() -> None:
    machine = host_platform.machine().lower()
    expected = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine, machine)
    assert host_arch() == expected


@pytest.mark.parametrize(("machine", "expected"), [("aarch64", "arm64"), ("AMD64", "x86_64"), ("riscv64", "riscv64")])
def test_host_arch_mapping(monkeypatch: pytest.MonkeyPatch, machine: str, expected: str) -> None:
    monkeypatch.setattr(verify_mod._host_platform, "machine", lambda: machine)
    assert host_arch() == expected


def test_cli_verify_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_elf(tmp_path / "app", ELF_X86_64)
    cli.main(["verify", str(path), "--target", "linux", "--arch", "x86_64"])
    assert "ELF" in capsys.readouterr().err


def test_cli_verify_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_pe(tmp_path / "app.exe", PE_X86_64)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["verify", str(path), "--target", "linux", "--arch", "x86_64"])
    assert excinfo.value.code == 1
    assert "格式不匹配" in capsys.readouterr().err
