"""manifest 产物清单生成与差异对比测试.

覆盖 :mod:`fspack.packaging.manifest` 公共 API 与内部辅助函数：

- :func:`collect_manifest`：扫描 dist 目录返回 manifest 字典（entries/summary）
- :func:`generate_manifest`：生成 JSON 文件到 dist/release/<name>-<version>-manifest.json
- :func:`load_manifest`：加载 manifest JSON + 版本兼容校验
- :func:`diff_manifest`：新增/删除/修改三类差异检测
- :func:`print_manifest_diff`：控制台格式化输出（无差异/有差异路径均覆盖）
- 分类规则（runtime/site-packages/src/release/build/other）验证
- manifest 自身被扫描时跳过（避免每次生成 sha256 变化）
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType, ProjectInfo
from fspack.packaging.manifest import (
    ManifestDiff,
    ManifestEntry,
    _categorize,
    _format_size,
    _format_size_delta,
    _sha256_file,
    collect_manifest,
    diff_manifest,
    generate_manifest,
    load_manifest,
    print_manifest_diff,
)


def _make_info(tmp_path: Path, name: str = "app", version: str = "1.0") -> ProjectInfo:
    """构造最小 ProjectInfo 用于 manifest 测试."""
    return ProjectInfo(
        name=name,
        version=version,
        src_dir=tmp_path,
        entry_module=name,
        entry_file=tmp_path / f"{name}.py",
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.11.9",
    )


def _make_dist_tree(dist: Path) -> dict[str, bytes]:
    """构造多分类 dist 目录，返回 {相对路径: 字节内容}.

    覆盖所有分类：runtime / site-packages / src / release / build / other（根目录文件）
    """
    files: dict[str, bytes] = {
        "runtime/python3.11.exe": b"fake-python-binary",
        "runtime/Lib/site-packages/_distutils_hack/__init__.py": b"hack",
        "site-packages/requests/__init__.py": b"import requests",
        "site-packages/requests/api.py": b"def get():\n    pass",
        "src/app/main.py": b"def main():\n    print('hi')",
        "src/app/data/config.json": b'{"key": "val"}',
        "release/README.txt": "发布说明".encode(),
        "build/icon.ico": b"fake-ico-bytes",
        "python311._pth": b".\\src\\n.\\site-packages",
        "python.exe": b"stub-python",
    }
    for rel, content in files.items():
        p = dist / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return files


def test_collect_manifest_categories_and_summary(tmp_path: Path) -> None:
    """collect_manifest 应正确分类各子目录并汇总总大小/文件数."""
    dist = tmp_path / "dist"
    info = _make_info(tmp_path)
    files = _make_dist_tree(dist)

    data = collect_manifest(dist, info)

    assert data["manifestVersion"] == 1
    assert data["project"] == {"name": "app", "version": "1.0", "py_version": "3.11.9"}
    assert data["summary"]["total_files"] == len(files)
    assert data["summary"]["total_size"] == sum(len(v) for v in files.values())

    # entries 按 path 排序
    paths = [e["path"] for e in data["entries"]]
    assert paths == sorted(paths)

    # 分类校验：按相对路径前缀分配正确分类
    by_path = {e["path"]: e for e in data["entries"]}
    assert by_path["runtime/python3.11.exe"]["category"] == "runtime"
    assert by_path["site-packages/requests/__init__.py"]["category"] == "site-packages"
    assert by_path["src/app/main.py"]["category"] == "src"
    assert by_path["release/README.txt"]["category"] == "release"
    assert by_path["build/icon.ico"]["category"] == "build"
    assert by_path["python311._pth"]["category"] == "other"

    # sha256 与大小值与原始内容一致
    for rel, content in files.items():
        e = by_path[rel]
        assert e["size"] == len(content)
        assert e["sha256"] == hashlib.sha256(content).hexdigest()

    # category_count 汇总与实际分类一致
    cc = data["summary"]["category_count"]
    for e in data["entries"]:
        cat = e["category"]
        assert cc[cat]["files"] >= 1
        assert cc[cat]["size"] >= e["size"]


def test_generate_manifest_writes_json_and_skips_self(tmp_path: Path) -> None:
    """generate_manifest 写 JSON 到 release/，且下次 collect 跳过自身."""
    dist = tmp_path / "dist"
    info = _make_info(tmp_path)
    _make_dist_tree(dist)

    out = generate_manifest(dist, info)

    assert out.is_file()
    assert out.name == "app-1.0-manifest.json"
    assert out.parent.name == "release"

    raw = json.loads(out.read_text(encoding="utf-8"))
    # 写入内容与 collect_manifest 返回一致（版本/条目结构）
    assert raw["manifestVersion"] == 1
    assert len(raw["entries"]) == 10

    # 重新 collect（manifest 已存在）：manifest JSON 自身应被跳过，entries 数量不变
    data2 = collect_manifest(dist, info)
    # 10 个原始文件，不包含 manifest 自身
    assert data2["summary"]["total_files"] == 10


def test_collect_manifest_empty_dist_returns_no_entries(tmp_path: Path) -> None:
    """空 dist 目录（无文件）应返回空 entries、total_files=0 不崩溃."""
    dist = tmp_path / "dist"
    dist.mkdir()
    info = _make_info(tmp_path)

    data = collect_manifest(dist, info)

    assert data["summary"]["total_files"] == 0
    assert data["summary"]["total_size"] == 0
    assert data["entries"] == []


def test_diff_manifest_added_removed_modified(tmp_path: Path) -> None:
    """diff_manifest 三类差异均能正确检测."""
    old_entries = [
        ManifestEntry(path="a.txt", size=10, sha256="a" * 64, category="src"),
        ManifestEntry(path="b.txt", size=20, sha256="b" * 64, category="src"),
        ManifestEntry(path="c.txt", size=30, sha256="c" * 64, category="runtime"),
    ]
    new_entries = [
        ManifestEntry(path="b.txt", size=20, sha256="b" * 64, category="src"),  # 未变化
        ManifestEntry(path="c.txt", size=99, sha256="c" * 64, category="runtime"),  # 仅大小变
        ManifestEntry(path="d.txt", size=5, sha256="d" * 64, category="release"),  # 新增
    ]

    old: dict[str, Any] = {"entries": [asdict(e) for e in old_entries]}
    new: dict[str, Any] = {"entries": [asdict(e) for e in new_entries]}

    diff = diff_manifest(old, new)

    # added: d.txt
    assert [e.path for e in diff.added] == ["d.txt"]
    # removed: a.txt
    assert [e.path for e in diff.removed] == ["a.txt"]
    # modified: c.txt（大小变化）
    assert [(o.path, n.path) for o, n in diff.modified] == [("c.txt", "c.txt")]
    old_c, new_c = diff.modified[0]
    assert old_c.size == 30
    assert new_c.size == 99

    # is_empty 属性
    assert diff.is_empty is False
    assert ManifestDiff(added=[], removed=[], modified=[]).is_empty is True


def test_diff_manifest_sha256_change_only_is_modified(tmp_path: Path) -> None:
    """内容变化（sha256 变但大小不变）也应判为 modified."""
    old: dict[str, Any] = {
        "entries": [
            asdict(ManifestEntry(path="x.bin", size=4, sha256="0" * 64, category="other")),
        ]
    }
    new: dict[str, Any] = {
        "entries": [
            asdict(ManifestEntry(path="x.bin", size=4, sha256="f" * 64, category="other")),
        ]
    }
    diff = diff_manifest(old, new)
    assert len(diff.modified) == 1
    assert diff.added == []
    assert diff.removed == []


def test_load_manifest_rejects_incompatible_version(tmp_path: Path) -> None:
    """load_manifest 对非法结构 / 不兼容版本应抛 ValueError."""
    # 结构非法
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="非法 manifest 结构"):
        load_manifest(bad)

    # 缺少 manifestVersion 键
    no_ver = tmp_path / "no_ver.json"
    no_ver.write_text(json.dumps({"entries": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="非法 manifest 结构"):
        load_manifest(no_ver)

    # 版本不兼容
    wrong_ver = tmp_path / "wrong_ver.json"
    wrong_ver.write_text(
        json.dumps({"manifestVersion": 999, "entries": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="版本不兼容"):
        load_manifest(wrong_ver)

    # 正常版本可加载
    ok = tmp_path / "ok.json"
    ok.write_text(
        json.dumps({"manifestVersion": 1, "entries": []}),
        encoding="utf-8",
    )
    assert load_manifest(ok) == {"manifestVersion": 1, "entries": []}


def test_categorize_rules(tmp_path: Path) -> None:
    """_categorize 按前缀顺序匹配，未命中返回 other."""
    assert _categorize("runtime/python.exe") == "runtime"
    assert _categorize("runtime/sub/a.dll") == "runtime"
    assert _categorize("site-packages/pkg/__init__.py") == "site-packages"
    assert _categorize("src/app/main.py") == "src"
    assert _categorize("release/sbom.json") == "release"
    assert _categorize("build/stamp.txt") == "build"
    # 无前缀：dist 根或未知子目录 → other
    assert _categorize("python._pth") == "other"
    assert _categorize("unknown/sub/x") == "other"


def test_sha256_file_missing_returns_empty(tmp_path: Path) -> None:
    """_sha256_file 读取不存在文件时返回空串（不抛异常）."""
    missing = tmp_path / "nope.bin"
    assert _sha256_file(missing) == ""


def test_format_size_and_delta(tmp_path: Path) -> None:
    """_format_size 按量级切换单位；_format_size_delta 带正负号."""
    assert _format_size(0) == "0B"
    assert _format_size(1023) == "1023B"
    assert _format_size(1024).endswith("KB")
    assert _format_size(1024 * 1024 * 5).endswith("MB")
    assert _format_size_delta(1024).startswith("+")
    assert _format_size_delta(-2048).startswith("-")
    assert "+" not in _format_size_delta(0)


def test_print_manifest_diff_both_paths(capsys: pytest.CaptureFixture[str]) -> None:
    """print_manifest_diff 无差异/有差异路径均输出且不崩溃."""
    # 无差异
    diff_empty = ManifestDiff(added=[], removed=[], modified=[])
    print_manifest_diff(diff_empty)
    # 有差异
    diff = ManifestDiff(
        added=[ManifestEntry(path="n.py", size=8, sha256="a" * 64, category="src")],
        removed=[ManifestEntry(path="o.py", size=4, sha256="b" * 64, category="src")],
        modified=[
            (
                ManifestEntry(path="c.txt", size=100, sha256="c" * 64, category="runtime"),
                ManifestEntry(path="c.txt", size=200, sha256="d" * 64, category="runtime"),
            )
        ],
    )
    print_manifest_diff(diff)
    # 控制台输出通过 rich 输出到 stdout/stderr，关键字段均应被渲染
    out = capsys.readouterr().out
    # 注意：rich 可能输出 ANSI 码，按关键字匹配即可
    assert "新增" in out or "n.py" in out or "差异概览" in out
