"""SBOM（软件物料清单）生成：SPDX 2.3 兼容 JSON.

在 :func:`fspack.packaging.pipeline.build` 完成后扫描 ``dist`` 下 site-packages
的 ``*.dist-info`` 目录，提取每个依赖的名称/版本/许可证/SHA256，生成 SPDX 2.3
兼容 JSON 文件到 ``dist/release/<name>-<version>-sbom.json``，便于审计与合规检查。

公共 API：

- :func:`generate_sbom` — 扫描 dist 目录生成 SBOM JSON 文件，返回路径
- :func:`collect_sbom` — 扫描 dist 目录返回结构化 SBOM 数据（便于测试）
- :class:`SbomPackage` — 单个依赖包的 SBOM 条目数据类

SPDX 2.3 字段映射：

- ``spdxVersion``: 固定 ``"SPDX-2.3"``
- ``dataLicense``: 固定 ``"CC0-1.0"``（SPDX 规范要求）
- ``SPDXID``: ``"SPDXRef-DOCUMENT"``
- ``name``: 项目名（来自 :class:`ProjectInfo`）
- ``documentNamespace``: ``https://fspack.dev/spdx/<name>-<version>-<uuid>``
- ``creationInfo``: 创建时间与工具信息
- ``packages``: 依赖包列表（含 name/version/license/sha256/downloadLocation）

许可证来源（按优先级）：

1. ``METADATA`` 文件 ``License-Expression:`` 字段（PEP 639，SPDX 表达式）
2. ``METADATA`` 文件 ``License:`` 字段（古典 License 字段，可能含非 SPDX 文本）
3. ``License-File:`` 字段存在时返回 ``"LicenseRef-<name>"`` 指向随包分发许可证文件
4. 都无时返回 ``"NOASSERTION"``（SPDX 规范定义的"无法判断"标识）

SHA256 计算：扫描 ``*.dist-info/RECORD`` 文件列出所有文件，按相对路径排序后
对每个文件计算 SHA256 并拼接，最终对拼接结果计算 SHA256 作为包的整体校验和。
此方式避免逐文件枚举到 SPDX ``files`` 数组（数百文件时 JSON 体积过大），
同时保留可验证性（RECORD 自身已被签名 wheel 校验）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fspack._util.fsutil import atomic_write_text
from fspack.config import ProjectInfo
from fspack.packaging.site_packages import find_site_packages

__all__ = [
    "SbomPackage",
    "collect_sbom",
    "generate_sbom",
]

_logger = logging.getLogger(__name__)

# SPDX 规范常量
_SPDX_VERSION = "SPDX-2.3"
_DATA_LICENSE = "CC0-1.0"
_DOCUMENT_SPDXID = "SPDXRef-DOCUMENT"
_TOOL_NAME = "fspack"
_NOASSERTION = "NOASSERTION"

# METADATA 字段正则：``License-Expression:`` 与 ``License:``（PEP 639 vs 古典）
# License-Expression 优先（PEP 639 标准化 SPDX 表达式），License 次之（可能含自由文本）
_LICENSE_EXPR_RE = re.compile(r"^License-Expression:\s*(.+?)\s*$", re.MULTILINE)
_LICENSE_RE = re.compile(r"^License:\s*(.+?)\s*$", re.MULTILINE)
_LICENSE_FILE_RE = re.compile(r"^License-File:\s*(.+?)\s*$", re.MULTILINE)

# dist-info 目录名解析：``<name>-<version>.dist-info``
# 从右侧分离 version（最后一个 - 之后的部分），name 可能含连字符
_DIST_INFO_NAME_RE = re.compile(r"^(.+)-([^-]+)\.dist-info$")


@dataclass(frozen=True)
class SbomPackage:
    """单个依赖包的 SBOM 条目（对应 SPDX ``packages[]`` 数组元素）."""

    name: str
    version: str
    spdx_id: str
    download_location: str
    files_analyzed: bool
    license_concluded: str
    license_declared: str
    checksums: list[dict[str, str]] = field(default_factory=list)
    supplier: str = "NOASSERTION"


def collect_sbom(dist_dir: Path, info: ProjectInfo) -> dict[str, Any]:
    """扫描 dist 目录收集 SBOM 数据，返回 SPDX 2.3 兼容字典.

    扫描 ``dist/site-packages`` 下的所有 ``*.dist-info`` 目录，提取依赖元信息。
    每个包计算整体 SHA256（基于 RECORD 列出文件的内容拼接哈希），license 从
    METADATA 解析。多包时逐包 ``ThreadPoolExecutor`` 并行哈希（结果保序）。

    Args:
        dist_dir: dist 根目录（``dist/``）
        info: 项目元信息（用于 SPDX 文档名与命名空间）

    Returns:
        SPDX 2.3 兼容字典，可直接 ``json.dumps`` 为 SBOM 文件
    """
    site_packages = find_site_packages(dist_dir)
    packages: list[SbomPackage] = []
    if site_packages is not None:
        dist_infos = sorted(d for d in site_packages.glob("*.dist-info") if d.is_dir())
        if dist_infos:
            # 逐包并行：_sha256_file 分块哈希释放 GIL（hashlib 对 >2KB 块），
            # 文件读取为 I/O 等待，线程是真并行。单包退化为直接调用避免
            # 线程池开销。executor.map 保序，packages 顺序与串行一致。
            from concurrent.futures import ThreadPoolExecutor

            if len(dist_infos) == 1:
                pkg = _parse_dist_info(dist_infos[0])
                if pkg is not None:
                    packages.append(pkg)
            else:
                with ThreadPoolExecutor(max_workers=min(len(dist_infos), 8)) as pool:
                    for pkg in pool.map(_parse_dist_info, dist_infos):
                        if pkg is not None:
                            packages.append(pkg)

    # 文档命名空间：含 UUID 避免同项目多次构建命名空间冲突
    namespace = f"https://fspack.dev/spdx/{info.name}-{info.version}-{uuid.uuid4()}"
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "spdxVersion": _SPDX_VERSION,
        "dataLicense": _DATA_LICENSE,
        "SPDXID": _DOCUMENT_SPDXID,
        "name": info.name,
        "documentNamespace": namespace,
        "creationInfo": {
            "created": created,
            "creators": [f"Tool: {_TOOL_NAME}"],
        },
        "packages": [asdict(p) for p in packages],
    }


def generate_sbom(dist_dir: Path, info: ProjectInfo) -> Path:
    """生成 SBOM JSON 文件到 ``dist/release/<name>-<version>-sbom.json``.

    Args:
        dist_dir: dist 根目录
        info: 项目元信息

    Returns:
        生成的 SBOM 文件路径
    """
    import json

    release_dir = dist_dir / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    sbom_path = release_dir / f"{info.name}-{info.version}-sbom.json"
    sbom_data = collect_sbom(dist_dir, info)
    atomic_write_text(sbom_path, json.dumps(sbom_data, ensure_ascii=False, indent=2))
    _logger.info("SBOM 已生成: %s（%d 个包）", sbom_path, len(sbom_data["packages"]))
    return sbom_path


def _parse_dist_info(dist_info: Path) -> SbomPackage | None:
    """解析单个 ``*.dist-info`` 目录为 :class:`SbomPackage`.

    从目录名 ``<name>-<version>.dist-info`` 提取 name/version，从 METADATA
    文件提取 license，从 RECORD 文件计算包整体 SHA256。

    Returns:
        SBOM 包条目；METADATA 不存在或目录名不匹配时返回 None
    """
    name_match = _DIST_INFO_NAME_RE.match(dist_info.name)
    if not name_match:
        _logger.debug("跳过非标准 dist-info 目录名: %s", dist_info.name)
        return None

    pkg_name, pkg_version = name_match.group(1), name_match.group(2)
    metadata_path = dist_info / "METADATA"
    if not metadata_path.is_file():
        _logger.debug("dist-info 缺 METADATA 文件: %s", dist_info.name)
        return None

    metadata_text = metadata_path.read_text(encoding="utf-8", errors="replace")
    license_concluded = _extract_license(metadata_text, pkg_name)
    checksum = _compute_package_checksum(dist_info)
    spdx_id = f"SPDXRef-Package-{_sanitize_spdx_id(pkg_name)}"

    return SbomPackage(
        name=pkg_name,
        version=pkg_version,
        spdx_id=spdx_id,
        download_location="NOASSERTION",
        files_analyzed=bool(checksum),
        license_concluded=license_concluded,
        license_declared=license_concluded,
        checksums=[{"algorithm": "SHA256", "checksumValue": checksum}] if checksum else [],
    )


def _extract_license(metadata_text: str, pkg_name: str) -> str:
    """从 METADATA 文本提取许可证标识.

    优先级：``License-Expression:`` (PEP 639) > ``License:`` (古典) >
    ``License-File:`` 存在 > ``NOASSERTION``。

    ``License-File`` 存在时返回 ``LicenseRef-<pkg>`` 指向随包分发的许可证文件，
    SPDX 规范允许 ``LicenseRef-*`` 自定义引用。
    """
    m = _LICENSE_EXPR_RE.search(metadata_text)
    if m:
        return m.group(1).strip()

    m = _LICENSE_RE.search(metadata_text)
    if m:
        text = m.group(1).strip()
        # License 字段可能含自由文本（非 SPDX 标识），无法机器判断时用 NOASSERTION
        # 单行短字符串视为 SPDX 标识（如 "MIT"），多行或含分号视为自由文本
        if "\n" not in text and len(text) <= 80:
            return text
        return _NOASSERTION

    if _LICENSE_FILE_RE.search(metadata_text):
        return f"LicenseRef-{_sanitize_spdx_id(pkg_name)}"

    return _NOASSERTION


def _sha256_file(path: Path) -> str | None:
    """分块（64KB）计算文件 SHA256，避免大文件 ``read_bytes`` 整体读入内存.

    Returns:
        64 字符十六进制字符串；文件缺失/不可读（含路径为目录的场景）返回
        ``None``，调用方跳过该文件（单文件失败不应放弃整个包摘要）
    """
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _compute_package_checksum(dist_info: Path) -> str | None:
    """计算包整体 SHA256 校验和（基于 RECORD 列出文件的内容拼接）.

    RECORD 文件格式：``<path>,<hash>,<size>``，每行一个文件。读取 RECORD
    列出的所有文件（跳过 RECORD 自身），按相对路径排序后对每个文件内容计算
    SHA256，将所有 SHA256 拼接后对拼接字符串计算 SHA256 作为包整体校验和。

    此方式平衡了验证性与 JSON 体积：逐文件枚举到 SPDX ``files`` 数组在数百
    文件时 JSON 体积过大，整体校验和则保持单字段简洁。

    Returns:
        64 字符 SHA256 十六进制字符串；RECORD 不存在或无文件时返回 None
    """
    record_path = dist_info / "RECORD"
    if not record_path.is_file():
        return None

    # 解析 RECORD：每行 ``<path>,<hash>,<size>``，path 可能含逗号（用 csv 更稳）
    import csv

    file_hashes: list[tuple[str, str]] = []
    try:
        with record_path.open(encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 2:
                    continue
                rel_path = row[0]
                # 跳过 RECORD 自身（wheel 规范要求 RECORD 列出自身但 hash 为空）
                if rel_path == "RECORD":
                    continue
                abs_path = dist_info.parent / rel_path
                file_hash = _sha256_file(abs_path)
                if file_hash is None:
                    continue
                file_hashes.append((rel_path, file_hash))
    except (OSError, UnicodeDecodeError):
        # RECORD 非 UTF-8（损坏/异常 wheel）时 csv 迭代触发 UnicodeDecodeError
        # （ValueError 子类，非 OSError）：视为无法生成 SBOM 摘要，返回 None 不崩溃。
        return None

    if not file_hashes:
        return None

    # 按相对路径排序保证可重现性
    file_hashes.sort(key=lambda x: x[0])
    concatenated = "\n".join(f"{rel}:{h}" for rel, h in file_hashes)
    return hashlib.sha256(concatenated.encode("utf-8")).hexdigest()


def _sanitize_spdx_id(name: str) -> str:
    """将包名转为 SPDX ID 合法字符（字母数字与 ``-``，``_``/``.`` 替换为 ``-``）.

    SPDX 规范要求 SPDXID 仅含 ``[A-Za-z0-9.-]``，包名可能含下划线或大写字母，
    统一转为小写并用 ``-`` 分隔。例如 ``PySide6_Essentials`` → ``pyside6-essentials``。
    """
    sanitized = re.sub(r"[_.\s]+", "-", name.lower())
    sanitized = re.sub(r"[^a-z0-9-]", "", sanitized)
    return sanitized or "package"
