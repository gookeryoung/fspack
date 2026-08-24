"""``fsp b --no-size-report`` 体积报告单元测试.

覆盖 :mod:`fspack.packaging.size_report` 与 CLI 层 ``--no-size-report`` 标志：

- :func:`collect_size_report`：扫描 dist 目录返回结构化数据
- :func:`print_size_report`：渲染体积报告到控制台
- :class:`SizeCategory`/``PackageSize``/``SizeReport`` 数据类
- CLI ``--no-size-report`` 标志透传
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack import cli
from fspack.console import console
from fspack.packaging.size_report import (
    PackageSize,
    SizeCategory,
    SizeReport,
    _dir_size,
    _package_dir_size,
    _parse_dist_info_name,
    _size_from_record,
    collect_size_report,
    print_size_report,
)

# ---- 辅助函数 ----


def _make_dist_with_runtime(tmp_path: Path) -> Path:
    """创建含 runtime/src/site-packages 的最小 dist 目录."""
    dist = tmp_path / "dist"
    # site-packages 与 runtime 平级
    sp = dist / "site-packages"
    sp.mkdir(parents=True)
    (sp / "python311.dll").write_bytes(b"dll content")  # 11 bytes
    # src 目录
    src = dist / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')\n")
    # 其他文件（exe）
    (dist / "app.exe").write_bytes(b"exe content")  # 11 bytes
    return dist


def _make_dist_info(site_packages: Path, name: str, version: str, files: dict[str, bytes]) -> None:
    """在 site-packages 下创建一个 dist-info 目录与对应的包文件.

    Args:
        site_packages: site-packages 目录
        name: 包名（如 "requests"）
        version: 版本（如 "2.31.0"）
        files: {相对路径: 内容}，相对 site-packages，写入 RECORD
    """
    pkg_dir = site_packages / name.replace("-", "_")
    pkg_dir.mkdir(exist_ok=True)
    for rel_path, content in files.items():
        target = site_packages / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    dist_info = site_packages / f"{name}-{version}.dist-info"
    dist_info.mkdir(exist_ok=True)
    record_lines = [f"{rel_path},sha256=abc,{len(content)}" for rel_path, content in files.items()]
    record_lines.append(f"{name}-{version}.dist-info/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(record_lines), encoding="utf-8")


# ---- _dir_size ----


def test_dir_size_empty_dir(tmp_path: Path) -> None:
    """空目录返回 (0, 0)."""
    assert _dir_size(tmp_path) == (0, 0)


def test_dir_size_nonexistent(tmp_path: Path) -> None:
    """不存在的目录返回 (0, 0)."""
    assert _dir_size(tmp_path / "nope") == (0, 0)


def test_dir_size_accumulates_files(tmp_path: Path) -> None:
    """累加所有文件大小与数量."""
    (tmp_path / "a.txt").write_bytes(b"hello")  # 5
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"\x00" * 100)  # 100
    size, count = _dir_size(tmp_path)
    assert size == 105
    assert count == 2


# ---- _parse_dist_info_name ----


def test_parse_dist_info_name_normal() -> None:
    """正常 <name>-<version>.dist-info 目录名解析."""
    name, version = _parse_dist_info_name(Path("requests-2.31.0.dist-info"))
    assert name == "requests"
    assert version == "2.31.0"


def test_parse_dist_info_name_no_version() -> None:
    """无版本号时 version 为空."""
    name, version = _parse_dist_info_name(Path("mypackage.dist-info"))
    assert name == "mypackage"
    assert version == ""


def test_parse_dist_info_name_complex_name() -> None:
    """包名含连字符时正确解析（从右侧分离版本）."""
    name, version = _parse_dist_info_name(Path("ordered-set-4.1.0.dist-info"))
    assert name == "ordered-set"
    assert version == "4.1.0"


# ---- _size_from_record ----


def test_size_from_record_accumulates_files(tmp_path: Path) -> None:
    """RECORD 文件记录的文件累加大小."""
    site_packages = tmp_path / "sp"
    site_packages.mkdir()
    (site_packages / "pkg").mkdir()
    (site_packages / "pkg" / "__init__.py").write_bytes(b"x = 1\n")
    (site_packages / "pkg" / "main.py").write_bytes(b"print('hi')\n")
    record = site_packages / "pkg-1.0.0.dist-info" / "RECORD"
    record.parent.mkdir()
    record.write_text(
        "pkg/__init__.py,sha256=abc,6\npkg/main.py,sha256=def,12\npkg-1.0.0.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    size, count = _size_from_record(site_packages, record)
    assert size == 18  # 6 + 12
    assert count == 2


def test_size_from_record_missing_file(tmp_path: Path) -> None:
    """RECORD 引用的文件不存在时跳过不报错."""
    site_packages = tmp_path / "sp"
    site_packages.mkdir()
    record = site_packages / "pkg-1.0.0.dist-info" / "RECORD"
    record.parent.mkdir()
    record.write_text("pkg/missing.py,sha256=abc,100\n", encoding="utf-8")
    size, count = _size_from_record(site_packages, record)
    assert size == 0
    assert count == 0


def test_size_from_record_path_with_comma(tmp_path: Path) -> None:
    """RECORD 路径含逗号时用 CSV 引号包裹，csv.reader 正确解析.

    旧实现 ``line.split(",")`` 在路径含逗号时会错位（把逗号后的部分当 hash），
    导致文件被错误跳过。CSV 规范要求含逗号的字段用双引号包裹，``csv.reader``
    能正确还原。本测试构造 ``"pkg/sub,dir/file.py"`` 路径验证修复。
    """
    site_packages = tmp_path / "sp"
    site_packages.mkdir()
    # 创建含逗号的子目录与文件
    comma_dir = site_packages / "pkg" / "sub,dir"
    comma_dir.mkdir(parents=True)
    (comma_dir / "file.py").write_bytes(b"y = 2\n")  # 6 字节
    record = site_packages / "pkg-1.0.0.dist-info" / "RECORD"
    record.parent.mkdir()
    # CSV 引号包裹含逗号的路径
    record.write_text(
        '"pkg/sub,dir/file.py",sha256=abc,6\npkg-1.0.0.dist-info/RECORD,,\n',
        encoding="utf-8",
    )
    size, count = _size_from_record(site_packages, record)
    assert size == 6
    assert count == 1


# ---- _package_dir_size ----


def test_package_dir_size_with_record(tmp_path: Path) -> None:
    """有 RECORD 时按 RECORD 累加."""
    site_packages = tmp_path / "sp"
    site_packages.mkdir()
    _make_dist_info(site_packages, "requests", "2.31.0", {"requests/__init__.py": b"x = 1\n"})
    dist_info = site_packages / "requests-2.31.0.dist-info"
    size, count = _package_dir_size(site_packages, dist_info)
    assert size == 6  # b"x = 1\n" = 6 字节
    assert count == 1


def test_package_dir_size_without_record(tmp_path: Path) -> None:
    """无 RECORD 时按包名前缀匹配."""
    site_packages = tmp_path / "sp"
    site_packages.mkdir()
    pkg_dir = site_packages / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_bytes(b"x = 1\n")  # 6 字节
    (pkg_dir / "sub.py").write_bytes(b"y = 2\n")  # 6 字节
    dist_info = site_packages / "mypkg-1.0.0.dist-info"
    dist_info.mkdir()
    # 不创建 RECORD，触发回退逻辑（dist-info 目录不应被计入）
    size, count = _package_dir_size(site_packages, dist_info)
    assert size == 12  # 6 + 6
    assert count == 2


# ---- collect_size_report ----


def test_collect_size_report_empty_dist(tmp_path: Path) -> None:
    """dist 不存在时返回空报告."""
    report = collect_size_report(tmp_path / "nope")
    assert report.total_size == 0
    assert report.total_files == 0
    assert report.categories == ()
    assert report.top_packages == ()


def test_collect_size_report_categories(tmp_path: Path) -> None:
    """报告含 runtime/src/site-packages/其他 四个类别."""
    dist = _make_dist_with_runtime(tmp_path)
    report = collect_size_report(dist)
    names = [c.name for c in report.categories]
    assert names == ["runtime", "src", "site-packages", "其他"]


def test_collect_size_report_totals(tmp_path: Path) -> None:
    """total_size 与 total_files 为各类别之和."""
    dist = _make_dist_with_runtime(tmp_path)
    report = collect_size_report(dist)
    expected_total = sum(c.size for c in report.categories)
    expected_files = sum(c.file_count for c in report.categories)
    assert report.total_size == expected_total
    assert report.total_files == expected_files


def test_collect_size_report_top_packages_sorted(tmp_path: Path) -> None:
    """site-packages Top N 包按体积降序排序."""
    dist = _make_dist_with_runtime(tmp_path)
    sp = dist / "site-packages"
    _make_dist_info(sp, "big", "1.0.0", {"big/__init__.py": b"x" * 1000})
    _make_dist_info(sp, "small", "1.0.0", {"small/__init__.py": b"x" * 10})
    report = collect_size_report(dist, top_n=2)
    assert len(report.top_packages) == 2
    assert report.top_packages[0].name == "big"
    assert report.top_packages[0].size > report.top_packages[1].size


def test_collect_size_report_top_n_limit(tmp_path: Path) -> None:
    """top_n 限制返回包数量."""
    dist = _make_dist_with_runtime(tmp_path)
    sp = dist / "site-packages"
    for i in range(5):
        _make_dist_info(sp, f"pkg{i}", "1.0.0", {f"pkg{i}/__init__.py": b"x" * (i + 1) * 10})
    report = collect_size_report(dist, top_n=3)
    assert len(report.top_packages) == 3


def test_collect_size_report_other_category(tmp_path: Path) -> None:
    """其他类别包含 dist 根目录下非 runtime/src 的文件."""
    dist = _make_dist_with_runtime(tmp_path)
    report = collect_size_report(dist)
    other = next(c for c in report.categories if c.name == "其他")
    assert other.size > 0  # app.exe
    assert other.file_count >= 1


# ---- print_size_report ----


def test_print_size_report_renders_tables(tmp_path: Path) -> None:
    """print_size_report 渲染类别分布与 Top N 表格."""
    dist = _make_dist_with_runtime(tmp_path)
    sp = dist / "site-packages"
    _make_dist_info(sp, "rich", "13.0.0", {"rich/__init__.py": b"x" * 100})

    with console.rich.capture() as capture:
        report = print_size_report(dist)

    out = capture.get()
    assert "体积报告" in out
    assert "类别分布" in out
    assert "runtime" in out
    assert "src" in out
    assert "site-packages" in out
    assert "其他" in out
    assert "总计" in out
    assert "rich" in out  # Top N 表含包名
    assert report.total_size > 0


def test_print_size_report_empty_dist_skips(tmp_path: Path) -> None:
    """dist 为空时不输出报告（仅返回空 SizeReport）."""
    dist = tmp_path / "empty"
    dist.mkdir()

    with console.rich.capture() as capture:
        report = print_size_report(dist)

    out = capture.get()
    assert out == ""  # 空输出
    assert report.total_size == 0


def test_print_size_report_no_packages_skips_top_table(tmp_path: Path) -> None:
    """site-packages 无 dist-info 时不输出 Top N 表."""
    dist = _make_dist_with_runtime(tmp_path)

    with console.rich.capture() as capture:
        print_size_report(dist)

    out = capture.get()
    assert "Top" not in out  # 无包时不渲染 Top 表


# ---- 数据类 ----


def test_size_category_size_formatted() -> None:
    """SizeCategory.size_formatted 返回人类可读字符串."""
    cat = SizeCategory(name="runtime", size=1024 * 1024, file_count=10)
    assert "MB" in cat.size_formatted


def test_package_size_size_formatted() -> None:
    """PackageSize.size_formatted 返回人类可读字符串."""
    pkg = PackageSize(name="requests", version="2.31.0", size=2048, file_count=5)
    assert "KB" in pkg.size_formatted


def test_size_report_total_size_formatted() -> None:
    """SizeReport.total_size_formatted 返回人类可读字符串."""
    report = SizeReport(
        categories=(SizeCategory(name="runtime", size=1024, file_count=1),),
        top_packages=(),
        total_size=1024,
        total_files=1,
    )
    assert "KB" in report.total_size_formatted


# ---- CLI 层 --no-size-report 标志 ----


def _make_minimal_project(tmp_path: Path) -> Path:
    """创建最小可解析项目."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return tmp_path


def test_cli_build_no_size_report_flag_passed_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp b --no-size-report`` 透传 no_size_report=True 给 build()."""
    _make_minimal_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: bool = False,
        profile_out: Path | None = None,
        profile_compare: str | None = None,
        auto_clean: bool = False,
    ) -> None:
        captured["options"] = options

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--no-size-report"])
    assert captured["options"].no_size_report is True


def test_cli_build_without_no_size_report_defaults_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --no-size-report 时 no_size_report=False."""
    _make_minimal_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: bool = False,
        profile_out: Path | None = None,
        profile_compare: str | None = None,
        auto_clean: bool = False,
    ) -> None:
        captured["options"] = options

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert captured["options"].no_size_report is False
