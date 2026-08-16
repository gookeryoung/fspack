"""Win7 兼容自检：版本清单对齐、shim 资产与缓存 zip 哈希抽检.

``fsp doctor`` 在 Windows 平台追加的诊断项（P3 诊断与守卫），前置发现
Win7 打包链路的环境问题，避免打包中途失败：

- **清单对齐**：``KNOWN_EMBED_VERSIONS`` 的 3.12+ 版本必须收录
  ``WIN7_EMBED_SHA256``（缺失属 fspack 版本升级遗漏，ERROR）
- **shim 资产**：内置 ``api-ms-win-core-path-l1-1-0.dll`` 必须随包分发
  （缺失属安装损坏，3.9+ 打包必需，ERROR）
- **缓存抽检**：``~/.fspack/cache/win7-dll/`` 下已缓存的 embed zip 逐个
  sha256 核对（损坏仅 WARN：删除后下次构建自动重新下载即可自愈）
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from fspack.config import KNOWN_EMBED_VERSIONS
from fspack.config.cache import win7_dll_cache_dir
from fspack.doctor.models import CheckResult, CheckStatus
from fspack.packaging.win7_dll import WIN7_EMBED_SHA256, WIN7_SHIM_DLL_PATH, win7_zip_cache_name

__all__ = ["_check_win7_compat"]

_logger = logging.getLogger(__name__)

# sha256 流式读取块大小（64KB，兼顾内存与调用次数）
_HASH_CHUNK = 1 << 16


def _file_sha256(path: Path) -> str:
    """流式计算文件 sha256（hex 小写），避免大 zip 整读内存."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _check_win7_compat() -> CheckResult:
    """Win7 兼容三查：清单对齐 / shim 资产 / 缓存 zip 哈希抽检.

    全部通过返回 OK；清单缺失或 shim 资产缺失为 ERROR（阻断 Win7 打包）；
    缓存 zip 哈希不匹配为 WARN（删除后重新构建即可自愈）。
    """
    errors: list[str] = []
    warns: list[str] = []
    cached = 0

    # 1. 清单对齐：KNOWN_EMBED_VERSIONS 的 3.12+ 必须收录 win7 清单
    expected = {
        full for minor, full in KNOWN_EMBED_VERSIONS.items() if tuple(int(x) for x in minor.split(".")) >= (3, 12)
    }
    missing = expected - set(WIN7_EMBED_SHA256)
    if missing:
        errors.append(f"win7 清单缺失版本 {sorted(missing)}（fspack 版本升级遗漏）")

    # 2. shim 资产：3.9+ 打包注入必需
    if not WIN7_SHIM_DLL_PATH.is_file():
        errors.append(f"内置 shim 缺失: {WIN7_SHIM_DLL_PATH.name}（重装 fspack 修复）")

    # 3. 缓存抽检：清单内版本的已缓存 zip 逐个核对哈希
    cache_dir = win7_dll_cache_dir()
    for version, expected_sha in WIN7_EMBED_SHA256.items():
        zip_path = cache_dir / win7_zip_cache_name(version)
        if not zip_path.is_file():
            continue
        cached += 1
        try:
            actual = _file_sha256(zip_path)
        except OSError as exc:
            warns.append(f"{zip_path.name} 读取失败: {exc}")
            continue
        if actual != expected_sha:
            warns.append(f"{zip_path.name} 哈希不匹配（删除后重新构建自动重下）")

    if errors:
        return CheckResult(
            name="Win7 兼容",
            status=CheckStatus.ERROR,
            detail="；".join(errors + warns) or "存在阻断性问题",
            suggestion="升级 fspack 到最新版本；安装损坏时 pip install --force-reinstall fspack",
        )
    if warns:
        return CheckResult(
            name="Win7 兼容",
            status=CheckStatus.WARN,
            detail=f"清单 {len(WIN7_EMBED_SHA256)} 版本对齐；shim 就绪；" + "；".join(warns),
            suggestion="删除哈希不匹配的缓存 zip（下次构建自动重新下载）",
        )
    cache_note = f"缓存 {cached} 个 zip 校验通过" if cached else "暂无缓存（首次打包自动下载）"
    return CheckResult(
        name="Win7 兼容",
        status=CheckStatus.OK,
        detail=f"清单 {len(WIN7_EMBED_SHA256)} 版本对齐；shim 就绪；{cache_note}",
    )
