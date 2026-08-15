"""Win7 兼容性静态检查：PE 导入表 Win8+ API 黑名单校验.

背景（refs 两个 embed 发行版实测导入表结论）：

- Python 3.9–3.11：官方 python3XX.dll 仅额外静态导入 ``api-ms-win-core-path-l1-1-0.dll``
  （PathCch*），随包注入 shim 即可运行于 Win7 SP1；
- Python 3.12+：官方 python3XX.dll 还静态导入 kernel32 的 Win8+/Win8.1+ API
  （3.12 起 ``CopyFile2``/``Pss*``，3.13 起 ``GetSystemTimePreciseAsFileTime``）。
  kernel32 是 KnownDLL，无法用同名 DLL 遮蔽，loader 解析静态导入失败会直接拒绝
  加载，因此 shim 注入对 3.12+ 失效。可行方案是用 adang1345/PythonWin7 重编译版
  仅替换 python3XX.dll，其余组件保持官方原件（patch 版本须完全一致，ABI 才兼容）。

本模块即"只替换 python3XX.dll"方案的构建期门禁，判定维度：

- 函数级：kernel32/kernelbase 导入按 Win8+ 黑名单拦截；
- DLL 级：``api-ms-win-core-path-*`` 判定需随包 shim（可校验 shim 导出覆盖）；
  ``api-ms-win-crt-*`` 需系统 UCRT（Win7 SP1 需 KB2999226）；其余 ``api-ms-*``/
  ``ext-ms-*`` 未知 API Set 与 ``SHCORE.dll``/``combase.dll`` 判违规。

检查范围限定 loader 静态导入层；实体第三方 DLL（vcruntime/libcrypto 等）内部
实现不在范围内（由实测结论与随包分发策略保证）。

CLI 用法::

    python -m fspack.packaging.win7_check [--shim <shim.dll>] [--json] <python3XX.dll>...

退出码：0 通过（含"需 shim"且覆盖校验通过）；1 存在违规或解析失败。
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

__all__ = ["PeParseError", "Win7ApiViolation", "Win7CheckResult", "check_win7_imports", "main"]

# ---------------------------------------------------------------------------
# Win7 SP1 不可用、Win8/8.1+ 才有的 KERNEL32 导出（静态导入即阻塞加载）
# ---------------------------------------------------------------------------

_WIN8_KERNEL32_APIS: dict[str, str] = {
    "CopyFile2": "Win8+",
    "CreateFile2": "Win8+",
    "GetSystemTimePreciseAsFileTime": "Win8+",
    "WaitOnAddress": "Win8+",
    "WakeByAddressSingle": "Win8+",
    "WakeByAddressAll": "Win8+",
    "GetOverlappedResultEx": "Win8+",
    "GetProcessInformation": "Win8+",
    "SetProcessInformation": "Win8+",
    "PssCaptureSnapshot": "Win8.1+",
    "PssFreeSnapshot": "Win8.1+",
    "PssQuerySnapshot": "Win8.1+",
    "PssWalkSnapshot": "Win8.1+",
    "PssDuplicateSnapshot": "Win8.1+",
}

# 需按函数名黑名单校验的 KnownDLL（小写）
_FUNCTION_CHECKED_DLLS = frozenset({"kernel32.dll", "kernelbase.dll"})

# 整个 DLL 层面即 Win8+ 的系统库（小写全名）
_WIN8_SYSTEM_DLLS = frozenset({"shcore.dll", "combase.dll"})


class PeParseError(Exception):
    """PE 文件解析失败（非 PE 镜像、截断、RVA 越界等）。"""


@dataclass(frozen=True)
class Win7ApiViolation:
    """单条 Win7 兼容性违规.

    target 形如 ``KERNEL32.dll!CopyFile2``（DLL 级违规则为 DLL 名）；
    reason 为中文说明（含最低系统要求）。
    """

    target: str
    reason: str


@dataclass(frozen=True)
class Win7CheckResult:
    """单个 PE 文件的 Win7 兼容性检查结果.

    ok 为 True 表示 Win7 SP1 可加载（"需 shim/UCRT" 属运行前提，不算违规）。
    """

    path: Path
    violations: tuple[Win7ApiViolation, ...] = ()
    shim_dlls: tuple[str, ...] = ()
    shim_missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """是否通过检查（无任何违规）。"""
        return not self.violations

    def as_dict(self) -> dict[str, object]:
        """序列化为可 JSON 化的字典（CLI --json 输出用）。"""
        return {
            "path": str(self.path),
            "ok": self.ok,
            "violations": [{"target": v.target, "reason": v.reason} for v in self.violations],
            "shim_dlls": list(self.shim_dlls),
            "shim_missing": list(self.shim_missing),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# PE 解析（纯标准库，函数级导入表 + 导出表名列表）
# ---------------------------------------------------------------------------

_PE32_MAGIC = 0x10B
_PE32PLUS_MAGIC = 0x20B
_MAX_IMPORT_DLLS = 256
_MAX_THUNKS = 4096
_MAX_EXPORT_NAMES = 65536


@dataclass(frozen=True)
class _PeInfo:
    """PE 解析产物：{DLL 名: 导入函数名列表}（按名导入）与导出函数名元组."""

    imports: dict[str, list[str]]
    exports: tuple[str, ...]


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _read_cstr(data: bytes, offset: int) -> str:
    """从 offset 读取 NUL 结尾 ASCII 字符串（越界由 struct.error 兜底）."""
    end = data.find(b"\x00", offset)
    if end == -1:
        raise PeParseError("字符串无 NUL 终止符")
    return data[offset:end].decode("ascii", errors="replace")


def _parse_pe(data: bytes) -> _PeInfo:
    """解析 PE 导入表（函数级）与导出表（名字列表），失败抛 PeParseError."""
    try:
        return _parse_pe_inner(data)
    except (struct.error, IndexError) as exc:
        raise PeParseError(f"文件截断或格式错误: {exc}") from exc


def _parse_pe_inner(data: bytes) -> _PeInfo:
    if len(data) < 64 or data[:2] != b"MZ":
        raise PeParseError("非 MZ 文件")
    pe_offset = _u32(data, 0x3C)
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        raise PeParseError("PE 签名缺失")
    num_sections = _u16(data, pe_offset + 6)
    opt_size = _u16(data, pe_offset + 20)
    opt_offset = pe_offset + 24
    if opt_offset + opt_size > len(data):
        raise PeParseError("可选头越界")

    magic = _u16(data, opt_offset)
    if magic == _PE32_MAGIC:
        dd_offset, thunk_fmt, thunk_size, ord_flag = opt_offset + 96, "<I", 4, 1 << 31
    elif magic == _PE32PLUS_MAGIC:
        dd_offset, thunk_fmt, thunk_size, ord_flag = opt_offset + 112, "<Q", 8, 1 << 63
    else:
        raise PeParseError(f"未知 PE 可选头魔数: {magic:#x}")
    if dd_offset + 16 > len(data):
        raise PeParseError("数据目录越界")

    sections: list[tuple[int, int, int]] = []  # (virtual_address, size_of_raw, pointer_to_raw)
    sec_offset = opt_offset + opt_size
    for i in range(num_sections):
        base = sec_offset + i * 40
        va, raw_size, raw_ptr = _u32(data, base + 12), _u32(data, base + 16), _u32(data, base + 20)
        sections.append((va, raw_size, raw_ptr))

    def rva2off(rva: int) -> int:
        for va, raw_size, raw_ptr in sections:
            if va <= rva < va + raw_size:
                return rva - va + raw_ptr
        raise PeParseError(f"RVA 越界: {rva:#x}")

    export_rva = _u32(data, dd_offset)
    import_rva = _u32(data, dd_offset + 8)
    imports = _read_imports(data, rva2off, import_rva, thunk_fmt, thunk_size, ord_flag)
    exports = _read_exports(data, rva2off, export_rva) if export_rva else ()
    return _PeInfo(imports=imports, exports=exports)


def _read_imports(
    data: bytes,
    rva2off: Callable[[int], int],
    import_rva: int,
    thunk_fmt: str,
    thunk_size: int,
    ord_flag: int,
) -> dict[str, list[str]]:
    """遍历 IMAGE_IMPORT_DESCRIPTOR 数组，返回 {DLL 名: 导入函数名列表}.

    按序号导入记录为 ``#N``；import_rva 为 0 返回空表。
    """
    imports: dict[str, list[str]] = {}
    if not import_rva:
        return imports
    cursor = rva2off(import_rva)
    for _ in range(_MAX_IMPORT_DLLS):
        oft, _ts, _fc, name_rva, ft = struct.unpack_from("<IIIII", data, cursor)
        if oft == 0 and name_rva == 0 and ft == 0:
            break
        dll = _read_cstr(data, rva2off(name_rva))
        thunk_offset = rva2off(oft or ft)
        funcs: list[str] = []
        for _ in range(_MAX_THUNKS):
            (value,) = struct.unpack_from(thunk_fmt, data, thunk_offset)
            if value == 0:
                break
            if value & ord_flag:
                funcs.append(f"#{value & 0xFFFF}")
            else:
                funcs.append(_read_cstr(data, rva2off(value & 0x7FFFFFFF) + 2))
            thunk_offset += thunk_size
        imports[dll] = funcs
        cursor += 20
    return imports


def _read_exports(data: bytes, rva2off: Callable[[int], int], export_rva: int) -> tuple[str, ...]:
    """读取导出目录按名导出的函数名元组（shim 覆盖校验用）."""
    directory = rva2off(export_rva)
    _chars, _ts, _maj, _min, _name, _base, _nfuncs, num_names, _funcs, names_rva, _ords = struct.unpack_from(
        "<IIHHIIIIIII", data, directory
    )
    names_offset = rva2off(names_rva)
    names: list[str] = []
    for i in range(min(num_names, _MAX_EXPORT_NAMES)):
        name_rva = _u32(data, names_offset + i * 4)
        names.append(_read_cstr(data, rva2off(name_rva)))
    return tuple(names)


# ---------------------------------------------------------------------------
# Win7 兼容性判定
# ---------------------------------------------------------------------------


def check_win7_imports(path: Path, *, shim: Path | None = None) -> Win7CheckResult:
    """校验单个 PE 文件的静态导入表在 Win7 SP1 上可解析.

    Args:
        path: PE 文件（python3XX.dll/.pyd/.exe）路径。
        shim: 可选的 api-ms-win-core-path shim DLL 路径；提供时校验其导出
            覆盖所有被导入的 PathCch* 函数。

    Returns:
        Win7CheckResult：ok=True 表示 Win7 SP1 可加载；violations 列出
        无法通过随包组件解决的 Win8+ 静态导入。

    Raises:
        PeParseError: 文件不是合法 PE 镜像。
    """
    data = Path(path).read_bytes()
    info = _parse_pe(data)

    violations: list[Win7ApiViolation] = []
    notes: list[str] = []
    shim_dlls: list[str] = []
    path_set_funcs: list[str] = []

    for dll, funcs in info.imports.items():
        low = dll.lower()
        if low.startswith("api-ms-win-core-path-"):
            shim_dlls.append(dll)
            path_set_funcs.extend(f for f in funcs if not f.startswith("#"))
        elif low.startswith("api-ms-win-crt-"):
            notes.append(f"{dll}: 需系统 UCRT（Win7 SP1 需 KB2999226 或 VC 运行库）")
        elif low.startswith(("api-ms-", "ext-ms-")):
            violations.append(Win7ApiViolation(dll, "未知 API Set，Win7 可能缺失且无 shim"))
        elif low in _WIN8_SYSTEM_DLLS:
            violations.append(Win7ApiViolation(dll, "Win8+ 系统库，Win7 不存在"))
        elif low in _FUNCTION_CHECKED_DLLS:
            for func in sorted(set(funcs)):
                if func.startswith("#"):
                    notes.append(f"{dll}: 存在按序号导入 {func}，无法按名校验")
                elif func in _WIN8_KERNEL32_APIS:
                    level = _WIN8_KERNEL32_APIS[func]
                    violations.append(Win7ApiViolation(f"{dll}!{func}", f"{level} API，Win7 SP1 不存在"))

    shim_missing: tuple[str, ...] = ()
    if shim_dlls:
        notes.append("需随包分发 api-ms-win-core-path shim（fspack 已内置注入）")
        if shim is not None:
            shim_exports = _parse_pe(Path(shim).read_bytes()).exports
            shim_missing = tuple(sorted(set(path_set_funcs) - set(shim_exports)))
            violations.extend(
                Win7ApiViolation(f"shim!{func}", f"shim（{Path(shim).name}）缺少导出") for func in shim_missing
            )

    return Win7CheckResult(
        path=Path(path),
        violations=tuple(sorted(violations, key=lambda v: v.target)),
        shim_dlls=tuple(sorted(shim_dlls)),
        shim_missing=shim_missing,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def _render_report(result: Win7CheckResult) -> list[str]:
    """把单文件结果渲染为多行中文报告（CLI 文本模式用）."""
    status = "通过" if result.ok else "失败"
    lines = [f"[{status}] {result.path.name}"]
    for violation in result.violations:
        lines.append(f"  违规: {violation.target} — {violation.reason}")
    if result.shim_dlls:
        lines.append(f"  需 shim: {', '.join(result.shim_dlls)}")
        if result.shim_missing:
            lines.append(f"  shim 缺少导出: {', '.join(result.shim_missing)}")
    lines.extend(f"  提示: {note}" for note in result.notes)
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：校验若干 PE 文件导入表的 Win7 兼容性，返回进程退出码."""
    parser = argparse.ArgumentParser(
        prog="python -m fspack.packaging.win7_check",
        description="校验 PE 导入表不含 Win7 SP1 缺失的 Win8+ 静态导入（只替换 python3XX.dll 方案的门禁）",
    )
    parser.add_argument("files", nargs="+", type=Path, help="待校验的 PE 文件（python3XX.dll/.pyd/.exe）")
    parser.add_argument("--shim", type=Path, default=None, help="shim DLL 路径，用于校验 PathCch* 导出覆盖")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果（供管线集成）")
    args = parser.parse_args(argv)

    results: list[Win7CheckResult] = []
    failed = False
    for file in args.files:
        try:
            result = check_win7_imports(file, shim=args.shim)
        except PeParseError as exc:
            result = Win7CheckResult(file, violations=(Win7ApiViolation(str(file), f"解析失败: {exc}"),))
        results.append(result)
        failed = failed or not result.ok

    if args.json:
        payload = {"ok": not failed, "results": [r.as_dict() for r in results]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in results:
            for line in _render_report(result):
                print(line)
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
