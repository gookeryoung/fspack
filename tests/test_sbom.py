"""SBOM（软件物料清单）生成测试：SPDX 2.3 兼容 JSON 结构与许可证/校验和提取.

覆盖 :mod:`fspack.packaging.sbom` 公共 API 与内部辅助函数：

- :func:`collect_sbom`：扫描 dist 目录返回 SPDX 2.3 字典
- :func:`generate_sbom`：生成 JSON 文件到 dist/release/<name>-<version>-sbom.json
- :func:`_extract_license`：METADATA 许可证字段优先级解析
- :func:`_compute_package_checksum`：基于 RECORD 的包整体 SHA256 计算
- :func:`_sanitize_spdx_id`：包名转合法 SPDX ID
- 无 site-packages 时不崩溃（packages 为空列表）
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fspack.config import AppType, ProjectInfo
from fspack.packaging.sbom import (
    SbomPackage,
    _compute_package_checksum,
    _extract_license,
    _sanitize_spdx_id,
    collect_sbom,
    generate_sbom,
)


def _make_info(tmp_path: Path, name: str = "app", version: str = "1.0") -> ProjectInfo:
    """构造最小 ProjectInfo 用于 SBOM 测试（参考 test_linux_installer.py 模式）."""
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


def _make_dist_info(
    site_packages: Path,
    pkg_name: str,
    pkg_version: str,
    *,
    metadata: str = "",
    record_entries: list[tuple[str, bytes]] | None = None,
) -> Path:
    """构造单个 dist-info 目录（METADATA + RECORD + 引用文件）.

    Args:
        site_packages: site-packages 根目录
        pkg_name: 包名（如 ``requests``）
        pkg_version: 包版本（如 ``2.31.0``）
        metadata: METADATA 文件内容（可含 License-Expression/License/License-File 字段）
        record_entries: RECORD 条目列表，每项为 ``(相对路径, 文件字节内容)``；
            相对路径基于 site-packages（即 dist-info.parent）

    Returns:
        dist-info 目录路径
    """
    dist_info = site_packages / f"{pkg_name}-{pkg_version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {pkg_name}\nVersion: {pkg_version}\n{metadata}",
        encoding="utf-8",
    )
    # RECORD 文件格式：<path>,<hash>,<size>，path 相对 site-packages
    record_lines: list[str] = []
    for rel_path, content in record_entries or []:
        actual_path = site_packages / rel_path
        actual_path.parent.mkdir(parents=True, exist_ok=True)
        actual_path.write_bytes(content)
        file_hash = hashlib.sha256(content).hexdigest()
        record_lines.append(f"{rel_path},sha256={file_hash},{len(content)}")
    # RECORD 自身条目（wheel 规范要求，hash 为空）
    record_lines.append("RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(record_lines), encoding="utf-8")
    return dist_info


def _make_dist_with_site_packages(tmp_path: Path) -> tuple[Path, Path]:
    """构造含 Windows embed 风格 site-packages 的 dist 目录.

    返回 (dist_dir, site_packages_dir)。
    """
    dist = tmp_path / "dist"
    site_packages = dist / "runtime" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    return dist, site_packages


# ---- collect_sbom 结构测试 ----


def test_collect_sbom_returns_spdx_23_structure(tmp_path: Path) -> None:
    """collect_sbom 返回 SPDX 2.3 兼容字典，含所有顶层字段."""
    dist, site_packages = _make_dist_with_site_packages(tmp_path)
    _make_dist_info(
        site_packages,
        "requests",
        "2.31.0",
        metadata="License-Expression: Apache-2.0\n",
        record_entries=[("requests/__init__.py", b"import os\n")],
    )
    info = _make_info(tmp_path, name="myapp", version="2.0")

    sbom = collect_sbom(dist, info)

    # SPDX 2.3 顶层字段
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert sbom["dataLicense"] == "CC0-1.0"
    assert sbom["SPDXID"] == "SPDXRef-DOCUMENT"
    assert sbom["name"] == "myapp"
    assert isinstance(sbom["documentNamespace"], str)
    assert "myapp-2.0" in sbom["documentNamespace"]
    # creationInfo 含创建时间与工具信息
    creation = sbom["creationInfo"]
    assert "created" in creation
    assert creation["creators"] == ["Tool: fspack"]
    # packages 数组含 1 个包
    packages = sbom["packages"]
    assert isinstance(packages, list)
    assert len(packages) == 1
    pkg = packages[0]
    assert pkg["name"] == "requests"
    assert pkg["version"] == "2.31.0"
    assert pkg["spdx_id"] == "SPDXRef-Package-requests"
    assert pkg["download_location"] == "NOASSERTION"
    assert pkg["files_analyzed"] is True
    assert pkg["license_concluded"] == "Apache-2.0"
    assert pkg["license_declared"] == "Apache-2.0"
    assert pkg["supplier"] == "NOASSERTION"
    # 校验和列表含 1 条 SHA256 记录
    assert len(pkg["checksums"]) == 1
    assert pkg["checksums"][0]["algorithm"] == "SHA256"
    assert len(pkg["checksums"][0]["checksumValue"]) == 64


def test_collect_sbom_namespace_unique_per_call(tmp_path: Path) -> None:
    """documentNamespace 含 UUID，多次调用产生不同命名空间."""
    dist, site_packages = _make_dist_with_site_packages(tmp_path)
    _make_dist_info(site_packages, "pkg", "1.0", metadata="License: MIT\n")
    info = _make_info(tmp_path)

    ns1 = collect_sbom(dist, info)["documentNamespace"]
    ns2 = collect_sbom(dist, info)["documentNamespace"]
    assert ns1 != ns2, "UUID 应保证命名空间唯一"


def test_collect_sbom_no_site_packages_returns_empty_packages(tmp_path: Path) -> None:
    """无 site-packages 目录时不崩溃，packages 为空列表."""
    dist = tmp_path / "dist"
    dist.mkdir()
    # 不创建 runtime/Lib/site-packages
    info = _make_info(tmp_path)

    sbom = collect_sbom(dist, info)

    assert sbom["packages"] == []
    # 顶层字段仍完整
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert sbom["name"] == "app"


def test_collect_sbom_empty_site_packages_returns_empty_packages(tmp_path: Path) -> None:
    """site-packages 目录存在但无 dist-info 时 packages 为空列表."""
    dist, _site_packages = _make_dist_with_site_packages(tmp_path)
    info = _make_info(tmp_path)

    sbom = collect_sbom(dist, info)

    assert sbom["packages"] == []


def test_collect_sbom_skips_non_dist_info_dirs(tmp_path: Path) -> None:
    """非 *.dist-info 目录（如普通包目录）被跳过."""
    dist, site_packages = _make_dist_with_site_packages(tmp_path)
    # 普通包目录（非 dist-info）应被忽略
    (site_packages / "requests").mkdir()
    (site_packages / "requests" / "__init__.py").write_text("")
    # 正常 dist-info
    _make_dist_info(site_packages, "rich", "13.0.0", metadata="License: MIT\n")
    info = _make_info(tmp_path)

    sbom = collect_sbom(dist, info)

    packages = sbom["packages"]
    assert len(packages) == 1
    assert packages[0]["name"] == "rich"


def test_collect_sbom_linux_standalone_site_packages(tmp_path: Path) -> None:
    """Linux standalone 风格 site-packages 路径也能被识别."""
    dist = tmp_path / "dist"
    site_packages = dist / "runtime" / "python" / "lib" / "python3.11" / "site-packages"
    site_packages.mkdir(parents=True)
    _make_dist_info(
        site_packages,
        "click",
        "8.1.0",
        metadata="License: BSD-3-Clause\n",
        record_entries=[("click/__init__.py", b"x = 1\n")],
    )
    info = _make_info(tmp_path)

    sbom = collect_sbom(dist, info)

    packages = sbom["packages"]
    assert len(packages) == 1
    assert packages[0]["name"] == "click"


# ---- generate_sbom 文件生成测试 ----


def test_generate_sbom_creates_json_file(tmp_path: Path) -> None:
    """generate_sbom 在 dist/release/<name>-<version>-sbom.json 生成 JSON 文件."""
    dist, site_packages = _make_dist_with_site_packages(tmp_path)
    _make_dist_info(
        site_packages,
        "requests",
        "2.31.0",
        metadata="License-Expression: Apache-2.0\n",
        record_entries=[("requests/__init__.py", b"v1\n")],
    )
    info = _make_info(tmp_path, name="myapp", version="3.5")

    sbom_path = generate_sbom(dist, info)

    expected = dist / "release" / "myapp-3.5-sbom.json"
    assert sbom_path == expected
    assert sbom_path.is_file()
    # 文件内容为合法 JSON，结构与 collect_sbom 一致
    data = json.loads(sbom_path.read_text(encoding="utf-8"))
    assert data["spdxVersion"] == "SPDX-2.3"
    assert data["name"] == "myapp"
    assert len(data["packages"]) == 1
    assert data["packages"][0]["name"] == "requests"


def test_generate_sbom_creates_release_dir_if_missing(tmp_path: Path) -> None:
    """generate_sbom 自动创建 release 目录."""
    dist, site_packages = _make_dist_with_site_packages(tmp_path)
    _make_dist_info(site_packages, "pkg", "1.0", metadata="License: MIT\n")
    info = _make_info(tmp_path)

    sbom_path = generate_sbom(dist, info)

    assert sbom_path.is_file()
    assert sbom_path.parent.is_dir()


def test_generate_sbom_overwrites_existing(tmp_path: Path) -> None:
    """重复调用 generate_sbom 覆盖已有文件."""
    dist, site_packages = _make_dist_with_site_packages(tmp_path)
    _make_dist_info(site_packages, "pkg", "1.0", metadata="License: MIT\n")
    info = _make_info(tmp_path)

    first = generate_sbom(dist, info)
    first.write_text("stale", encoding="utf-8")

    second = generate_sbom(dist, info)
    assert second.read_text(encoding="utf-8") != "stale"
    # 内容仍为合法 JSON
    json.loads(second.read_text(encoding="utf-8"))


# ---- _extract_license 优先级测试 ----


def test_extract_license_prefers_license_expression(tmp_path: Path) -> None:
    """License-Expression 优先于 License 与 License-File."""
    metadata = "License-Expression: Apache-2.0\nLicense: old MIT text\nLicense-File: LICENSE\n"
    assert _extract_license(metadata, "pkg") == "Apache-2.0"


def test_extract_license_uses_license_when_no_expression(tmp_path: Path) -> None:
    """无 License-Expression 时用 License 字段（单行短字符串视为 SPDX 标识）."""
    metadata = "License: MIT\n"
    assert _extract_license(metadata, "pkg") == "MIT"


def test_extract_license_returns_noassertion_for_long_license_text() -> None:
    """License 字段首行超过 80 字符时视为自由文本，返回 NOASSERTION.

    注意：正则 ``^License:\\s*(.+?)\\s*$`` 仅捕获首行，多行文本的后续行不参与匹配。
    因此「多行」本质上是首行长度判定：首行 > 80 字符即返回 NOASSERTION。
    """
    long_first_line = "A" * 81 + "\nsecond line does not matter"
    metadata = f"License: {long_first_line}\n"
    assert _extract_license(metadata, "pkg") == "NOASSERTION"


def test_extract_license_uses_license_file_when_no_expression_or_license() -> None:
    """无 License-Expression 与 License 时，存在 License-File 返回 LicenseRef-<pkg>."""
    metadata = "License-File: LICENSE\n"
    assert _extract_license(metadata, "my_pkg") == "LicenseRef-my-pkg"


def test_extract_license_returns_noassertion_when_all_missing() -> None:
    """所有 License 字段均缺失时返回 NOASSERTION."""
    metadata = "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
    assert _extract_license(metadata, "pkg") == "NOASSERTION"


def test_extract_license_returns_noassertion_for_long_single_line_license() -> None:
    """License 字段单行但超过 80 字符时视为自由文本，返回 NOASSERTION."""
    long_license = "A" * 81
    metadata = f"License: {long_license}\n"
    assert _extract_license(metadata, "pkg") == "NOASSERTION"


# ---- _compute_package_checksum 测试 ----


def test_compute_package_checksum_based_on_record(tmp_path: Path) -> None:
    """_compute_package_checksum 基于 RECORD 列出文件计算 SHA256."""
    site_packages = tmp_path / "sp"
    site_packages.mkdir()
    dist_info = _make_dist_info(
        site_packages,
        "pkg",
        "1.0",
        record_entries=[
            ("pkg/__init__.py", b"init\n"),
            ("pkg/mod.py", b"mod\n"),
        ],
    )

    checksum = _compute_package_checksum(dist_info)

    assert checksum is not None
    assert len(checksum) == 64
    # 手动复算：按相对路径排序后拼接 ``rel:hash`` 再 SHA256
    file_hashes = [
        ("pkg/__init__.py", hashlib.sha256(b"init\n").hexdigest()),
        ("pkg/mod.py", hashlib.sha256(b"mod\n").hexdigest()),
    ]
    file_hashes.sort(key=lambda x: x[0])
    expected = hashlib.sha256("\n".join(f"{rel}:{h}" for rel, h in file_hashes).encode("utf-8")).hexdigest()
    assert checksum == expected


def test_compute_package_checksum_skips_record_self(tmp_path: Path) -> None:
    """RECORD 自身条目被跳过（避免循环）."""
    site_packages = tmp_path / "sp"
    site_packages.mkdir()
    dist_info = _make_dist_info(
        site_packages,
        "pkg",
        "1.0",
        record_entries=[("pkg/mod.py", b"v\n")],
    )

    checksum = _compute_package_checksum(dist_info)

    assert checksum is not None
    # 仅 pkg/mod.py 参与计算
    inner_hash = hashlib.sha256(b"v\n").hexdigest()
    expected = hashlib.sha256(f"pkg/mod.py:{inner_hash}".encode()).hexdigest()
    assert checksum == expected


def test_compute_package_checksum_no_record_returns_none(tmp_path: Path) -> None:
    """无 RECORD 文件时返回 None."""
    site_packages = tmp_path / "sp"
    site_packages.mkdir()
    dist_info = site_packages / "pkg-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Name: pkg\n", encoding="utf-8")

    assert _compute_package_checksum(dist_info) is None


def test_compute_package_checksum_empty_record_returns_none(tmp_path: Path) -> None:
    """RECORD 仅含自身条目（无其他文件）时返回 None."""
    site_packages = tmp_path / "sp"
    site_packages.mkdir()
    dist_info = _make_dist_info(site_packages, "pkg", "1.0", record_entries=[])

    assert _compute_package_checksum(dist_info) is None


def test_compute_package_checksum_skips_missing_files(tmp_path: Path) -> None:
    """RECORD 引用了不存在的文件时跳过，仍能计算剩余文件的校验和."""
    site_packages = tmp_path / "sp"
    site_packages.mkdir()
    dist_info = _make_dist_info(
        site_packages,
        "pkg",
        "1.0",
        record_entries=[("pkg/mod.py", b"v\n")],
    )
    # 在 RECORD 中追加一条不存在的文件引用
    record_path = dist_info / "RECORD"
    record_path.write_text(
        record_path.read_text(encoding="utf-8") + "\nmissing/file.py,sha256=abc,10",
        encoding="utf-8",
    )

    checksum = _compute_package_checksum(dist_info)

    # 仍能计算（仅 pkg/mod.py 参与）
    assert checksum is not None
    assert len(checksum) == 64


# ---- _sanitize_spdx_id 测试 ----


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("requests", "requests"),
        ("PySide6_Essentials", "pyside6-essentials"),
        ("ordered-set", "ordered-set"),
        ("my.package", "my-package"),
        ("UPPER_CASE", "upper-case"),
        ("Mixed.Case_Pkg", "mixed-case-pkg"),
        ("pkg with spaces", "pkg-with-spaces"),
        ("pkg!@#name", "pkgname"),
    ],
    ids=[
        "simple",
        "underscore_to_dash",
        "already_dash",
        "dot_to_dash",
        "upper_to_lower",
        "mixed_separators",
        "spaces_to_dash",
        "special_chars_removed",
    ],
)
def test_sanitize_spdx_id(name: str, expected: str) -> None:
    """_sanitize_spdx_id 将包名转为小写字母数字与连字符的 SPDX ID."""
    assert _sanitize_spdx_id(name) == expected


def test_sanitize_spdx_id_empty_string_fallback() -> None:
    """空字符串（或仅特殊字符）回退到 'package'."""
    assert _sanitize_spdx_id("") == "package"
    assert _sanitize_spdx_id("!@#") == "package"


# ---- SbomPackage dataclass 测试 ----


def test_sbom_package_dataclass_defaults() -> None:
    """SbomPackage 默认值：checksums 空、supplier 为 NOASSERTION."""
    pkg = SbomPackage(
        name="pkg",
        version="1.0",
        spdx_id="SPDXRef-Package-pkg",
        download_location="NOASSERTION",
        files_analyzed=False,
        license_concluded="NOASSERTION",
        license_declared="NOASSERTION",
    )
    assert pkg.checksums == []
    assert pkg.supplier == "NOASSERTION"


# ---- 集成：collect_sbom + 多包场景 ----


def test_collect_sbom_multiple_packages_sorted(tmp_path: Path) -> None:
    """collect_sbom 收集多个包并按 dist-info 目录名排序."""
    dist, site_packages = _make_dist_with_site_packages(tmp_path)
    _make_dist_info(
        site_packages,
        "zzz-lib",
        "1.0",
        metadata="License: MIT\n",
        record_entries=[("zzz/__init__.py", b"z\n")],
    )
    _make_dist_info(
        site_packages,
        "aaa-lib",
        "2.0",
        metadata="License-Expression: Apache-2.0\n",
        record_entries=[("aaa/__init__.py", b"a\n")],
    )
    info = _make_info(tmp_path)

    sbom = collect_sbom(dist, info)

    packages = sbom["packages"]
    assert len(packages) == 2
    # 排序后 aaa-lib 在前
    assert packages[0]["name"] == "aaa-lib"
    assert packages[1]["name"] == "zzz-lib"


def test_collect_sbom_package_without_metadata_skipped(tmp_path: Path) -> None:
    """dist-info 缺 METADATA 文件时跳过该包."""
    dist, site_packages = _make_dist_with_site_packages(tmp_path)
    # 构造仅含 RECORD 的 dist-info（缺 METADATA）
    bad = site_packages / "broken-1.0.dist-info"
    bad.mkdir(parents=True)
    (bad / "RECORD").write_text("RECORD,,", encoding="utf-8")
    # 正常包
    _make_dist_info(site_packages, "good", "1.0", metadata="License: MIT\n")
    info = _make_info(tmp_path)

    sbom = collect_sbom(dist, info)

    packages = sbom["packages"]
    assert len(packages) == 1
    assert packages[0]["name"] == "good"


def test_collect_sbom_no_checksum_when_no_record(tmp_path: Path) -> None:
    """dist-info 无 RECORD 时 files_analyzed=False 且 checksums 为空."""
    dist, site_packages = _make_dist_with_site_packages(tmp_path)
    dist_info = site_packages / "norecord-1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: norecord\nVersion: 1.0\nLicense: MIT\n",
        encoding="utf-8",
    )
    info = _make_info(tmp_path)

    sbom = collect_sbom(dist, info)

    packages = sbom["packages"]
    assert len(packages) == 1
    pkg = packages[0]
    assert pkg["files_analyzed"] is False
    assert pkg["checksums"] == []
    # license 仍能从 METADATA 解析
    assert pkg["license_concluded"] == "MIT"
