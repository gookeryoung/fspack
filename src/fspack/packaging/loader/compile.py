"""C loader 公共对象编译、独立资源链接与三层产物缓存。"""

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
from fspack.packaging.loader.cache import cache_lock, cached_copy, publish_cache
from fspack.packaging.loader.cache_keys import (
    _effective_app_type,
    _icon_hash,
    _loader_cache_key,
    _object_cache_key,
    _resource_cache_key,
    _version_info_hash,
    loader_cache_dir,
)
from fspack.packaging.loader.resource import LoaderVersionInfo
from fspack.packaging.loader.source import (
    apply_splash,
    loader_linux_source,
    loader_macos_source,
    loader_windows_source,
)
from fspack.packaging.loader.toolchain import (
    LINUX_GCC,
    MACOS_CLANG,
    MINGW_GCC,
    _compile_resource_obj,
    _find_mingw_gcc,
    _find_windres,
    toolchain_fingerprint,
)
from fspack.platform import Platform

if TYPE_CHECKING:
    # StageRecorder 仅用于类型注解；顶部不导入 fspack.progress 避免连锁触发
    # rich.progress 加载（``import fspack.builder`` 热路径不编译 loader）。
    from fspack.metrics import StageRecorder

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
    """按目标平台共享 C 对象并缓存独立入口产物。"""

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
        obj_file: Path,
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
        icon: Path | None,
        version_info: LoaderVersionInfo | None,
        work_dir: Path,
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
    def _compiler(cls) -> str:
        return cls.compiler_name

    @classmethod
    def _compile_flags(cls) -> tuple[str, ...]:
        return ("-O2",)

    @classmethod
    def _run_command(cls, cmd: list[str]) -> None:
        _logger.debug("编译 loader: %s", " ".join(cmd))
        try:
            completed = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_LOADER_COMPILE_TIMEOUT,
            )
            _forward_tool_output(completed)
        except FileNotFoundError as e:
            raise LoaderError(f"未找到编译器 {cls.compiler_name}，请安装 {cls.install_hint}") from e
        except subprocess.TimeoutExpired as e:
            raise LoaderError(
                f"loader 编译超时（{int(_LOADER_COMPILE_TIMEOUT)}s）: {cls.compiler_name}，"
                "请检查杀软是否锁定输出文件后重试"
            ) from e
        except subprocess.CalledProcessError as e:
            _forward_tool_output(subprocess.CompletedProcess(cmd, e.returncode, e.stdout, e.stderr))
            raise LoaderError(f"loader 编译失败:\n{e.stderr}") from e

    @classmethod
    def _compile_object(
        cls,
        source: str,
        work_dir: Path,
        cache: Path,
        toolchain_key: str,
        stage: StageRecorder | None,
    ) -> Path:
        flags = cls._compile_flags()
        key = _object_cache_key(source, cls.platform, toolchain_key, flags)
        cached_obj = cache / "objects" / f"{key}.o"
        obj_file = work_dir / "loader.o"
        with cache_lock(cached_obj):
            if cached_copy(cached_obj, obj_file):
                _logger.debug("loader C 对象缓存命中: %s", cached_obj.name)
                if stage is not None:
                    stage.hit_cache()
                return obj_file
            c_file = work_dir / "loader.c"
            c_file.write_text(source, encoding="utf-8")
            cls._run_command([cls._compiler(), *flags, "-c", str(c_file), "-o", str(obj_file)])
            publish_cache(obj_file, cached_obj)
        return obj_file

    @classmethod
    def _resource_object(
        cls,
        icon: Path | None,
        version_info: LoaderVersionInfo | None,
        work_dir: Path,
        cache: Path,
        key: str,
        stage: StageRecorder | None,
    ) -> Path | None:
        if not cls._supports_icon():
            return None
        cached_obj = cache / "resources" / f"{key}.o"
        obj_file = work_dir / "resource.o"
        with cache_lock(cached_obj):
            if cached_copy(cached_obj, obj_file):
                _logger.debug("loader 资源对象缓存命中: %s", cached_obj.name)
                if stage is not None:
                    stage.hit_cache()
                return obj_file
            resource_obj = cls._prepare_resources(icon, version_info, work_dir)
            if resource_obj is not None:
                publish_cache(resource_obj, cached_obj)
            return resource_obj

    @classmethod
    def compile(
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
        """共享 C 对象，独立生成资源与链接；完整产物校验后复用。"""
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        app_type = _effective_app_type(app_type, cls.platform)
        if not cls._supports_icon():
            icon = None
            version_info = None
        icon_hash = ""
        if icon is not None:
            if icon.is_file():
                icon_hash = _icon_hash(icon)
            else:
                _logger.warning("icon 文件不存在，按无图标处理: %s", icon)
        version_info_hash = _version_info_hash(version_info) if version_info is not None else ""
        cache = cache_dir or loader_cache_dir()
        identities = cache / "toolchain"
        compiler_key = toolchain_fingerprint(cls._compiler(), store=identities)
        resource_key = ""
        if cls._supports_icon():
            resource_key = _resource_cache_key(
                icon_hash,
                version_info,
                toolchain_fingerprint(_find_windres(), resource=True, store=identities) + compiler_key,
            )
        link_flags = tuple(
            cls._build_command(
                Path("loader.o"), Path("loader"), app_type, Path("resource.o") if cls._supports_icon() else None
            )
        )
        key = _loader_cache_key(
            source,
            app_type,
            cls.platform,
            icon_hash,
            version_info_hash,
            toolchain_key=compiler_key,
            compile_flags=cls._compile_flags(),
            link_flags=link_flags,
            resource_key=resource_key,
        )
        cached_exe = cache / f"{key}{cls.exe_suffix}"
        with cache_lock(cached_exe):
            if cached_copy(cached_exe, out_exe):
                _logger.debug("loader 缓存命中: %s", cached_exe.name)
                if stage is not None:
                    stage.hit_cache()
                    stage.set_detail("缓存命中")
                return out_exe

            from fspack.progress import spinner

            work_dir.mkdir(parents=True, exist_ok=True)
            with spinner(f"编译 loader ({cls.compiler_name})"):
                obj_file = cls._compile_object(source, work_dir, cache, compiler_key, stage)
                resource_obj = cls._resource_object(icon, version_info, work_dir, cache, resource_key, stage)
                cls._run_command(cls._build_command(obj_file, out_exe, app_type, resource_obj))
            if cls._supports_icon() and resource_obj is None:
                _logger.warning("资源段未嵌入，跳过 loader 缓存（修复后重新打包将重新编译）")
            else:
                publish_cache(out_exe, cached_exe)
        if stage is not None:
            stage.set_detail(cls.compiler_name)
        return out_exe


# ---- 子类 ----


def _forward_tool_output(completed: subprocess.CompletedProcess[str]) -> None:
    """把 gcc 的原始输出转发到 DEBUG（``[loader]`` 前缀，默认不显示）.

    编译器在成功路径上常年静默（无 warning 时 stdout/stderr 为空），失败时由
    :class:`LoaderError` 携带 stderr。转发保证 ``-v`` 与 CI 日志文件里能拿到
    完整命令行/告警/告警上下文，而默认输出仍然只有阶段级一行。
    """
    if not completed.stdout and not completed.stderr:
        return
    from fspack.console import console

    for text in (completed.stdout, completed.stderr):
        if text:
            console.tool_output("loader", text.encode("utf-8"))


class WindowsLoader(LoaderCompiler):
    """Windows C loader 编译器（mingw 交叉编译）。"""

    platform = Platform.WINDOWS
    exe_suffix = ".exe"
    compiler_name = MINGW_GCC
    install_hint = "mingw-w64"

    @classmethod
    @override
    def _compiler(cls) -> str:
        gcc = _find_mingw_gcc()
        if gcc is None:
            raise LoaderError(f"未找到编译器 {cls.compiler_name}，请安装 {cls.install_hint}")
        return gcc

    @classmethod
    @override
    def _compile_flags(cls) -> tuple[str, ...]:
        return ("-O2", "-municode")

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
        return loader_windows_source().replace("{python_dll}", python_dll)

    @classmethod
    @override
    def _build_command(
        cls,
        obj_file: Path,
        out_exe: Path,
        app_type: AppType,
        resource_obj: Path | None,
    ) -> list[str]:
        """链接公共对象与入口资源，链接时选择 console/windowed 子系统。"""
        cmd = [cls._compiler(), *cls._compile_flags(), "-o", str(out_exe), str(obj_file), "-luser32", "-lgdi32"]
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
        return loader_linux_source().replace("{libpython}", libpython)

    @classmethod
    @override
    def _build_command(
        cls,
        obj_file: Path,
        out_exe: Path,
        app_type: AppType,  # 抽象方法签名要求，Linux 不区分 app_type
        resource_obj: Path | None,  # 抽象方法签名要求，Linux 无 PE 资源段
    ) -> list[str]:
        """构造 gcc 编译命令，链接 libdl + 设置 runtime 库搜索 RPATH.

        ``--disable-new-dtags`` 生成老式 ``DT_RPATH``（而非 ``DT_RUNPATH``）：
        RPATH 对进程内**所有**动态加载生效（RUNPATH 仅作用于 exe 直接 NEEDED），
        之后 Python import 机制 dlopen 的 C 扩展（如 ``_tkinter.so``）解析
        NEEDED（``libtcl9.0.so`` 等）时同样搜索该目录。``$ORIGIN`` 由 ld.so
        展开为 exe 所在目录（dist 根），与 libpython 相对路径布局一致。
        """
        return [
            LINUX_GCC,
            "-O2",
            "-o",
            str(out_exe),
            str(obj_file),
            "-ldl",
            "-Wl,--disable-new-dtags",
            "-Wl,-rpath,$ORIGIN/runtime/python/lib",
        ]


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
        return loader_macos_source().replace("{libpython}", libpython)

    @classmethod
    @override
    def _build_command(
        cls,
        obj_file: Path,
        out_exe: Path,
        app_type: AppType,  # 抽象方法签名要求，macOS 不区分 app_type
        resource_obj: Path | None,  # 抽象方法签名要求，macOS 无 PE 资源段
    ) -> list[str]:
        """构造 clang 编译命令（dlopen 在 libSystem，无需 -ldl）。"""
        return [MACOS_CLANG, "-O2", "-o", str(out_exe), str(obj_file)]


# ---- 函数式 API（委托给类，按 platform dispatch）----


def generate_loader_source(
    py_xy: str,
    platform: Platform = Platform.WINDOWS,
    *,
    splash: bool = False,
    splash_title: str = "",
) -> str:
    """生成 C loader 源码。

    py_xy: 形如 python311 的版本前缀。
    platform: 目标平台，决定加载 DLL（Windows）/ .so（Linux）/ .dylib（macOS）。
    splash: 注入 splash 启动画面（仅 Windows；Linux/macOS 忽略，保持源码不变）。
        画面显示应用名，进程内出现首个可见窗口（GUI 通用）、wrapper 事件通知
        （WEB server 启动）或 30s 超时后自动关闭。
    splash_title: splash 显示的应用名（splash=True 时生效，经 C 转义嵌入源码）。

    入口脚本路径在运行时从 ``<exe_dir>/<exe_basename>.entry`` 读取（多入口），
    回退 ``<exe_dir>/.entry``（单入口）；构建时由 build 写入对应入口文件。
    loader 源码依赖 ``(py_xy, app_type, platform)`` 与 splash 配置缓存复用——
    splash 标题嵌入源码，不同应用名自然产生不同缓存键。
    """
    cls = _loader_class_for(platform)
    source = cls.generate_source(py_xy)
    if splash and platform is Platform.WINDOWS:
        source = apply_splash(source, splash_title)
    else:
        source = apply_splash(source, None)
    return source


def compile_loader(
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
    """按平台编译 loader，复用公共对象并保留各入口 Windows 资源。"""
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
