"""Win7 重编译版 python3XX.dll 清单驱动下载与双重校验.

背景（win7_check 模块结论）：Python 3.12+ 官方 python3XX.dll 静态导入 kernel32
的 Win8+ API（CopyFile2/Pss*/GetSystemTimePreciseAsFileTime），shim 注入失效；
可行方案是用 adang1345 重编译版（上游仓库 PythonWin7，现更名 PythonVista）
仅替换 python3XX.dll，patch 版本须与官方 embed 完全一致（ABI 兼容前提）。

重编译版 dll 每个 ~6MB，直入 git 仓库会使历史永久膨胀（二进制无 delta 压缩）。
本模块以"清单驱动构建期下载"替代入库：

- 仓库仅存 :data:`WIN7_EMBED_SHA256` 清单（版本 → embed zip sha256，取自
  GitHub release asset digest，升级版本时改一行可 review 的 diff）；
- 构建期按需下载 win7 embed zip 到缓存目录（gitignored），命中缓存复用；
- 下载后双重门禁：zip sha256 校验（完整性）+ :func:`check_win7_imports`
  导入表校验（Win7 兼容性，含内置 shim 导出覆盖）——即使上游发布损坏或
  被污染的 dll 也会被拦截。
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from fspack._compat import override
from fspack.exceptions import FspackError
from fspack.packaging.runtime.download import RuntimeDownloader
from fspack.packaging.runtime.urls import embed_dirname, embed_zip_name
from fspack.packaging.win7.check import PeParseError, Win7CheckResult, check_win7_imports

if TYPE_CHECKING:
    from fspack.progress import StageRecorder

__all__ = [
    "WIN7_EMBED_SHA256",
    "WIN7_SHIM_DLL_PATH",
    "Win7DllError",
    "Win7EmbedRuntime",
    "download_win7_embed",
    "ensure_win7_dll",
    "extract_win7_dll",
    "needs_win7_dll",
    "win7_dll_name",
    "win7_zip_cache_name",
    "win7_zip_url",
]

_logger = logging.getLogger(__name__)

# GitHub releases 直连（上游仓库已由 PythonWin7 更名为 PythonVista，旧名 URL 仍
# 302 跳转）。国内镜像（阿里云 github/releases）未收录此 repo，实测 404。
WIN7_PYTHON_BASE_URL = "https://github.com/adang1345/PythonVista"

# 清单：完整版本 → win7 embed-amd64 zip 的 sha256（GitHub release asset digest）。
# 版本须与 config.KNOWN_EMBED_VERSIONS 对齐（"只替换 dll"要求 patch 完全一致）；
# 升级版本时同步更新两处并复核 win7.check 通过。
WIN7_EMBED_SHA256: dict[str, str] = {
    "3.12.10": "6f4e1a6c607aaac0b052c9f8962a863ae23ddeab4502619dc0a151cf1bca1a60",
    "3.13.14": "bc02b825b073087c542c9c7158d85fb81ed35fd971efdf8f20223adb1b1add1d",
    "3.14.6": "ff2345af4334a6c5e122b92512146e984b00fd55ace53dda00dc9cdefcfbb1c9",
}

# 内置 api-ms-win-core-path shim（随 fspack 分发），dll 导入表校验时验证导出覆盖。
# 公开常量：win7.scan 全量扫描复用同一 shim 做覆盖校验。
WIN7_SHIM_DLL_PATH = Path(__file__).parent.parent.parent / "assets" / "runtime" / "api-ms-win-core-path-l1-1-0.dll"


class Win7DllError(FspackError):
    """win7 python3XX.dll 获取或校验失败（清单未收录、zip 损坏、导入表违规等）。"""


def needs_win7_dll(py_version: str) -> bool:
    """该版本官方 python3XX.dll 是否含 Win8+ 静态导入、需替换为重编译版.

    3.9–3.11 官方 dll 仅缺 api-ms-win-core-path（shim 注入即可）；3.12 起
    另含 kernel32 的 Win8+/Win8.1+ API，必须替换 dll。
    """
    parts = py_version.split(".")
    return (int(parts[0]), int(parts[1])) >= (3, 12)


def win7_dll_name(version: str) -> str:
    """返回该版本的 python3XX.dll 文件名（如 ``3.12.10`` → ``python312.dll``）."""
    return f"{embed_dirname(version)}.dll"


def win7_zip_url(version: str) -> str:
    """返回 win7 embed zip 的下载 URL（GitHub releases download 路径）."""
    return f"{WIN7_PYTHON_BASE_URL}/releases/download/v{version}/{embed_zip_name(version)}"


def win7_zip_cache_name(version: str) -> str:
    """返回 win7 embed zip 的本地缓存文件名.

    上游资产与官方 embed zip 同名（``python-{version}-embed-amd64.zip``），
    加 ``-win7`` 后缀避免与官方 zip 在同一缓存目录互相覆盖。
    """
    return embed_zip_name(version).replace(".zip", "-win7.zip")


class Win7EmbedRuntime(RuntimeDownloader):
    """win7 重编译版 embed python 下载器（仅提取 python3XX.dll，其余组件用官方原件）."""

    runtime_label = "win7 python embed"

    @classmethod
    @override
    def archive_name(cls, version: str, **kwargs: object) -> str:  # noqa: ARG003
        return win7_zip_cache_name(version)

    @classmethod
    @override
    def download_url(cls, version: str, **kwargs: object) -> str:  # noqa: ARG003
        return win7_zip_url(version)

    @classmethod
    @override
    def marker_path(cls, runtime_dir: Path, version: str) -> Path:
        return runtime_dir / win7_dll_name(version)

    @classmethod
    @override
    def extract_archive(cls, archive_path: Path, runtime_dir: Path) -> None:
        with zipfile.ZipFile(archive_path) as zf:
            _extract_dll_member(zf, runtime_dir, _dll_member_version(archive_path))


def _dll_member_version(archive_path: Path) -> str:
    """从缓存文件名 ``python-3.12.10-embed-amd64-win7.zip`` 解析版本号."""
    stem = archive_path.name.split("-")[1]
    return stem


def _extract_dll_member(zf: zipfile.ZipFile, dest_dir: Path, version: str) -> Path:
    """从已打开的 zip 提取 python3XX.dll 到 dest_dir，返回 dll 路径.

    zip 内无该成员或 zip 损坏时抛 :class:`Win7DllError`。dll 经临时文件
    原子替换落盘，避免中断留下半写文件被后续构建误判为就绪。
    """
    dll_name = win7_dll_name(version)
    try:
        data = zf.read(dll_name)
    except KeyError as exc:
        raise Win7DllError(f"win7 embed zip 内缺少 {dll_name}: {zf.filename}") from exc
    except zipfile.BadZipFile as exc:
        raise Win7DllError(f"win7 embed zip 损坏: {zf.filename} -> {exc}") from exc
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / dll_name
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


def download_win7_embed(version: str, cache_dir: Path, *, stage: StageRecorder | None = None) -> Path:
    """按清单 sha256 下载 win7 embed zip 到缓存目录，已缓存且哈希匹配则复用.

    版本未收录清单时抛 :class:`Win7DllError`（防止下载不可信的未知版本）。
    """
    expected = WIN7_EMBED_SHA256.get(version)
    if expected is None:
        known = "、".join(sorted(WIN7_EMBED_SHA256))
        raise Win7DllError(
            f"Python {version} 不在 win7 重编译版清单（收录: {known}），"
            f"版本须与 KNOWN_EMBED_VERSIONS 对齐且 patch 完全一致，请更新 WIN7_EMBED_SHA256"
        )
    return Win7EmbedRuntime.download(version, cache_dir, stage=stage, expected_hash=expected)


def extract_win7_dll(zip_path: Path, dest_dir: Path, version: str) -> Path:
    """从 win7 embed zip 提取 python3XX.dll 到 dest_dir，zip 损坏时删除缓存并抛错."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            dll = _extract_dll_member(zf, dest_dir, version)
    except zipfile.BadZipFile as exc:
        zip_path.unlink(missing_ok=True)
        raise Win7DllError(f"win7 embed zip 损坏，已删除缓存: {zip_path} -> {exc}") from exc
    _logger.info("提取 %s: %s", win7_dll_name(version), dll)
    return dll


def _check_dll(dll: Path) -> Win7CheckResult:
    """校验 dll 导入表 Win7 兼容性（含内置 shim 导出覆盖），违规抛 Win7DllError."""
    try:
        result = check_win7_imports(dll, shim=WIN7_SHIM_DLL_PATH)
    except PeParseError as exc:
        raise Win7DllError(f"{dll.name} 不是合法 PE 镜像: {exc}") from exc
    if not result.ok:
        detail = "；".join(f"{v.target}（{v.reason}）" for v in result.violations)
        raise Win7DllError(f"{dll.name} 导入表含 Win8+ 依赖，不能用于 Win7: {detail}")
    return result


def ensure_win7_dll(
    version: str,
    cache_dir: Path,
    dest_dir: Path,
    *,
    stage: StageRecorder | None = None,
    replace_invalid: bool = False,
) -> Path:
    """确保 dest_dir 内有经双重校验的 win7 重编译版 python3XX.dll，返回 dll 路径.

    dest_dir 已有 dll 时重新校验导入表后复用（防误换/篡改，~6MB 解析耗时可忽略）；
    否则按清单下载 zip（sha256 校验）→ 提取 dll → 导入表校验。

    Args:
        version: 完整 Python 版本号（须收录 :data:`WIN7_EMBED_SHA256`）。
        cache_dir: win7 embed zip 下载缓存目录。
        dest_dir: dll 目标目录（runtime 根目录）。
        stage: 可选进度记录器（缓存命中/下载字节自动回写）。
        replace_invalid: dest_dir 已有 dll 且校验不通过时的处理——False（默认）
            抛错（独立调用时透明暴露篡改）；True 删除后重新下载提取。打包
            pipeline 传 True：官方 embed 解压出的 python3XX.dll 必然含 Win8+
            导入，需静默替换为重编译版而非报错。
    """
    dll = dest_dir / win7_dll_name(version)
    if dll.is_file():
        try:
            _check_dll(dll)
        except Win7DllError:
            if not replace_invalid:
                raise
            _logger.info("%s 校验未通过（官方 dll 或已损坏），删除后重新替换", dll.name)
            dll.unlink(missing_ok=True)
        else:
            _logger.info("win7 python dll 已就绪并校验通过: %s", dll)
            if stage is not None:
                stage.hit_cache()
            return dll
    zip_path = download_win7_embed(version, cache_dir, stage=stage)
    dll = extract_win7_dll(zip_path, dest_dir, version)
    try:
        result = _check_dll(dll)
    except Win7DllError:
        dll.unlink(missing_ok=True)
        raise
    _logger.info("win7 python dll 校验通过: %s（需 shim: %s）", dll, ", ".join(result.shim_dlls) or "无")
    return dll
