"""Win7 shim_build 模块单元测试."""

from __future__ import annotations

import logging
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

    def test_registry_contains_two_compiled_shims(self) -> None:
        """注册表只含 synch / bcryptprimitives 两个编译型 shim（path 为二进制入库，
        kernel32-shim 已随导入表原地改名方案移除）。"""
        dll_names = {s.dll_name for s in shim_build.ALL_SHIMS}
        assert dll_names == {
            "api-ms-win-core-synch-l1-2-0.dll",
            "bcryptprimitives.dll",
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
        with mock.patch(
            "shutil.which",
            side_effect=lambda n: None if n == "x86_64-w64-mingw32-gcc" else (n if n == "gcc" else None),
        ), mock.patch.object(sys, "platform", "win32"):
            result = shim_build._find_mingw_gcc()
        assert result == "gcc"

    def test_returns_none_when_no_compiler(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            result = shim_build._find_mingw_gcc()
        assert result is None

    def test_windows_without_any_gcc_returns_none(self) -> None:
        """win32 平台且 gcc 也缺失时返回 None（非 win32 不回退无前缀 gcc）。"""
        with mock.patch("shutil.which", return_value=None), mock.patch.object(sys, "platform", "win32"):
            assert shim_build._find_mingw_gcc() is None

    def test_non_windows_never_falls_back_to_bare_gcc(self) -> None:
        """非 win32 平台即使系统存在 gcc 也不回退（交叉编译前缀必须存在）。"""
        with mock.patch(
            "shutil.which",
            side_effect=lambda n: n if n == "gcc" else None,
        ):
            assert shim_build._find_mingw_gcc() is None


def _make_spec(tmp_path: Path, *, dll: str = "test-shim.dll", src: str = "test-shim.c") -> shim_build.ShimSpec:
    """在临时 SHIM_SRC_DIR 下构造 spec，并创建源码文件。"""
    spec = shim_build.ShimSpec(src_name=src, dll_name=dll)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / src).write_text("int DllMain(void) { return 1; }\n", encoding="utf-8")
    return spec


class TestCompileOne:
    """_compile_one gcc 命令构造与失败路径。"""

    def test_success_builds_dll(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        spec = _make_spec(tmp_path)
        dll = tmp_path / spec.dll_name

        def fake_run(cmd: list[str], **kwargs: object) -> mock.Mock:
            # gcc 成功后产物落盘
            dll.write_bytes(b"MZ")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path), mock.patch(
            "fspack.packaging.win7.shim_build.subprocess.run", side_effect=fake_run
        ) as run_mock, caplog.at_level(logging.INFO):
            result = shim_build._compile_one(spec, "x86_64-w64-mingw32-gcc", work_dir=tmp_path)
        assert result == dll
        cmd = run_mock.call_args.args[0]
        # 命令结构：gcc -shared -O2 -o <dll> <src> -lkernel32 [-nostdlib -e DllMain]
        assert cmd[:4] == ["x86_64-w64-mingw32-gcc", "-shared", "-O2", "-o"]
        assert str(dll) in cmd
        assert str(spec.src_path) in cmd
        assert "-lkernel32" in cmd
        assert "-nostdlib" in cmd
        assert "编译 shim" in caplog.text

    def test_success_includes_def_and_extra_libs(self, tmp_path: Path) -> None:
        spec = shim_build.ShimSpec(
            src_name="a.c",
            dll_name="a.dll",
            libs=("bcrypt",),
            cflags=("-nostdlib",),
            def_name="a.def",
        )
        (tmp_path / "a.c").write_text("x", encoding="utf-8")
        (tmp_path / "a.def").write_text("EXPORTS\n  f\n", encoding="utf-8")
        dll = tmp_path / "a.dll"

        def fake_run(cmd: list[str], **kwargs: object) -> mock.Mock:
            dll.write_bytes(b"MZ")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path), mock.patch(
            "fspack.packaging.win7.shim_build.subprocess.run", side_effect=fake_run
        ) as run_mock:
            shim_build._compile_one(spec, "gcc", work_dir=tmp_path)
        # .def 与 -lbcrypt 均进入命令行
        cmd = run_mock.call_args.args[0]
        assert str(tmp_path / "a.def") in cmd
        assert "-lbcrypt" in cmd

    def test_gcc_failure_raises_with_details(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        failing = mock.Mock(returncode=2, stdout="out-line", stderr="err-line")
        with (
            mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path),
            mock.patch("fspack.packaging.win7.shim_build.subprocess.run", return_value=failing),
            pytest.raises(shim_build.ShimBuildError) as exc_info,
        ):
            shim_build._compile_one(spec, "gcc", work_dir=tmp_path)
        msg = str(exc_info.value)
        assert "编译失败" in msg
        assert "exit 2" in msg
        assert "err-line" in msg
        assert "out-line" in msg

    def test_missing_def_file_raises(self, tmp_path: Path) -> None:
        spec = shim_build.ShimSpec(src_name="a.c", dll_name="a.dll", def_name="missing.def")
        (tmp_path / "a.c").write_text("x", encoding="utf-8")
        with (
            mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path),
            pytest.raises(shim_build.ShimBuildError, match=r"\.def 文件缺失"),
        ):
            shim_build._compile_one(spec, "gcc", work_dir=tmp_path)

    def test_missing_output_dll_raises(self, tmp_path: Path) -> None:
        """gcc 返回 0 但产物未落盘 → 报产物缺失。"""
        spec = _make_spec(tmp_path)
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path),
            mock.patch("fspack.packaging.win7.shim_build.subprocess.run", return_value=ok),
            pytest.raises(shim_build.ShimBuildError, match="产物缺失"),
        ):
            shim_build._compile_one(spec, "gcc", work_dir=tmp_path)


class TestBuildShim:
    """build_shim 存在跳过 / 源码缺失 / 工具链缺失 / 正常编译四条路径。"""

    def test_existing_dll_skipped_without_force(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        dll = tmp_path / spec.dll_name
        dll.write_bytes(b"MZ")
        with mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path):
            assert shim_build.build_shim(spec, force=False) is None

    def test_source_missing_raises(self, tmp_path: Path) -> None:
        spec = shim_build.ShimSpec(src_name="no-such-src.c", dll_name="ghost.dll")
        with mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path):
            with pytest.raises(shim_build.ShimBuildError, match="源码缺失"):
                shim_build.build_shim(spec, force=True)

    def test_no_compiler_non_force_warns_and_skips(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        spec = _make_spec(tmp_path)
        with (
            mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path),
            mock.patch.object(shim_build, "_find_mingw_gcc", return_value=None),
            caplog.at_level(logging.WARNING),
        ):
            assert shim_build.build_shim(spec, force=False) is None
        assert "未找到 mingw gcc" in caplog.text

    def test_no_compiler_force_raises(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        with (
            mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path),
            mock.patch.object(shim_build, "_find_mingw_gcc", return_value=None),
            pytest.raises(shim_build.ShimBuildError, match="未找到 mingw gcc"),
        ):
            shim_build.build_shim(spec, force=True)

    def test_with_compiler_invokes_compile(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        compiled = tmp_path / spec.dll_name
        with (
            mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path),
            mock.patch.object(shim_build, "_find_mingw_gcc", return_value="x86_64-w64-mingw32-gcc"),
            mock.patch.object(shim_build, "_compile_one", return_value=compiled) as compile_mock,
        ):
            assert shim_build.build_shim(spec, force=False) == compiled
        compile_mock.assert_called_once()
        assert compile_mock.call_args.kwargs["work_dir"] == tmp_path


class TestBuildAllShims:
    """build_all_shims 汇总与失败处理。"""

    def test_collects_results_per_spec(self, tmp_path: Path) -> None:
        ok_spec = _make_spec(tmp_path, dll="ok.dll", src="ok.c")
        (tmp_path / "ok.dll").write_bytes(b"MZ")
        fail_spec = shim_build.ShimSpec(src_name="broken.c", dll_name="broken.dll")
        with mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path):
            result = shim_build.build_all_shims([ok_spec, fail_spec], force=False)
        assert result["ok.dll"] == tmp_path / "ok.dll"
        assert result["broken.dll"] is None  # 源码缺失非 force → None 不抛

    def test_force_failure_propagates(self, tmp_path: Path) -> None:
        spec = shim_build.ShimSpec(src_name="no.c", dll_name="x.dll")
        with mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path):
            with pytest.raises(shim_build.ShimBuildError):
                shim_build.build_all_shims([spec], force=True)

    def test_default_uses_full_registry(self, tmp_path: Path) -> None:
        """specs=None 时使用注册表全部 shim（仓库内均已存在 → 全 None）。"""
        result = shim_build.build_all_shims()
        assert set(result) == {s.dll_name for s in shim_build.ALL_SHIMS}


class TestNamedSpecs:
    """_named_specs 过滤逻辑（逗号分隔 / 大小写 / 子串 / 空值）。"""

    def test_empty_names_returns_all(self) -> None:
        assert shim_build._named_specs([""]) == list(shim_build.ALL_SHIMS)

    def test_comma_and_case_insensitive_substring(self) -> None:
        specs = shim_build._named_specs(["SYNC", "bcrypt"])
        names = {s.dll_name for s in specs}
        assert names == {"api-ms-win-core-synch-l1-2-0.dll", "bcryptprimitives.dll"}

    def test_no_match_returns_empty_list(self) -> None:
        assert shim_build._named_specs(["kernel32"]) == []  # kernel32 shim 已移除

    def test_matches_by_src_name(self) -> None:
        specs = shim_build._named_specs(["bcryptprimitives.c"])
        assert [s.dll_name for s in specs] == ["bcryptprimitives.dll"]


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
        with mock.patch("fspack.packaging.win7.shim_build._find_mingw_gcc", return_value=None), mock.patch.object(
            shim_build, "SHIM_SRC_DIR", tmp_path
        ):
            shim_build._ALL_SHIMS = (test_spec,)
            result = shim_build.ensure_all_shims()
        # 还原注册表（避免污染后续用例）
        shim_build._ALL_SHIMS = shim_build.ALL_SHIMS
        assert result.get("missing.dll") is None

    def test_missing_dll_build_failure_warns_and_continues(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """兜底编译抛错（源码缺失）→ warning 后返回 None，不中断。"""
        test_spec = shim_build.ShimSpec(src_name="gone.c", dll_name="gone.dll")
        with mock.patch.object(shim_build, "SHIM_SRC_DIR", tmp_path):
            shim_build._ALL_SHIMS = (test_spec,)
            with caplog.at_level(logging.WARNING):
                result = shim_build.ensure_all_shims()
        shim_build._ALL_SHIMS = shim_build.ALL_SHIMS
        assert result.get("gone.dll") is None
        assert "编译失败" in caplog.text


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

    def test_main_reports_ok_with_size(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        dll = tmp_path / "built.dll"
        dll.write_bytes(b"MZ-shim")
        with mock.patch.object(shim_build, "build_all_shims", return_value={"built.dll": dll}):
            rc = shim_build.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[ok] built.dll" in out
        assert "7 bytes" in out

    def test_main_force_reports_plain_skip(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(shim_build, "build_all_shims", return_value={"kept.dll": None}):
            rc = shim_build.main(["--force"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[skip] kept.dll 已存在" in out
        assert "--force 可重建" not in out

    def test_main_build_failure_returns_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = shim_build.ShimBuildError("gcc 崩溃")
        with mock.patch.object(shim_build, "build_all_shims", side_effect=err):
            rc = shim_build.main(["--force"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "[FAIL] gcc 崩溃" in captured.err
