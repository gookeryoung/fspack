"""Win7 shim_build 模块单元测试."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

from fspack.packaging.win7 import shim_build


class TestShimSpec:
    """ShimSpec dataclass 行为。"""

    def test_paths_relative_to_SHIM_SRC_DIR(self) -> None:
        spec = shim_build.ALL_SHIMS[0]
        assert spec.src_path.name == spec.src_name
        assert spec.dll_path.name == spec.dll_name
        assert spec.src_path.parent == shim_build.SHIM_SRC_DIR
        assert spec.def_path is None  # 当前 shim 未用 .def


class TestALLSHIMS:
    """shim 注册表与实际 assets/runtime 目录一致。"""

    def test_all_registered_dlls_present_in_assets(self) -> None:
        for spec in shim_build.ALL_SHIMS:
            assert spec.dll_path.is_file(), f"{spec.dll_name} 二进制缺失"
            assert spec.src_path.is_file(), f"{spec.src_name} 源码缺失"

    def test_registry_contains_three_shims(self) -> None:
        dll_names = {s.dll_name for s in shim_build.ALL_SHIMS}
        assert dll_names == {
            "api-ms-win-core-synch-l1-2-0.dll",
            "bcryptprimitives.dll",
            "api-ms-win-core-kernel32-shim.dll",
        }


class TestIsShimMissing:
    """is_shim_missing 对存在/缺失 DLL 的判定。"""

    def test_existing_dll(self) -> None:
        # synch DLL 已入库
        assert shim_build.is_shim_missing("api-ms-win-core-synch-l1-2-0.dll") is False

    def test_nonexistent_dll(self) -> None:
        assert shim_build.is_shim_missing("does-not-exist.dll") is True


class TestFindMingwGcc:
    """编译器发现逻辑。"""

    def test_falls_back_on_windows_native_gcc(self) -> None:
        # 测试当 mingw 前缀不存在但 host gcc 存在时，Windows 上回退 gcc
        with (
            mock.patch(
                "shutil.which",
                side_effect=lambda n: None if n == "x86_64-w64-mingw32-gcc" else (n if n == "gcc" else None),
            ),
            mock.patch.object(sys, "platform", "win32"),
        ):
            result = shim_build._find_mingw_gcc()
        assert result == "gcc"

    def test_returns_none_when_no_compiler(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            result = shim_build._find_mingw_gcc()
        assert result is None


class TestEnsureAllShims:
    """ensure_all_shims 保证二进制就绪——正常场景已存在，编译器缺失时 warning."""

    def test_all_already_present(self) -> None:
        result = shim_build.ensure_all_shims()
        # 正常 clone 仓库所有 shim 已存在，返回值全部 None（无编译）
        assert len(result) >= 2

    def test_missing_dll_no_compiler(self, tmp_path: Path) -> None:
        """编译器缺失 + 源码/二进制均缺失 → 返回 None 但不抛错."""
        # 不碰仓库真实 DLL：构造全 fake 的注册表指向临时目录
        test_spec = shim_build.ShimSpec(
            src_name="not-a-real-source.c",
            dll_name="missing.dll",
        )
        with (
            mock.patch("fspack.packaging.win7.shim_build._find_mingw_gcc", return_value=None),
            mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path),
        ):
            shim_build._ALL_SHIMS = (test_spec,)
            result = shim_build.ensure_all_shims()
        # 还原注册表（避免污染后续用例）
        shim_build._ALL_SHIMS = shim_build.ALL_SHIMS
        assert result.get("missing.dll") is None


class TestCLI:
    """python -m fspack.packaging.win7.shim_build CLI."""

    def test_main_without_force_skips_existing(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 不带 --force：dll 已存在，全部 skip
        rc = shim_build.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[skip]" in out
        # 不应出现 [ok]（没 force 没编译）
        assert "[ok]" not in out

    def test_main_nonexistent_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = shim_build.main(["--only", "does-not-exist"])
        assert rc == 1
        assert "没有匹配" in capsys.readouterr().out
