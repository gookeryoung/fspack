"""win7_scan 模块测试：dist 全量扫描聚合、报告渲染与 loader exe 硬门禁.

合成 PE 复用 test_win7_check 的 ``_build_pe`` 构造器（tests 为包，跨模块导入）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.win7.scan import (
    _SCAN_SUFFIXES,
    Win7ScanError,
    enforce_win7_loaders,
    iter_pe_files,
    render_win7_report,
    scan_dist_win7,
    write_win7_report,
)
from tests.test_win7_check import _build_pe

# 内置 shim 实际导出的 PathCch* 函数（test_win7_check 已实测覆盖）
_SHIM_EXPORTED = "PathCchSkipRoot"

_OK_IMPORTS: dict[str, list[str]] = {"KERNEL32.dll": ["GetModuleHandleW", "CreateFileW"]}
_BAD_IMPORTS: dict[str, list[str]] = {"KERNEL32.dll": ["CopyFile2"]}


def _write_pe(path: Path, imports: dict[str, list[str]] | None = None) -> Path:
    """在 path 写入合成 PE（默认 Win7 干净导入表），父目录自动创建."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_build_pe(imports if imports is not None else _OK_IMPORTS))
    return path


# ---------------------------------------------------------------------------
# iter_pe_files：dist 遍历语义（os.scandir 快路径须与 Path.rglob 结果完全一致）
# ---------------------------------------------------------------------------


def test_iter_pe_files_suffix_and_exclusion_rules(tmp_path: Path) -> None:
    """后缀大小写不敏感，仅排除一级 build/release，深层同名目录照常收集。"""
    rels = (
        "app.exe",
        "runtime/PYTHON311.DLL",
        "site-packages/_speedups.pyd",
        "build/artifact.dll",
        "release/report.dll",
        "nested/build/deep.dll",
        "notes.txt",
        "runtime/readme.md",
    )
    for rel in rels:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"MZ")

    got = [p.relative_to(tmp_path).as_posix() for p in iter_pe_files(tmp_path)]

    assert got == sorted(got)
    assert set(got) == {"app.exe", "runtime/PYTHON311.DLL", "site-packages/_speedups.pyd", "nested/build/deep.dll"}


def test_iter_pe_files_includes_symlinked_pe(tmp_path: Path) -> None:
    """符号链接指向的 PE 纳入扫描（跟随链接，与旧 rglob + is_file() 语义一致）。"""
    real = tmp_path / "runtime" / "libpython3.11.dll"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"MZ")
    link = tmp_path / "runtime" / "libpython.dll"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 无开发者模式/符号链接权限
        pytest.skip("当前环境不支持创建符号链接")

    assert link in iter_pe_files(tmp_path)


def test_iter_pe_files_missing_dist_returns_empty(tmp_path: Path) -> None:
    """dist 不存在时返回空列表，不抛异常（由调用方报告零扫描）。"""
    assert iter_pe_files(tmp_path / "nope") == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("python311.dll", True),
        ("_SPEEDUPS.PYD", True),
        ("app.exe", True),
        ("readme.md", False),
        ("no_suffix", False),
        (".dll", False),  # 纯点前缀名：pathlib 视为无后缀，不采集
        ("a.b.dll", True),
        ("dir.", False),
    ],
)
def test_is_pe_name_matches_pathlib_suffix(name: str, expected: bool) -> None:
    """``_is_pe_name`` 与 ``Path(name).suffix.lower() in _SCAN_SUFFIXES`` 判定一致。"""
    from fspack.packaging.win7.scan import _is_pe_name

    assert _is_pe_name(name) is expected
    assert (Path(name).suffix.lower() in _SCAN_SUFFIXES) is expected


def test_scan_empty_dist(tmp_path: Path) -> None:
    """空 dist 扫描结果为零计数且 ok."""
    report = scan_dist_win7(tmp_path)
    assert report.scanned == 0
    assert report.violations == ()
    assert report.skipped == ()
    assert report.ok


def test_scan_aggregates_violations_and_skips(tmp_path: Path) -> None:
    """违规聚合、非 PE 跳过、build/release 子目录排除."""
    _write_pe(tmp_path / "runtime" / "python312.dll")
    _write_pe(tmp_path / "site-packages" / "_rust.pyd", _BAD_IMPORTS)
    _write_pe(tmp_path / "app.exe")
    (tmp_path / "runtime" / "fake.dll").write_bytes(b"not-a-pe")
    # build/release 子目录排除（即使违规也不计入）
    _write_pe(tmp_path / "build" / "tmp.dll", _BAD_IMPORTS)
    _write_pe(tmp_path / "release" / "r.dll", _BAD_IMPORTS)

    report = scan_dist_win7(tmp_path)

    assert report.scanned == 3
    assert report.skipped == ("fake.dll",)
    assert not report.ok
    assert len(report.violations) == 1
    assert report.violations[0].path.name == "_rust.pyd"
    assert [v.target for v in report.violations[0].violations] == ["KERNEL32.dll!CopyFile2"]


def test_scan_counts_shim_and_ucrt_files(tmp_path: Path) -> None:
    """需 shim 与依赖 UCRT 的文件分别计数（运行前提提示，不算违规）."""
    _write_pe(tmp_path / "python311.dll", {"api-ms-win-core-path-l1-1-0.dll": [_SHIM_EXPORTED]})
    _write_pe(tmp_path / "vcruntime.pyd", {"api-ms-win-crt-runtime-l1-1-0.dll": ["memcpy"]})

    report = scan_dist_win7(tmp_path)

    assert report.scanned == 2
    assert report.ok
    assert report.shim_files == 1
    assert report.ucrt_files == 1


def test_scan_shim_none_skips_coverage(tmp_path: Path) -> None:
    """shim=None 跳过覆盖校验（导入任意 PathCch* 也不判 shim_missing）."""
    _write_pe(tmp_path / "python311.dll", {"api-ms-win-core-path-l1-1-0.dll": ["PathCchNotExists"]})

    report = scan_dist_win7(tmp_path, shim=None)

    assert report.ok
    assert report.shim_files == 1


def test_render_report_all_pass(tmp_path: Path) -> None:
    """全部通过时报告含通过计数与结论行."""
    _write_pe(tmp_path / "app.exe")
    report = scan_dist_win7(tmp_path)
    text = render_win7_report(report)
    assert "扫描文件: 1（通过 1，违规 0，跳过 0）" in text
    assert "全部通过" in text


def test_render_report_lists_violations(tmp_path: Path) -> None:
    """违规报告含相对路径、API 明细与不阻断结论."""
    dll = _write_pe(tmp_path / "site-packages" / "_rust.pyd", _BAD_IMPORTS)
    report = scan_dist_win7(tmp_path)
    text = render_win7_report(report)
    assert f"[违规] {dll.relative_to(tmp_path)}" in text
    assert "KERNEL32.dll!CopyFile2 — Win8+ API，Win7 SP1 不存在" in text
    assert "不阻断构建" in text


def test_render_report_tips_lines(tmp_path: Path) -> None:
    """shim/UCRT/跳过文件各有独立提示行."""
    _write_pe(tmp_path / "python311.dll", {"api-ms-win-core-path-l1-1-0.dll": [_SHIM_EXPORTED]})
    _write_pe(tmp_path / "vc.pyd", {"api-ms-win-crt-runtime-l1-1-0.dll": ["memcpy"]})
    (tmp_path / "broken.dll").write_bytes(b"x" * 16)
    report = scan_dist_win7(tmp_path)
    text = render_win7_report(report)
    assert "1 个文件依赖 Win8+ 系统库" in text
    assert "1 个文件依赖 api-ms-win-crt-*" in text
    assert "跳过非 PE 文件: broken.dll" in text


def test_write_win7_report_to_release(tmp_path: Path) -> None:
    """报告写入 dist/release/win7-compat-report.txt 并返回路径."""
    _write_pe(tmp_path / "app.exe")
    report = scan_dist_win7(tmp_path)
    out = write_win7_report(report, tmp_path)
    assert out == tmp_path / "release" / "win7-compat-report.txt"
    assert out.is_file()
    assert out.read_text(encoding="utf-8").startswith("Win7 兼容性扫描报告")


def test_enforce_win7_loaders_ok(tmp_path: Path) -> None:
    """Win7 干净的 loader exe 全部通过（含缓存命中复检场景）."""
    exes = [_write_pe(tmp_path / f"{name}.exe") for name in ("cli", "gui")]
    enforce_win7_loaders(exes)


# ---------------------------------------------------------------------------
# scan_dist_win7 增量缓存：命中复用、文件替换失效、缓存损坏/版本不符、shim 失效
# ---------------------------------------------------------------------------


def _cache_file(dist: Path) -> Path:
    return dist / "build" / "win7scan.json"


def test_scan_second_run_reuses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件未变时第二次扫描复用缓存结果，不再解析 PE."""
    from fspack.packaging.win7 import scan as scan_mod

    _write_pe(tmp_path / "app.exe")
    _write_pe(tmp_path / "site-packages" / "_rust.pyd", _BAD_IMPORTS)
    first = scan_dist_win7(tmp_path)
    assert _cache_file(tmp_path).is_file()

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("缓存命中时不应重新解析 PE")

    monkeypatch.setattr(scan_mod, "check_win7_imports", boom)
    second = scan_dist_win7(tmp_path)

    assert second.scanned == first.scanned == 2
    assert [v.target for v in second.violations[0].violations] == ["KERNEL32.dll!CopyFile2"]
    assert second.violations[0].path == tmp_path / "site-packages" / "_rust.pyd"
    assert second.shim_files == first.shim_files
    assert second.ucrt_files == first.ucrt_files


def test_scan_replaced_file_invalidates_cache(tmp_path: Path) -> None:
    """被替换的 DLL（size/mtime 变化）必须重扫，不能沿用旧的“通过”结论."""
    _write_pe(tmp_path / "runtime" / "python311.dll")
    assert scan_dist_win7(tmp_path).ok

    _write_pe(tmp_path / "runtime" / "python311.dll", _BAD_IMPORTS)
    report = scan_dist_win7(tmp_path)
    assert not report.ok
    assert [v.target for v in report.violations[0].violations] == ["KERNEL32.dll!CopyFile2"]


def test_scan_corrupt_cache_falls_back_to_full_scan(tmp_path: Path) -> None:
    """缓存文件损坏（非 JSON）时按全量扫描处理."""
    _write_pe(tmp_path / "app.exe")
    assert scan_dist_win7(tmp_path).scanned == 1
    _cache_file(tmp_path).write_text("{broken", encoding="utf-8")
    assert scan_dist_win7(tmp_path).scanned == 1


def test_scan_cache_version_mismatch_falls_back(tmp_path: Path) -> None:
    """缓存版本升级后旧条目全部失效（检查语义可能已变化）."""
    import json

    _write_pe(tmp_path / "app.exe")
    assert scan_dist_win7(tmp_path).scanned == 1
    payload = json.loads(_cache_file(tmp_path).read_text(encoding="utf-8"))
    payload["version"] = 0
    _cache_file(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    assert scan_dist_win7(tmp_path).scanned == 1


def test_scan_shim_signature_change_invalidates_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """shim 文件变化（路径/size/mtime）使整张缓存失效，避免沿用旧覆盖校验结论."""
    from fspack.packaging.win7 import scan as scan_mod

    shim = tmp_path / "shim.dll"
    shim.write_bytes(_build_pe(exports=[_SHIM_EXPORTED]))
    dist = tmp_path / "dist"
    _write_pe(dist / "python311.dll", {"api-ms-win-core-path-l1-1-0.dll": [_SHIM_EXPORTED]})
    assert scan_dist_win7(dist, shim=shim).ok

    calls: list[Path] = []
    real_check = scan_mod.check_win7_imports

    def spy(path: Path, *, shim: Path | None = None) -> Any:
        calls.append(path)
        return real_check(path, shim=shim)

    monkeypatch.setattr(scan_mod, "check_win7_imports", spy)
    shim.write_bytes(shim.read_bytes() + b"\x00")
    assert scan_dist_win7(dist, shim=shim).ok
    assert calls == [dist / "python311.dll"]


# ---------------------------------------------------------------------------
# enforce_win7_loaders 通过结论缓存
# ---------------------------------------------------------------------------


def test_enforce_win7_loaders_caches_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """loader 未变化时第二次校验直接读缓存，不重复解析 PE."""
    from fspack.packaging.win7 import scan as scan_mod

    exe = _write_pe(tmp_path / "cli.exe")
    cache_dir = tmp_path / "build"
    enforce_win7_loaders([exe], cache_dir=cache_dir)
    assert (cache_dir / "win7loaders.json").is_file()

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("缓存命中时不应重新解析 loader")

    monkeypatch.setattr(scan_mod, "check_win7_imports", boom)
    enforce_win7_loaders([exe], cache_dir=cache_dir)


def test_enforce_win7_loaders_failure_not_cached(tmp_path: Path) -> None:
    """违规 loader 不写缓存：每次构建都必须重新报错，不能因缓存放行."""
    import json

    exe = _write_pe(tmp_path / "cli.exe", _BAD_IMPORTS)
    cache_dir = tmp_path / "build"
    for _ in range(2):
        with pytest.raises(Win7ScanError, match="CopyFile2"):
            enforce_win7_loaders([exe], cache_dir=cache_dir)
    payload_path = cache_dir / "win7loaders.json"
    assert not payload_path.exists() or json.loads(payload_path.read_text(encoding="utf-8"))["files"] == {}


def test_enforce_win7_loaders_recompiled_invalidates(tmp_path: Path) -> None:
    """loader 重新编译（size/mtime 变化）后重新校验并发现新增违规."""
    exe = _write_pe(tmp_path / "cli.exe")
    cache_dir = tmp_path / "build"
    enforce_win7_loaders([exe], cache_dir=cache_dir)
    _write_pe(exe, _BAD_IMPORTS)
    with pytest.raises(Win7ScanError, match="CopyFile2"):
        enforce_win7_loaders([exe], cache_dir=cache_dir)


# ---------------------------------------------------------------------------
# inject_win7_shims：runtime 缺失 / 无需注入 / 源缺失 / 已存在跳过 / 成功注入
# ---------------------------------------------------------------------------

_RUST_WHEEL_IMPORTS = {"bcryptprimitives.dll": ["ProcessPrng"]}


def _write_rust_wheel_pyd(dist: Path) -> Path:
    """写入导入 bcryptprimitives（Rust 1.78+ 硬依赖）的合成 pyd."""
    return _write_pe(dist / "site-packages" / "pkg" / "native.pyd", _RUST_WHEEL_IMPORTS)


def test_inject_win7_shims_no_runtime_dir_returns_empty(tmp_path: Path) -> None:
    """runtime 目录缺失（dist 结构异常）时跳过注入，不抛错."""
    from fspack.packaging.win7.scan import inject_win7_shims

    dist = tmp_path / "dist"
    dist.mkdir()
    _write_rust_wheel_pyd(dist)

    assert inject_win7_shims(dist) == ()


def test_inject_win7_shims_nothing_needed_returns_empty(tmp_path: Path) -> None:
    """dist 中无 PE 需要 shim 时直接返回空元组."""
    from fspack.packaging.win7.scan import inject_win7_shims

    dist = tmp_path / "dist"
    (dist / "runtime").mkdir(parents=True)
    _write_pe(dist / "app.exe")

    assert inject_win7_shims(dist) == ()


def test_inject_win7_shims_copies_needed_shim(tmp_path: Path) -> None:
    """需要 shim 时复制内置 DLL 到 runtime/ 并返回注入的文件名."""
    from fspack.packaging.win7.dll import WIN7_SYSTEM_SHIMS
    from fspack.packaging.win7.scan import inject_win7_shims

    dist = tmp_path / "dist"
    (dist / "runtime").mkdir(parents=True)
    _write_rust_wheel_pyd(dist)

    injected = inject_win7_shims(dist)

    assert injected == ("bcryptprimitives.dll",)
    dest = dist / "runtime" / "bcryptprimitives.dll"
    assert dest.read_bytes() == WIN7_SYSTEM_SHIMS["bcryptprimitives.dll"].read_bytes()


def test_inject_win7_shims_skips_existing_dest(tmp_path: Path) -> None:
    """runtime/ 中已存在同名 shim 时跳过（幂等），不覆盖已有文件."""
    from fspack.packaging.win7.scan import inject_win7_shims

    dist = tmp_path / "dist"
    runtime = dist / "runtime"
    runtime.mkdir(parents=True)
    existing = runtime / "bcryptprimitives.dll"
    existing.write_bytes(b"already-injected")
    _write_rust_wheel_pyd(dist)

    assert inject_win7_shims(dist) == ()
    assert existing.read_bytes() == b"already-injected"


def test_inject_win7_shims_reuses_scan_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """传入已扫描的 shim 需求名时不再整树重扫（复用 scan_dist_win7 结果）."""
    from fspack.packaging.win7 import scan as scan_mod
    from fspack.packaging.win7.scan import inject_win7_shims

    dist = tmp_path / "dist"
    (dist / "runtime").mkdir(parents=True)
    _write_rust_wheel_pyd(dist)
    report = scan_dist_win7(dist)
    assert report.shim_names == frozenset({"bcryptprimitives.dll"})

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("传入 needed 后不应重新扫描 dist")

    monkeypatch.setattr(scan_mod, "check_win7_imports", boom)
    assert inject_win7_shims(dist, needed=report.shim_names) == ("bcryptprimitives.dll",)


def test_inject_win7_shims_missing_source_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """内置 shim 源文件缺失属硬错误，抛 OSError 阻断构建."""
    from fspack.packaging.win7 import scan

    dist = tmp_path / "dist"
    (dist / "runtime").mkdir(parents=True)
    _write_rust_wheel_pyd(dist)
    monkeypatch.setattr(scan, "WIN7_SYSTEM_SHIMS", {"bcryptprimitives.dll": tmp_path / "missing.dll"})

    with pytest.raises(OSError, match="Win7 shim 源文件缺失"):
        scan.inject_win7_shims(dist)


def test_enforce_win7_loaders_blocks(tmp_path: Path) -> None:
    """违规 exe 抛 Win7ScanError 且消息含 API 明细."""
    exe = _write_pe(tmp_path / "app.exe", _BAD_IMPORTS)
    with pytest.raises(Win7ScanError, match=r"KERNEL32\.dll!CopyFile2"):
        enforce_win7_loaders([exe])


def test_enforce_win7_loaders_reports_all_failures(tmp_path: Path) -> None:
    """多个违规 exe 的明细全部列在异常消息中."""
    exes = [
        _write_pe(tmp_path / "cli.exe", _BAD_IMPORTS),
        _write_pe(tmp_path / "gui.exe", {"KERNEL32.dll": ["PssCaptureSnapshot"]}),
    ]
    with pytest.raises(Win7ScanError) as exc_info:
        enforce_win7_loaders(exes)
    message = str(exc_info.value)
    assert "cli.exe" in message
    assert "gui.exe" in message
    assert "PssCaptureSnapshot" in message


def test_enforce_win7_loaders_skips_non_pe(tmp_path: Path) -> None:
    """非 PE 的 exe（如测试 stub 产物）告警跳过，不阻断也不影响其余 exe 校验."""
    bad = tmp_path / "stub.exe"
    bad.write_bytes(b"MZ-truncated")
    good = _write_pe(tmp_path / "app.exe")
    enforce_win7_loaders([bad, good])


def test_enforce_win7_loaders_missing_file_skipped(tmp_path: Path) -> None:
    """文件缺失（测试 stub 未生成 exe）同样告警跳过，不抛错."""
    enforce_win7_loaders([tmp_path / "not-exist.exe"])
