"""产物机器类型校验：PE / ELF / Mach-O 的格式与目标架构一致性检查.

只读文件头，不加载整个产物（发行包可达数百 MB）。三种格式的识别均用
``struct`` 直接解析，不依赖平台工具链（``objdump``/``file``），因此可以在
任意构建机上校验任意平台的产物。

用途有两类：

- ``fsp verify <artifact> --target ... --arch ...`` 显式校验（CI 门禁）；
- 交叉构建排查：确认产物架构与目标一致，而不是「名字对了、内容是错的」。
"""

from __future__ import annotations

import platform as _host_platform
import struct
from dataclasses import dataclass
from pathlib import Path

from fspack.platform import Platform

__all__ = ["BinaryInfo", "detect_binary", "host_arch", "verify_artifact"]

# 只读文件头：PE 的 e_lfanew 与 Mach-O 的 fat 表都远在前 64KB 内
_HEAD_SIZE = 64 * 1024

_PE_MACHINES = {
    0x014C: "x86",
    0x8664: "x86_64",
    0x01C4: "armv7",
    0xAA64: "arm64",
}
_ELF_MACHINES = {
    3: "x86",
    40: "armv7",
    62: "x86_64",
    183: "arm64",
}
# cputype 为 32 位字段：x86_64/arm64 用 CPU_ARCH_ABI64 标志位 (0x01000000) 区分
_MACHO_CPUS = {
    7: "x86",
    12: "armv7",
    0x01000007: "x86_64",
    0x0100000C: "arm64",
}
# magic（文件字节）→ (字节序, 是否 64 位)
_MACHO_THIN_MAGICS = {
    b"\xfe\xed\xfa\xce": (">", False),
    b"\xce\xfa\xed\xfe": ("<", False),
    b"\xfe\xed\xfa\xcf": (">", True),
    b"\xcf\xfa\xed\xfe": ("<", True),
}
_MACHO_FAT_MAGICS = {
    b"\xca\xfe\xba\xbe": (">", False),
    b"\xbe\xba\xfe\xca": ("<", False),
    b"\xca\xfe\xba\xbf": (">", True),
    b"\xbf\xba\xfe\xca": ("<", True),
}

_EXPECTED_FORMAT = {
    Platform.WINDOWS: "pe",
    Platform.LINUX: "elf",
    Platform.MACOS: "macho",
}


@dataclass(frozen=True)
class BinaryInfo:
    """二进制文件头解析结果.

    Attributes:
        format: ``"pe"`` / ``"elf"`` / ``"macho"``
        archs: 归一化架构名（``x86_64``/``arm64``/``x86``/``armv7``）；未知机器码时为空
        raw_machines: 原始机器码（Mach-O fat 为多个），用于报错时给出原始值
    """

    format: str
    archs: tuple[str, ...]
    raw_machines: tuple[int, ...] = ()


def host_arch() -> str:
    """返回当前构建机的归一化架构名（``x86_64`` / ``arm64`` / 原样小写）."""
    machine = _host_platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    return machine


def detect_binary(path: Path | str) -> BinaryInfo | None:
    """识别文件格式与架构；不是 PE/ELF/Mach-O 时返回 ``None``."""
    path = Path(path)
    try:
        with path.open("rb") as f:
            data = f.read(_HEAD_SIZE)
    except OSError:
        return None

    return _detect_pe(data) or _detect_elf(data) or _detect_macho(data)


def verify_artifact(path: Path | str, target: Platform, arch: str | None = None) -> list[str]:
    """校验产物格式与架构是否匹配目标；返回问题列表（空列表表示通过）.

    Args:
        path: 待校验的产物文件
        target: 期望的目标平台（决定期望的二进制格式）
        arch: 期望架构，``None`` 时用当前主机架构
    """
    path = Path(path)
    if not path.is_file():
        return [f"文件不存在: {path}"]

    info = detect_binary(path)
    if info is None:
        return [f"无法识别二进制格式（既不是 PE/ELF/Mach-O）: {path}"]

    problems: list[str] = []
    expected_format = _EXPECTED_FORMAT[target]
    if info.format != expected_format:
        problems.append(
            f"格式不匹配: {path.name} 是 {info.format.upper()}，目标 {target.value} 要求 {expected_format.upper()}"
        )

    expected_arch = arch or host_arch()
    if expected_arch not in info.archs:
        actual = ", ".join(info.archs) or f"未知机器码 {list(info.raw_machines)}"
        problems.append(f"架构不匹配: {path.name} 是 {actual}，期望 {expected_arch}")

    return problems


def _detect_pe(data: bytes) -> BinaryInfo | None:
    """解析 PE：MZ → e_lfanew → ``PE\\0\\0`` → COFF machine."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    try:
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    except struct.error:  # pragma: no cover — len 已保证 >= 0x40
        return None
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        return None
    machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
    arch = _PE_MACHINES.get(machine)
    return BinaryInfo("pe", (arch,) if arch else (), (machine,))


def _detect_elf(data: bytes) -> BinaryInfo | None:
    """解析 ELF：magic → EI_DATA（端序）→ e_machine."""
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return None
    endian = "<" if data[5] == 1 else ">"
    machine = struct.unpack_from(f"{endian}H", data, 18)[0]
    arch = _ELF_MACHINES.get(machine)
    return BinaryInfo("elf", (arch,) if arch else (), (machine,))


def _detect_macho(data: bytes) -> BinaryInfo | None:
    """解析 Mach-O：thin 直接读 cputype，fat 遍历各 slice 的 cputype."""
    magic = data[:4]
    thin = _MACHO_THIN_MAGICS.get(magic)
    if thin is not None:
        endian, _is_64 = thin
        if len(data) < 8:
            return None
        machine = struct.unpack_from(f"{endian}I", data, 4)[0]
        arch = _MACHO_CPUS.get(machine)
        return BinaryInfo("macho", (arch,) if arch else (), (machine,))

    fat = _MACHO_FAT_MAGICS.get(magic)
    if fat is None:
        return None
    endian, is_64 = fat
    if len(data) < 8:
        return None
    count = struct.unpack_from(f"{endian}I", data, 4)[0]
    entry_size = 32 if is_64 else 20
    machines: list[int] = []
    for i in range(count):
        offset = 8 + i * entry_size
        if offset + 4 > len(data):
            break
        machines.append(struct.unpack_from(f"{endian}I", data, offset)[0])
    if not machines:
        return None
    archs = tuple(arch for machine in machines if (arch := _MACHO_CPUS.get(machine)))
    return BinaryInfo("macho", archs, tuple(machines))
