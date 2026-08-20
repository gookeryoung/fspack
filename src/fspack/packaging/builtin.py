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
from typing import TYPE_CHECKING

# 保留顶部 ``Downloader`` 引用：测试通过 ``fspack.packaging.builtin.Downloader.download``
# 路径 patch（test_builtin.py），移到方法内会破坏 monkeypatch.setattr 解析。
# net.py 顶部已轻量化（rich.progress/console/StageRecorder 延迟导入），加载 builtin
# 触发 net 模块定义不再连带加载 rich.progress。
from fspack.config import is_offline
from fspack.config.versions import _split_t_suffix
from fspack.exceptions import BuiltinError
from fspack.packaging.net import Downloader
from fspack.packaging.runtime import STANDALONE_BASE_URL, STANDALONE_RELEASE_TAG
from fspack.platform import Platform

if TYPE_CHECKING:
    # StageRecorder 仅用于类型注解；顶部不导入 fspack.progress 避免连锁触发
    # rich.progress 加载（``import fspack.builder`` 热路径不打包 tkinter）。
    from fspack.progress import StageRecorder

__all__ = ["TkinterBundler"]

_logger = logging.getLogger(__name__)

# 匹配 .../DLLs/tcl86t.dll / .../DLLs/tk86t.dll 等 Tcl/Tk C 运行时 DLL。
# tcl86t.dll / tk86t.dll 是 _tkinter.pyd 的直接依赖，缺失会导致
# ImportError: DLL load failed while importing _tkinter
_TCL_RUNTIME_DLL_RE = re.compile(r"/DLLs/((?:tcl|tk)\d+t?\.dll)$")

# 匹配 .../tcl/<subdir>/<file> 路径，捕获 <subdir>/<file> 部分。
# 含 tcl8.6/tk8.6 主脚本目录与 dde1.4/reg1.3/tix8.4.3 等扩展包目录
_TCL_DIR_PREFIX_RE = re.compile(r"/tcl/(.+)$")

# tcl/ 目录下运行时无用的开发期文件后缀（import library / config 脚本）
_TCL_DEV_EXTS = (".lib", ".sh")


class TkinterBundler:
    """tkinter 内置库打包器.

    从 python-build-standalone Windows 构建提取 tkinter 组件（纯 Python 包、
    ``_tkinter.pyd`` C 扩展、Tcl/Tk 运行时脚本），补充到 embed python runtime。

    缓存策略：首次下载 ~40MB tarball，提取 tkinter 组件为 ~3-5MB zip 缓存；
    后续构建直接解压缓存的 zip（秒级）。
    """

    @staticmethod
    def standalone_windows_tarball_name(version: str, release_tag: str) -> str:
        """返回 python-build-standalone Windows tarball 文件名。

        20241016 及更早 release 同时提供 ``-shared-`` 与非 shared 变体；
        20260718 起仅提供非 shared 变体（``x86_64-pc-windows-msvc-install_only``），
        故统一使用非 shared 命名以保证跨 release 兼容。
        """
        return f"cpython-{version}+{release_tag}-x86_64-pc-windows-msvc-install_only.tar.gz"

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
        """确保 tkinter 在 runtime 中可用（缓存优先，损坏自动恢复）。

        1. 检查 ``runtime/Lib/tkinter/__init__.py`` 是否已存在 → 命中跳过
        2. 检查 ``cache/tkinter/tkinter-{standalone_ver}.zip`` 是否已缓存 → 解压到 runtime；
           若 zip 损坏（``BadZipFile``）删除后走下载分支重建
        3. 下载 Windows standalone tarball → 提取 tkinter → 生成缓存 zip → 解压到 runtime；
           若 tarball 损坏（``EOFError``/``ReadError``）删除并重新下载重试一次；
           离线模式下损坏直接抛 :class:`BuiltinError` 提示用户删除缓存

        ``version`` 为 embed python 版本（如 3.11.9），但 python-build-standalone release
        只含该 minor 最新补丁（如 3.11.15）。``_tkinter.pyd`` 在同一 minor 内 ABI 兼容
        （cp311），故用 :data:`KNOWN_STANDALONE_VERSIONS` 解析同 minor 的 standalone 版本
        下载 tarball，避免拼出不存在的 URL。
        """
        from fspack.config import KNOWN_STANDALONE_VERSIONS

        tkinter_marker = runtime_dir / "Lib" / "tkinter" / "__init__.py"
        if tkinter_marker.is_file():
            stage.hit_cache()
            stage.set_detail("tkinter 已就绪")
            _logger.info("tkinter 打包: runtime 已含 tkinter，跳过")
            return

        # embed 版本 → 同 minor 的 standalone 版本（ABI 兼容，避免拼出不存在的 URL）
        # free-threaded build（t 后缀）查 '3.13t' 键，命中 '3.13.14t' 后用于
        # standalone tarball 下载与解压目录名（-freethreaded-install_only 段，版本号无 t）
        base, is_t = _split_t_suffix(version)
        minor_key = ".".join(base.split(".")[:2]) + ("t" if is_t else "")
        standalone_ver = KNOWN_STANDALONE_VERSIONS.get(minor_key, version)

        tkinter_cache_dir = cache_dir / "tkinter"
        tkinter_cache_dir.mkdir(parents=True, exist_ok=True)
        cache_zip = tkinter_cache_dir / f"tkinter-{standalone_ver}.zip"

        if cache_zip.is_file():
            stage.set_detail("从缓存解压 tkinter")
            _logger.info("tkinter 打包: 从缓存解压 %s", cache_zip.name)
            try:
                cls._unpack_tkinter_zip(cache_zip, runtime_dir)
                stage.processed(1)
                return
            except zipfile.BadZipFile as e:
                # 缓存 zip 损坏，删除后落到 tarball 下载分支重建
                _logger.warning("tkinter 缓存 zip 损坏，删除并重建: %s", e)
                cache_zip.unlink(missing_ok=True)

        # 下载 Windows standalone tarball
        standalone_windows_cache = cache_dir / "standalone-windows"
        standalone_windows_cache.mkdir(parents=True, exist_ok=True)
        tarball_path = standalone_windows_cache / cls.standalone_windows_tarball_name(
            standalone_ver, STANDALONE_RELEASE_TAG
        )

        if not tarball_path.is_file():
            # 离线模式 fail-fast：standalone tarball 缓存未命中时立即报错
            if is_offline():
                raise BuiltinError(
                    f"离线模式下 standalone Windows tarball 缓存未命中: {tarball_path.name}，"
                    f"请预先下载放入 {standalone_windows_cache} 或取消 FSPACK_OFFLINE 环境变量"
                )
            url = cls.standalone_windows_url(standalone_ver, STANDALONE_RELEASE_TAG)
            _logger.info("tkinter 打包: 下载 Windows standalone 构建 %s", standalone_ver)
            downloader = Downloader()
            downloader.download(url, tarball_path, stage=stage, label=f"standalone-windows {standalone_ver}")
        else:
            stage.hit_cache()
            _logger.info("tkinter 打包: standalone tarball 已缓存")

        # 从 tarball 提取 tkinter 组件，生成缓存 zip
        _logger.info("tkinter 打包: 从 tarball 提取 tkinter 组件")
        try:
            zip_data = cls._build_tkinter_zip(tarball_path)
        except (EOFError, tarfile.ReadError) as e:
            # tarball 损坏（gzip 流提前结束等），删除缓存并重新下载重试一次
            _logger.warning("standalone tarball 损坏，删除并重新下载: %s", e)
            tarball_path.unlink(missing_ok=True)
            if is_offline():
                raise BuiltinError(
                    f"离线模式下 standalone tarball 缓存损坏: {tarball_path.name}，"
                    f"请删除 {tarball_path} 后重新运行或取消 FSPACK_OFFLINE 环境变量"
                ) from e
            url = cls.standalone_windows_url(standalone_ver, STANDALONE_RELEASE_TAG)
            _logger.info("tkinter 打包: 重新下载 Windows standalone 构建 %s", standalone_ver)
            downloader = Downloader()
            downloader.download(url, tarball_path, stage=stage, label=f"standalone-windows {standalone_ver}")
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
        - ``.../DLLs/tcl*t.dll`` / ``.../DLLs/tk*t.dll`` → 根目录（Tcl/Tk C 运行时
          DLL，``_tkinter.pyd`` 直接依赖，缺失导致 ``ImportError: DLL load
          failed while importing _tkinter``）
        - ``.../tcl/<subdir>/**`` → ``tcl/<subdir>/...``（Tcl/Tk 脚本与扩展包，
          含 ``tcl8.6``/``tk8.6`` 主脚本与 ``dde1.4``/``reg1.3``/``tix8.4.3`` 等
          扩展；排除 ``.lib``/``.sh`` 等开发期文件以节省空间）
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

                    # DLLs/tcl*t.dll / DLLs/tk*t.dll → 根目录（Tcl/Tk C 运行时 DLL）
                    # _tkinter.pyd 直接依赖 tcl86t.dll / tk86t.dll，缺失导致
                    # ImportError: DLL load failed while importing _tkinter
                    dll_match = _TCL_RUNTIME_DLL_RE.search(name)
                    if dll_match:
                        zf.writestr(dll_match.group(1), data)
                        continue

                    # tcl/<subdir>/** → tcl/<subdir>/...（Tcl/Tk 脚本与扩展包）
                    # 含 tcl8.6/tk8.6 主脚本与 dde1.4/reg1.3/tix8.4.3 等扩展包；
                    # 排除 .lib（import library）/ .sh（config 脚本）等开发期文件
                    tcl_dir_match = _TCL_DIR_PREFIX_RE.search(name)
                    if tcl_dir_match:
                        rel = tcl_dir_match.group(1)  # <subdir>/<file>
                        if not rel.endswith(_TCL_DEV_EXTS):
                            zf.writestr(f"tcl/{rel}", data)

            zip_buffer.seek(0)
            return zip_buffer.getvalue()

    @staticmethod
    def _unpack_tkinter_zip(zip_path: Path, runtime_dir: Path) -> None:
        """解压 tkinter zip 到 runtime 目录。"""
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(runtime_dir)
