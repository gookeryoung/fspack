"""内置库打包：为 embed python 补充缺失的 stdlib 模块。

Windows embed python 是最小化子集，不含 tkinter（纯 Python 包 + ``_tkinter.pyd``
C 扩展 + Tcl/Tk 运行时脚本）。Linux standalone 已含全部 stdlib，无需补充。

从 python-build-standalone Windows 构建提取 tkinter 组件，按版本缓存 zip，
避免每次构建重复下载 40MB tarball。
"""

from __future__ import annotations

import io
import logging
import re
import tarfile
import zipfile
from pathlib import Path

from fspack.exceptions import BuiltinError
from fspack.packaging.net import Downloader
from fspack.packaging.runtime import STANDALONE_BASE_URL, STANDALONE_RELEASE_TAG
from fspack.platform import Platform
from fspack.progress import StageRecorder

__all__ = ["TkinterBundler"]

_logger = logging.getLogger(__name__)

# 匹配 tcl8.6 / tcl9.0 等版本目录
_TCL_DIR_RE = re.compile(r"/(tcl\d+\.\d+)/")
_TK_DIR_RE = re.compile(r"/(tk\d+\.\d+)/")


class TkinterBundler:
    """tkinter 内置库打包器.

    从 python-build-standalone Windows 构建提取 tkinter 组件（纯 Python 包、
    ``_tkinter.pyd`` C 扩展、Tcl/Tk 运行时脚本），补充到 embed python runtime。

    缓存策略：首次下载 ~40MB tarball，提取 tkinter 组件为 ~3-5MB zip 缓存；
    后续构建直接解压缓存的 zip（秒级）。
    """

    @staticmethod
    def standalone_windows_tarball_name(version: str, release_tag: str) -> str:
        """返回 python-build-standalone Windows tarball 文件名。"""
        return f"cpython-{version}+{release_tag}-x86_64-pc-windows-msvc-shared-install_only.tar.gz"

    @staticmethod
    def standalone_windows_url(version: str, release_tag: str) -> str:
        """返回 python-build-standalone Windows 构建下载 URL。"""
        return f"{STANDALONE_BASE_URL}/{release_tag}/{TkinterBundler.standalone_windows_tarball_name(version, release_tag)}"

    @classmethod
    def is_needed(cls, ast_stdlib: tuple[str, ...], target: Platform) -> bool:
        """检测项目是否使用 tkinter 且目标为 Windows embed。"""
        return target is Platform.WINDOWS and "tkinter" in ast_stdlib

    @classmethod
    def ensure(cls, runtime_dir: Path, version: str, cache_dir: Path, stage: StageRecorder) -> None:
        """确保 tkinter 在 runtime 中可用（缓存优先）。

        1. 检查 ``runtime/Lib/tkinter/__init__.py`` 是否已存在 → 命中跳过
        2. 检查 ``cache/tkinter/tkinter-{version}.zip`` 是否已缓存 → 解压到 runtime
        3. 下载 Windows standalone tarball → 提取 tkinter → 生成缓存 zip → 解压到 runtime
        """
        tkinter_marker = runtime_dir / "Lib" / "tkinter" / "__init__.py"
        if tkinter_marker.is_file():
            stage.hit_cache()
            stage.set_detail("tkinter 已就绪")
            _logger.info("tkinter 打包: runtime 已含 tkinter，跳过")
            return

        tkinter_cache_dir = cache_dir / "tkinter"
        tkinter_cache_dir.mkdir(parents=True, exist_ok=True)
        cache_zip = tkinter_cache_dir / f"tkinter-{version}.zip"

        if cache_zip.is_file():
            stage.set_detail("从缓存解压 tkinter")
            _logger.info("tkinter 打包: 从缓存解压 %s", cache_zip.name)
            cls._unpack_tkinter_zip(cache_zip, runtime_dir)
            stage.processed(1)
            return

        # 下载 Windows standalone tarball
        standalone_windows_cache = cache_dir / "standalone-windows"
        standalone_windows_cache.mkdir(parents=True, exist_ok=True)
        tarball_path = standalone_windows_cache / cls.standalone_windows_tarball_name(version, STANDALONE_RELEASE_TAG)

        if not tarball_path.is_file():
            url = cls.standalone_windows_url(version, STANDALONE_RELEASE_TAG)
            _logger.info("tkinter 打包: 下载 Windows standalone 构建")
            downloader = Downloader()
            downloader.download(url, tarball_path, stage=stage, label=f"standalone-windows {version}")
        else:
            stage.hit_cache()
            _logger.info("tkinter 打包: standalone tarball 已缓存")

        # 从 tarball 提取 tkinter 组件，生成缓存 zip
        _logger.info("tkinter 打包: 从 tarball 提取 tkinter 组件")
        zip_data = cls._build_tkinter_zip(tarball_path)
        cache_zip.write_bytes(zip_data)
        stage.processed(1)
        stage.set_detail("tkinter")

        # 解压到 runtime
        cls._unpack_tkinter_zip(cache_zip, runtime_dir)

    @staticmethod
    def _build_tkinter_zip(tar_path: Path) -> bytes:
        """从 standalone tarball 提取 tkinter 组件，返回 zip 字节流。

        提取四类文件并映射到 runtime 目标结构：

        - ``.../tkinter/**`` → ``Lib/tkinter/...``（纯 Python 包）
        - ``.../_tkinter*.pyd`` → ``_tkinter.pyd``（C 扩展，根目录）
        - ``.../tcl{ver}/...`` → ``tcl/tcl{ver}/...``（Tcl 运行时）
        - ``.../tk{ver}/...`` → ``tcl/tk{ver}/...``（Tk 运行时）
        """
        with tarfile.open(tar_path, "r:gz") as tar:
            members = tar.getmembers()

            # 定位 tkinter 包目录前缀（如 python/install/Lib 或 python/install/lib/python3.11）
            tkinter_prefix = ""
            for m in members:
                if m.name.endswith("/tkinter/__init__.py"):
                    tkinter_prefix = m.name[: m.name.rfind("/tkinter/__init__.py")]
                    break
            if not tkinter_prefix:
                raise BuiltinError("在 standalone tarball 中未找到 tkinter 包")

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for m in members:
                    if not m.isfile():
                        continue
                    name = m.name
                    f = tar.extractfile(m)
                    if f is None:
                        continue
                    data = f.read()

                    # tkinter 包 → Lib/tkinter/...
                    if name.startswith(f"{tkinter_prefix}/tkinter/"):
                        rel = name[len(tkinter_prefix) + 1 :]  # tkinter/...
                        zf.writestr(f"Lib/{rel}", data)
                        continue

                    # _tkinter*.pyd → 根目录（重命名为 _tkinter.pyd）
                    if "_tkinter" in name and name.endswith(".pyd"):
                        zf.writestr("_tkinter.pyd", data)
                        continue

                    # tcl{ver}/ → tcl/tcl{ver}/...
                    tcl_match = _TCL_DIR_RE.search(name)
                    if tcl_match:
                        tcl_ver_dir = tcl_match.group(1)  # e.g. tcl8.6
                        idx = name.find(f"/{tcl_ver_dir}/")
                        rel = name[idx + 1 :]  # tcl8.6/...
                        zf.writestr(f"tcl/{rel}", data)
                        continue

                    # tk{ver}/ → tcl/tk{ver}/...
                    tk_match = _TK_DIR_RE.search(name)
                    if tk_match:
                        tk_ver_dir = tk_match.group(1)  # e.g. tk8.6
                        idx = name.find(f"/{tk_ver_dir}/")
                        rel = name[idx + 1 :]  # tk8.6/...
                        zf.writestr(f"tcl/{rel}", data)

            zip_buffer.seek(0)
            return zip_buffer.getvalue()

    @staticmethod
    def _unpack_tkinter_zip(zip_path: Path, runtime_dir: Path) -> None:
        """解压 tkinter zip 到 runtime 目录。"""
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(runtime_dir)
