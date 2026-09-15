"""Win7 patch 模块单元测试 — PE 导入表重定向."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fspack.packaging.win7 import patch

# ---------------------------------------------------------------------------
# 常量 & 数据结构
# ---------------------------------------------------------------------------


class TestConstants:
    """REDIRECTED_IMPORTS 注册表和常量."""

    def test_shim_dll_name(self) -> None:
        assert patch.SHIM_DLL_NAME == "api-ms-win-core-kernel32-shim.dll"

    def test_redirected_imports_not_empty(self) -> None:
        assert len(patch.REDIRECTED_IMPORTS) >= 1
        any_win8_api = any("Precise" in si.func_name for si in patch.REDIRECTED_IMPORTS)
        assert any_win8_api

    def test_shim_import_reason_present(self) -> None:
        for si in patch.REDIRECTED_IMPORTS:
            assert si.reason, f"{si.func_name} 缺少重定向原因"


# ---------------------------------------------------------------------------
# patch_file 端到端（使用真实 PE 文件）
# ---------------------------------------------------------------------------


_PYDANTIC_CORE_CANDIDATES = [
    Path(
        r"C:\Users\zhou\AppData\Roaming\Python\Python313\site-packages\pydantic_core\_pydantic_core.cp313-win_amd64.pyd"
    ),
    Path(r"C:\Users\zhou\AppData\Roaming\Python\Python313\site-packages\kreuzberg\_internal_bindings.pyd"),
]


def _find_real_target() -> Path | None:
    """在常见 site-packages 里找一个导入了 GetSystemTimePreciseAsFileTime 的真实 PE."""
    for p in _PYDANTIC_CORE_CANDIDATES:
        if p.is_file():
            return p
    # 回退：扫描 .venv
    import pefile

    venv = Path(__file__).resolve().parent.parent / ".venv" / "Lib" / "site-packages"
    if not venv.is_dir():
        return None
    for pyd in venv.rglob("*.pyd"):
        try:
            pe = pefile.PE(str(pyd), fast_load=True)
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
            if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
                for entry in pe.DIRECTORY_ENTRY_IMPORT:
                    for imp in entry.imports:
                        if imp.name and b"Precise" in imp.name:
                            return pyd
        except Exception:
            continue
    return None


class TestPatchFile:
    """patch_file 真实 PE 改写测试."""

    def test_patch_real_pyd_redirects_import(self, tmp_path: Path) -> None:
        """用真实 pydantic_core.pyd 测试 kernel32!GetSystemTimePreciseAsFileTime → shim 重定向."""
        target = _find_real_target()
        if target is None:
            pytest.skip("本地未找到导入了 GetSystemTimePreciseAsFileTime 的真实 PE 文件")

        # 复制到 tmp_path，避免污染 site-packages
        work = tmp_path / "target.pyd"
        shutil.copy2(target, work)

        result = patch.patch_file(work, output=work)
        assert result.redirected, "应该捕获到至少一条重定向"
        assert any(src_func == "GetSystemTimePreciseAsFileTime" for _, src_func, _ in result.redirected)
        assert result.shim_dll_added is True

        # 验证改写后的 PE：kernel32 不再有 Precise，shim DLL 有 Precise
        import pefile

        patched = pefile.PE(str(work))
        patched.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        assert hasattr(patched, "DIRECTORY_ENTRY_IMPORT"), "改写后 PE 应该仍有导入目录"

        kernel32_funcs: set[str] = set()
        shim_funcs: set[str] = set()
        for entry in patched.DIRECTORY_ENTRY_IMPORT:
            dll = entry.dll.decode().upper()
            for imp in entry.imports:
                name = imp.name.decode() if imp.name else f"ord:{imp.ordinal}"
                # KERNEL32.DLL 精确匹配——不能用 "KERNEL32" in dll，否则会把
                # API-MS-WIN-CORE-KERNEL32-SHIM.DLL 也捞进来。
                if dll == "KERNEL32.DLL":
                    kernel32_funcs.add(name)
                elif "SHIM" in dll:
                    shim_funcs.add(name)

        assert "GetSystemTimePreciseAsFileTime" not in kernel32_funcs, (
            "kernel32 导入表不应该再有 GetSystemTimePreciseAsFileTime"
        )
        assert "GetSystemTimePreciseAsFileTime" in shim_funcs, (
            "shim DLL 应该出现在导入表中并导出 GetSystemTimePreciseAsFileTime"
        )

    def test_patch_overwrite_in_place(self, tmp_path: Path) -> None:
        """不传 output → 原地覆盖."""
        target = _find_real_target()
        if target is None:
            pytest.skip("无真实 target 文件")

        work = tmp_path / "target.pyd"
        shutil.copy2(target, work)
        size_before = work.stat().st_size

        result = patch.patch_file(work)
        assert result.output_path == work

        # 文件可能变大（追加路径）或不变（就地覆盖），但必须 >= 原始大小
        assert work.stat().st_size >= size_before - 16  # 允许少量缩小（padding 差异）

    def test_patch_writes_to_different_output(self, tmp_path: Path) -> None:
        """output 参数指定不同路径 → 原文件不变."""
        target = _find_real_target()
        if target is None:
            pytest.skip("无真实 target 文件")

        work = tmp_path / "original.pyd"
        out = tmp_path / "patched.pyd"
        shutil.copy2(target, work)
        size_before = work.stat().st_size

        patch.patch_file(work, output=out)

        assert out.is_file(), "output 指定路径应该生成文件"
        assert work.stat().st_size == size_before, "原文件不应该被修改"

    def test_patch_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(patch.PEPatchError, match="目标文件不存在"):
            patch.patch_file(tmp_path / "does-not-exist.pyd")

    def test_patch_no_match_skips(self, tmp_path: Path) -> None:
        """无匹配 Win8+ API 的文件 → redirected 为空，不写回."""

        # 创建一个假的 PE（实际上就是复制一个不包含 Precise 的 DLL）
        # 这里用我们自己的 shim DLL —— 它只从 kernel32 导入 Win7 原生 API
        shim_dll = Path(__file__).resolve().parent.parent / "src" / "fspack" / "assets" / "runtime"
        candidate = shim_dll / "api-ms-win-core-kernel32-shim.dll"
        if not candidate.is_file():
            pytest.skip("shim DLL 不存在，无法构造无匹配场景")

        work = tmp_path / "shim.pyd"
        shutil.copy2(candidate, work)
        size_before = work.stat().st_size

        result = patch.patch_file(work)
        assert result.redirected == []
        assert result.shim_dll_added is False

        # 验证文件没被修改
        assert work.stat().st_size == size_before


# ---------------------------------------------------------------------------
# build_patched_pe 内部函数（需要 mock pefile）
# ---------------------------------------------------------------------------


class TestBuildPatchedPeEdgeCases:
    """build_patched_pe 边界条件."""

    def test_no_imports_raises(self) -> None:
        """PE 没有导入目录 → PEPatchError."""
        import pefile

        # 用我们的 shim DLL（有导入）复制到临时路径，然后 mock 它
        shim_dll = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "fspack"
            / "assets"
            / "runtime"
            / "api-ms-win-core-kernel32-shim.dll"
        )
        if not shim_dll.is_file():
            pytest.skip("shim DLL 不存在")

        pe = pefile.PE(str(shim_dll))
        # 人为移除 DIRECTORY_ENTRY_IMPORT 属性
        if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
            delattr(pe, "DIRECTORY_ENTRY_IMPORT")

        with pytest.raises(patch.PEPatchError, match="目标文件无导入"):
            patch.build_patched_pe(pe)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


class TestCLI:
    """python -m fspack.packaging.win7.patch CLI."""

    def test_cli_no_match_exit_code_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """无匹配 → 退出码 1 + skip 提示."""
        shim_dll = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "fspack"
            / "assets"
            / "runtime"
            / "api-ms-win-core-kernel32-shim.dll"
        )
        if not shim_dll.is_file():
            pytest.skip("shim DLL 不存在")

        work = tmp_path / "target.dll"
        shutil.copy2(shim_dll, work)

        rc = patch.main([str(work)])
        assert rc == 1
        assert "skip" in capsys.readouterr().out

    def test_cli_missing_file_exit_code_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = patch.main([str(tmp_path / "nope.pyd")])
        assert rc == 2
        assert "FAIL" in capsys.readouterr().err

    def test_cli_real_pyd_exit_code_0(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """真实 pydantic_core.pyd patch → 退出码 0 + ok 提示."""
        target = _find_real_target()
        if target is None:
            pytest.skip("无真实 target 文件")

        work = tmp_path / "target.pyd"
        shutil.copy2(target, work)

        rc = patch.main([str(work)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ok" in out
        assert "GetSystemTimePreciseAsFileTime" in out
