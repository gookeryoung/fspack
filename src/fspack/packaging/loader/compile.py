"""C loader 编译器基类与平台子类：``generate → compile → cache`` 通用流程.

从 :mod:`fspack.packaging.loader` 拆分而来，封装平台差异：

- :class:`LoaderCompiler` 基类：缓存检查 → 命中复制 → 未命中编译 → 回写
- :class:`WindowsLoader`：mingw 交叉编译，GUI 加 -mwindows，icon 用 windres 嵌入
- :class:`LinuxLoader`：gcc 链接 libdl
- :class:`MacLoader`：clang，dlopen libpython3.X.dylib

工具链发现与 windres 资源编译见 :mod:`fspack.packaging.loader.toolchain`，
缓存键计算见 :mod:`fspack.packaging.loader.cache_keys`。C 源码模板从
:mod:`fspack.packaging.loader.source` 导入。
"""

from __future__ import annotations

import abc
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from fspack._compat import override
from fspack.config import AppType
from fspack.exceptions import LoaderError
from fspack.packaging.loader.cache_keys import (
    _icon_hash,
    _loader_cache_key,
    _version_info_hash,
    loader_cache_dir,
)
from fspack.packaging.loader.resource import LoaderVersionInfo
from fspack.packaging.loader.source import _LOADER_C_LINUX, _LOADER_C_MACOS, _LOADER_C_WINDOWS
from fspack.packaging.loader.toolchain import LINUX_GCC, MACOS_CLANG, MINGW_GCC, _compile_resource_obj, _find_mingw_gcc
from fspack.platform import Platform

if TYPE_CHECKING:
    # StageRecorder 仅用于类型注解；顶部不导入 fspack.progress 避免连锁触发
    # rich.progress 加载（``import fspack.builder`` 热路径不编译 loader）。
    from fspack.progress import StageRecorder

__all__ = [
    "LinuxLoader",
    "LoaderCompiler",
    "LoaderVersionInfo",
    "MacLoader",
    "WindowsLoader",
    "clang_available",
    "compile_loader",
    "gcc_available",
    "generate_loader_source",
    "mingw_available",
]

# 共享 logger 名：保持与原 loader.py 一致，测试 caplog 按 logger 名过滤
_logger = logging.getLogger("fspack.packaging.loader")

# 单次 loader C 编译超时（秒）：单文件 gcc/clang 链接实测 <5s（含资源段），
# 300s 与 pyc compileall 超时一致，覆盖慢速 CI/杀软扫描延迟；超时抛
# LoaderError 终止构建，避免编译器卡死（如杀软文件锁）无限阻塞
_LOADER_COMPILE_TIMEOUT = 300.0


# ---- 基类 ----


class LoaderCompiler(abc.ABC):
    """C loader 编译器基类。

    封装 ``generate → compile → cache`` 通用流程：

    1. :meth:`generate_source` —— 生成 C 源码（platform-specific 模板）
    2. :meth:`compile` —— 缓存检查 → 命中复制 → 未命中编译 → 回写缓存
    3. 编译时调用 :meth:`_build_command` 构造命令，:meth:`_prepare_icon` 处理图标资源

    类属性：
    - ``platform``：目标平台
    - ``exe_suffix``：可执行文件后缀（Windows 为 ``.exe``，Linux 为空）
    - ``compiler_name``：编译器可执行名
    - ``install_hint``：编译器缺失时的安装提示
    """

    platform: Platform
    exe_suffix: str = ""
    compiler_name: str = ""
    install_hint: str = ""

    @classmethod
    @abc.abstractmethod
    def generate_source(cls, py_xy: str) -> str:
        """生成 C loader 源码。

        py_xy: 形如 python311 的版本前缀。
        """

    @classmethod
    @abc.abstractmethod
    def _build_command(
        cls,
        c_file: Path,
        out_exe: Path,
        app_type: AppType,
        resource_obj: Path | None,
    ) -> list[str]:
        """构造编译命令。"""

    @classmethod
    def _supports_icon(cls) -> bool:
        """是否支持 icon 资源嵌入。默认 False，Windows 覆盖为 True。"""
        return False

    @classmethod
    def _prepare_resources(
        cls,
        icon: Path | None,  # noqa: ARG003
        version_info: LoaderVersionInfo | None,  # noqa: ARG003
        work_dir: Path,  # noqa: ARG003
    ) -> Path | None:
        """编译资源（icon/版本信息/manifest）为 .o 文件，返回路径。

        默认无资源处理（Linux/macOS 无 PE 资源段概念），Windows 子类覆盖为
        调用 :func:`fspack.packaging.loader.toolchain._compile_resource_obj`
        生成 windres COFF ``.o``。
        """
        return None

    @classmethod
    def available(cls) -> bool:
        """检测编译器是否可用。"""
        return shutil.which(cls.compiler_name) is not None

    @classmethod
    def compile(  # noqa: PLR0913
        cls,
        source: str,
        out_exe: Path,
        app_type: AppType,
        work_dir: Path,
        *,
        icon: Path | None = None,
        version_info: LoaderVersionInfo | None = None,
        cache_dir: Path | None = None,
        stage: StageRecorder | None = None,
    ) -> Path:
        """编译 loader 源码为可执行文件，返回路径。

        缓存命中时直接复制到 ``out_exe`` 并调 ``stage.hit_cache()``；
        未命中时编译并 best-effort 回写缓存供后续复用。缓存键为
        ``sha256(source + app_type + platform + icon_hash + version_info_hash)``
        前 16 字符，保证同配置命中、改配置失效。``cache_dir`` 默认 ``~/.fspack/cache/loaders/``。
        """
        out_exe.parent.mkdir(parents=True, exist_ok=True)

        # icon 路径不存在时不崩溃：warning 并按无 icon 处理（hash 空串），
        # 与 _compile_resource_obj 对 version_info/icon 的宽容行为对齐
        icon_hash = ""
        if icon is not None and cls._supports_icon():
            if icon.is_file():
                icon_hash = _icon_hash(icon)
            else:
                _logger.warning("icon 文件不存在，按无图标处理: %s", icon)
        version_info_hash = _version_info_hash(version_info) if version_info is not None else ""
        cache = cache_dir or loader_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        key = _loader_cache_key(source, app_type, cls.platform, icon_hash, version_info_hash)
        cached_exe = cache / f"{key}{cls.exe_suffix}"

        if cached_exe.is_file():
            _logger.info("loader 缓存命中: %s", cached_exe.name)
            shutil.copy2(cached_exe, out_exe)
            if stage is not None:
                stage.hit_cache()
                stage.set_detail("缓存命中")
            return out_exe

        # 缓存未命中：创建编译工作目录并写 loader.c
        # 缓存命中路径不创建 work_dir，避免 dist/build/ 留下空目录
        work_dir.mkdir(parents=True, exist_ok=True)
        c_file = work_dir / "loader.c"
        c_file.write_text(source, encoding="utf-8")

        resource_obj = cls._prepare_resources(icon, version_info, work_dir)
        cmd = cls._build_command(c_file, out_exe, app_type, resource_obj)
        _logger.info("编译 loader: %s", " ".join(cmd))
        try:
            # 延迟导入：避免 ``import fspack.builder`` 触发 fspack.progress 加载
            from fspack.progress import spinner

            with spinner(f"编译 loader ({cls.compiler_name})"):
                subprocess.run(
                    cmd,
                    check=True,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_LOADER_COMPILE_TIMEOUT,
                )
        except FileNotFoundError as e:
            raise LoaderError(f"未找到编译器 {cls.compiler_name}，请安装 {cls.install_hint}") from e
        except subprocess.TimeoutExpired as e:
            raise LoaderError(
                f"loader 编译超时（{int(_LOADER_COMPILE_TIMEOUT)}s）: {cls.compiler_name}，"
                "请检查杀软是否锁定输出文件后重试"
            ) from e
        except subprocess.CalledProcessError as e:
            raise LoaderError(f"loader 编译失败:\n{e.stderr}") from e
        # 资源编译失败（windres 不可用或 rc 语法错误）时不缓存 exe：
        # 此 exe 缺少 icon/版本信息/manifest 资源段，缓存后修复 resource.py
        # 重新打包会因缓存键（icon_hash+version_info_hash）不变而命中旧缓存，
        # 导致资源段永远无法嵌入。资源缺失时跳过缓存，下次打包重新编译。
        resource_failed = (
            cls._supports_icon() and (icon is not None or version_info is not None) and resource_obj is None
        )
        if resource_failed:
            _logger.warning("资源段未嵌入，跳过 loader 缓存（修复后重新打包将重新编译）")
        else:
            try:
                shutil.copy2(out_exe, cached_exe)
            except OSError as e:
                _logger.warning("loader 缓存回写失败: %s", e)
        if stage is not None:
            stage.set_detail(cls.compiler_name)
        return out_exe


# ---- 子类 ----


class WindowsLoader(LoaderCompiler):
    """Windows C loader 编译器（mingw 交叉编译）。"""

    platform = Platform.WINDOWS
    exe_suffix = ".exe"
    compiler_name = MINGW_GCC
    install_hint = "mingw-w64"

    @classmethod
    @override
    def available(cls) -> bool:
        """检测 mingw gcc 是否可用（带前缀或无前缀均可）。"""
        gcc = _find_mingw_gcc()
        return gcc is not None and shutil.which(gcc) is not None

    @classmethod
    @override
    def generate_source(cls, py_xy: str) -> str:
        """生成 Windows loader 源码，加载 python3X.dll 并调用 Py_Main。"""
        python_dll = f"runtime\\\\{py_xy}.dll"
        return _LOADER_C_WINDOWS.format(python_dll=python_dll)

    @classmethod
    @override
    def _build_command(
        cls,
        c_file: Path,
        out_exe: Path,
        app_type: AppType,
        resource_obj: Path | None,
    ) -> list[str]:
        """构造 mingw 编译命令：GUI/WEB 加 -mwindows，资源 .o 链接到 exe。

        ``-mwindows`` 使生成的 exe 不带控制台窗口（Windows subsystem）。
        WEB 类型与 GUI 一样需要关闭控制台（前后端分离 Web 应用作为桌面应用分发，
        黑色控制台窗口对终端用户不友好）。CLI 类型保留 console subsystem。
        ``resource_obj`` 为 windres 编译的资源段（icon + 版本信息 + manifest），
        非 None 时追加到 gcc 命令末尾链接进 exe。
        """
        # 无 mingw 前缀编译器时（如未装 mingw-w64 的 Linux）直接抛 LoaderError，
        # 避免 python -O 剥离 assert 后 cmd[0]=None 产生模糊 TypeError
        gcc = _find_mingw_gcc()
        if gcc is None:
            raise LoaderError(f"未找到编译器 {cls.compiler_name}，请安装 {cls.install_hint}")
        cmd: list[str] = [gcc, "-O2", "-municode", "-o", str(out_exe), str(c_file)]
        if app_type in (AppType.GUI, AppType.WEB):
            cmd.insert(1, "-mwindows")
        if resource_obj is not None:
            cmd.append(str(resource_obj))
        return cmd

    @classmethod
    @override
    def _supports_icon(cls) -> bool:
        """Windows 支持 icon 资源嵌入。"""
        return True

    @classmethod
    @override
    def _prepare_resources(
        cls,
        icon: Path | None,
        version_info: LoaderVersionInfo | None,
        work_dir: Path,
    ) -> Path | None:
        """用 windres 编译资源（icon + 版本信息 + manifest）为 COFF .o，返回路径。"""
        return _compile_resource_obj(icon, work_dir, version_info=version_info)


class LinuxLoader(LoaderCompiler):
    """Linux C loader 编译器（gcc 链接 libdl）。"""

    platform = Platform.LINUX
    exe_suffix = ""
    compiler_name = LINUX_GCC
    install_hint = "gcc"

    @classmethod
    @override
    def generate_source(cls, py_xy: str) -> str:
        """生成 Linux loader 源码，dlopen libpython3.X.so 并调用 Py_BytesMain。"""
        dotted = f"{py_xy[6]}.{py_xy[7:]}"
        libpython = f"runtime/python/lib/libpython{dotted}.so"
        return _LOADER_C_LINUX.format(libpython=libpython)

    @classmethod
    @override
    def _build_command(
        cls,
        c_file: Path,
        out_exe: Path,
        app_type: AppType,  # noqa: ARG003 # 抽象方法签名要求，Linux 不区分 app_type
        resource_obj: Path | None,  # noqa: ARG003 # 抽象方法签名要求，Linux 无 PE 资源段
    ) -> list[str]:
        """构造 gcc 编译命令，链接 libdl。"""
        return [LINUX_GCC, "-O2", "-o", str(out_exe), str(c_file), "-ldl"]


class MacLoader(LoaderCompiler):
    """macOS C loader 编译器（clang，dlopen libpython3.X.dylib）。

    与 LinuxLoader 结构相似：用 ``dlopen`` 加载 libpython，``setenv PYTHONHOME``
    指向 ``runtime/python``，调用 ``Py_BytesMain`` 运行入口脚本。差异：

    - 编译器用 ``clang``（macOS 默认，Xcode Command Line Tools 提供）
    - libpython 后缀为 ``.dylib``（Mach-O 动态库）
    - 不需 ``-ldl``（dlopen 在 libSystem.B.dylib，clang 默认链接）
    - C 源码用 ``_NSGetExecutablePath`` 取可执行路径（macOS 无 /proc/self/exe）
    - 不支持 icon 资源嵌入（Mach-O 无类似 windres 的 COFF 资源机制）
    """

    platform = Platform.MACOS
    exe_suffix = ""
    compiler_name = MACOS_CLANG
    install_hint = "clang（Xcode Command Line Tools）"

    @classmethod
    @override
    def generate_source(cls, py_xy: str) -> str:
        """生成 macOS loader 源码，dlopen libpython3.X.dylib 并调用 Py_BytesMain。"""
        dotted = f"{py_xy[6]}.{py_xy[7:]}"
        libpython = f"runtime/python/lib/libpython{dotted}.dylib"
        return _LOADER_C_MACOS.format(libpython=libpython)

    @classmethod
    @override
    def _build_command(
        cls,
        c_file: Path,
        out_exe: Path,
        app_type: AppType,  # noqa: ARG003 # 抽象方法签名要求，macOS 不区分 app_type
        resource_obj: Path | None,  # noqa: ARG003 # 抽象方法签名要求，macOS 无 PE 资源段
    ) -> list[str]:
        """构造 clang 编译命令（dlopen 在 libSystem，无需 -ldl）。"""
        return [MACOS_CLANG, "-O2", "-o", str(out_exe), str(c_file)]


# ---- 函数式 API（委托给类，按 platform dispatch）----


def generate_loader_source(
    py_xy: str,
    platform: Platform = Platform.WINDOWS,
) -> str:
    """生成 C loader 源码。

    py_xy: 形如 python311 的版本前缀。
    platform: 目标平台，决定加载 DLL（Windows）/ .so（Linux）/ .dylib（macOS）。

    入口脚本路径在运行时从 ``<exe_dir>/<exe_basename>.entry`` 读取（多入口），
    回退 ``<exe_dir>/.entry``（单入口）；构建时由 build 写入对应入口文件。
    loader 源码仅依赖 ``py_xy`` 与平台，可按 ``(py_xy, app_type, platform)`` 缓存复用。
    """
    cls = _loader_class_for(platform)
    return cls.generate_source(py_xy)


def compile_loader(  # noqa: PLR0913
    source: str,
    out_exe: Path,
    app_type: AppType,
    work_dir: Path,
    platform: Platform = Platform.WINDOWS,
    *,
    icon: Path | None = None,
    version_info: LoaderVersionInfo | None = None,
    cache_dir: Path | None = None,
    stage: StageRecorder | None = None,
) -> Path:
    """编译 loader 源码为可执行文件，返回路径。

    Windows 用 mingw 交叉编译（GUI 加 -mwindows），Linux 用 gcc（链接 libdl），
    macOS 用 clang（dlopen 在 libSystem，无需 -ldl）。

    ``icon`` 为 Windows 可执行文件图标（``.ico``），用 windres 编译资源文件
    链接到 exe。Linux 与 macOS 忽略 icon（ELF/Mach-O 无图标资源概念）。

    ``version_info`` 为 Windows loader exe 的 VS_VERSIONINFO 元数据（公司/产品/版本），
    与 icon 一并通过 windres 编译为资源段链接到 exe，并嵌入 application manifest
    （asInvoker + DPI 感知）。资源段完整的 exe 可显著降低 Defender 等杀软启发式误报。
    Linux 与 macOS 忽略（无 PE 资源段概念）。

    缓存命中时直接复制到 ``out_exe`` 并调 ``stage.hit_cache()``；
    未命中时编译并 best-effort 回写缓存供后续复用。缓存键为
    ``sha256(source + app_type + platform + icon_hash + version_info_hash)``
    前 16 字符，保证同配置命中、改配置失效。``cache_dir`` 默认 ``~/.fspack/cache/loaders/``。
    """
    cls = _loader_class_for(platform)
    return cls.compile(
        source, out_exe, app_type, work_dir, icon=icon, version_info=version_info, cache_dir=cache_dir, stage=stage
    )


def _loader_class_for(platform: Platform) -> type[LoaderCompiler]:
    """按目标平台返回对应的 loader 编译器子类。"""
    if platform is Platform.LINUX:
        return LinuxLoader
    if platform is Platform.MACOS:
        return MacLoader
    return WindowsLoader


def mingw_available() -> bool:
    """检测 mingw 交叉编译器是否可用。"""
    return WindowsLoader.available()


def gcc_available() -> bool:
    """检测 gcc 编译器是否可用。"""
    return LinuxLoader.available()


def clang_available() -> bool:
    """检测 clang 编译器是否可用（macOS）。"""
    return MacLoader.available()
