"""C loader 编译流程：基类、平台子类、编译命令构造、icon 处理.

从 :mod:`fspack.packaging.loader` 拆分而来，封装 ``generate → compile → cache``
通用流程与平台差异：

- :class:`LoaderCompiler` 基类：缓存检查 → 命中复制 → 未命中编译 → 回写
- :class:`WindowsLoader`：mingw 交叉编译，GUI 加 -mwindows，icon 用 windres 嵌入
- :class:`LinuxLoader`：gcc 链接 libdl

C 源码模板从 :mod:`fspack.packaging.loader.source` 导入。
"""

from __future__ import annotations

import abc
import hashlib
import logging
import shutil
import subprocess
from pathlib import Path

from fspack._compat import override
from fspack.config import AppType
from fspack.exceptions import LoaderError
from fspack.packaging.loader.source import _LOADER_C_LINUX, _LOADER_C_MACOS, _LOADER_C_WINDOWS
from fspack.platform import Platform
from fspack.progress import StageRecorder, spinner

__all__ = [
    "LINUX_GCC",
    "MACOS_CLANG",
    "MINGW_GCC",
    "LinuxLoader",
    "LoaderCompiler",
    "MacLoader",
    "WindowsLoader",
    "clang_available",
    "compile_loader",
    "gcc_available",
    "generate_loader_source",
    "loader_cache_dir",
    "mingw_available",
]

# 共享 logger 名：保持与原 loader.py 一致，测试 caplog 按 logger 名过滤
_logger = logging.getLogger("fspack.packaging.loader")
MINGW_GCC = "x86_64-w64-mingw32-gcc"
MINGW_WINDRES = "x86_64-w64-mingw32-windres"
LINUX_GCC = "gcc"
MACOS_CLANG = "clang"

_ICON_RC_TEMPLATE = 'id ICON "{icon_path}"\n'


def loader_cache_dir() -> Path:
    """返回 fspack loader 缓存目录（``FSPACK_CACHE_DIR`` 环境变量 > 默认 ``~/.fspack/cache/loaders``）."""
    from fspack.config.cache import loader_cache_dir as _cache_dir

    return _cache_dir()


def _loader_cache_key(source: str, app_type: AppType, platform: Platform, icon_hash: str = "") -> str:
    """计算 loader 缓存键：sha256(source + app_type + platform + icon_hash) 前 16 字符 hex。

    源码仅依赖 ``py_xy`` 与平台（入口路径运行时从 ``<exe_basename>.entry``
    或回退 ``.entry`` 读取），应用类型影响 ``-mwindows`` 编译选项，icon_hash
    区分不同 icon（空串表示无 icon）。四者组合哈希作为缓存文件名，保证同配置
    命中、改配置失效。
    """
    h = hashlib.sha256()
    h.update(source.encode("utf-8"))
    h.update(app_type.value.encode("utf-8"))
    h.update(platform.value.encode("utf-8"))
    h.update(icon_hash.encode("utf-8"))
    return h.hexdigest()[:16]


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
        icon_obj: Path | None,
    ) -> list[str]:
        """构造编译命令。"""

    @classmethod
    def _supports_icon(cls) -> bool:
        """是否支持 icon 资源嵌入。默认 False，Windows 覆盖为 True。"""
        return False

    @classmethod
    def _prepare_icon(cls, icon: Path, work_dir: Path) -> Path | None:  # noqa: ARG003
        """编译 icon 资源为 .o 文件，返回路径。默认无 icon 处理。"""
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
        cache_dir: Path | None = None,
        stage: StageRecorder | None = None,
    ) -> Path:
        """编译 loader 源码为可执行文件，返回路径。

        缓存命中时直接复制到 ``out_exe`` 并调 ``stage.hit_cache()``；
        未命中时编译并 best-effort 回写缓存供后续复用。缓存键为
        ``sha256(source + app_type + platform + icon_hash)`` 前 16 字符，保证
        同配置命中、改配置失效。``cache_dir`` 默认 ``~/.fspack/cache/loaders/``。
        """
        out_exe.parent.mkdir(parents=True, exist_ok=True)

        icon_hash = _icon_hash(icon) if icon is not None and cls._supports_icon() else ""
        cache = cache_dir or loader_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        key = _loader_cache_key(source, app_type, cls.platform, icon_hash)
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

        icon_obj = cls._prepare_icon(icon, work_dir) if icon is not None else None
        cmd = cls._build_command(c_file, out_exe, app_type, icon_obj)
        _logger.info("编译 loader: %s", " ".join(cmd))
        try:
            with spinner(f"编译 loader ({cls.compiler_name})"):
                subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
        except FileNotFoundError as e:
            raise LoaderError(f"未找到编译器 {cls.compiler_name}，请安装 {cls.install_hint}") from e
        except subprocess.CalledProcessError as e:
            raise LoaderError(f"loader 编译失败:\n{e.stderr}") from e
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
        return shutil.which(_find_mingw_gcc()) is not None

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
        icon_obj: Path | None,
    ) -> list[str]:
        """构造 mingw 编译命令：GUI 加 -mwindows，icon 编译为 .o 链接。"""
        cmd: list[str] = [_find_mingw_gcc(), "-O2", "-municode", "-o", str(out_exe), str(c_file)]
        if app_type is AppType.GUI:
            cmd.insert(1, "-mwindows")
        if icon_obj is not None:
            cmd.append(str(icon_obj))
        return cmd

    @classmethod
    @override
    def _supports_icon(cls) -> bool:
        """Windows 支持 icon 资源嵌入。"""
        return True

    @classmethod
    @override
    def _prepare_icon(cls, icon: Path, work_dir: Path) -> Path | None:
        """用 windres 把 .ico 编译为 COFF 格式 .o 文件，返回路径。"""
        return _compile_icon_resource(icon, work_dir)


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
        icon_obj: Path | None,  # noqa: ARG003 # 抽象方法签名要求，Linux 不支持 icon 资源嵌入
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
        icon_obj: Path | None,  # noqa: ARG003 # 抽象方法签名要求，macOS 不支持 icon 资源
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
    cache_dir: Path | None = None,
    stage: StageRecorder | None = None,
) -> Path:
    """编译 loader 源码为可执行文件，返回路径。

    Windows 用 mingw 交叉编译（GUI 加 -mwindows），Linux 用 gcc（链接 libdl），
    macOS 用 clang（dlopen 在 libSystem，无需 -ldl）。

    ``icon`` 为 Windows 可执行文件图标（.ico），用 windres 编译资源文件
    链接到 exe。Linux 与 macOS 忽略 icon（ELF/Mach-O 无图标资源概念）。

    缓存命中时直接复制到 ``out_exe`` 并调 ``stage.hit_cache()``；
    未命中时编译并 best-effort 回写缓存供后续复用。缓存键为
    ``sha256(source + app_type + platform + icon_hash)`` 前 16 字符，保证
    同配置命中、改配置失效。``cache_dir`` 默认 ``~/.fspack/cache/loaders/``。
    """
    cls = _loader_class_for(platform)
    return cls.compile(source, out_exe, app_type, work_dir, icon=icon, cache_dir=cache_dir, stage=stage)


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


# ---- icon 资源处理（Windows 专用）----


def _icon_hash(icon: Path) -> str:
    """计算 icon 文件内容的 sha256 前 16 字符 hex，用于缓存键。"""
    h = hashlib.sha256()
    h.update(icon.read_bytes())
    return h.hexdigest()[:16]


def _find_windres() -> str:
    """查找可用的 windres，优先交叉前缀，回退无前缀。

    Windows mingw64 发行版通常命名 ``windres``（无前缀），Linux 交叉编译
    环境命名 ``x86_64-w64-mingw32-windres``（带前缀）。两者都查找不到时
    返回默认名，让后续 subprocess 报 FileNotFoundError。
    """
    for name in (MINGW_WINDRES, "windres"):
        if shutil.which(name):
            return name
    return MINGW_WINDRES


def _find_mingw_gcc() -> str:
    """查找可用的 mingw gcc，优先交叉前缀，回退无前缀。

    与 :func:`_find_windres` 同理：Windows 原生 mingw64 发行版（MSYS2、WinLibs、
    chocolatey mingw 包）通常命名 ``gcc``（无前缀），Linux 交叉编译环境命名
    ``x86_64-w64-mingw32-gcc``（带前缀）。两者都查找不到时返回默认名，让后续
    subprocess 报 FileNotFoundError。
    """
    for name in (MINGW_GCC, "gcc"):
        if shutil.which(name):
            return name
    return MINGW_GCC


def _compile_icon_resource(icon: Path, work_dir: Path) -> Path | None:
    """用 windres 把 .ico 编译为 COFF 格式 .o 文件，返回路径。

    生成 ``icon.rc`` 引用 icon 文件，windres 编译为 ``icon.o`` 供 gcc 链接。
    windres 处理路径用 Windows 反斜杠风格，icon 文件复制到 work_dir 避免相对
    路径问题。windres 不可用时 warning 并返回 None（exe 仍可编译，仅无图标）。
    """
    if not icon.is_file():
        _logger.warning("icon 文件不存在，跳过图标嵌入: %s", icon)
        return None
    windres = _find_windres()
    if not shutil.which(windres):
        _logger.warning("未找到 windres，跳过图标嵌入（请安装 mingw-w64）")
        return None
    # 复制 icon 到 work_dir 避免相对路径问题
    icon_copy = work_dir / "icon.ico"
    shutil.copy2(icon, icon_copy)
    # windres 处理路径用 Windows 反斜杠
    rc_content = _ICON_RC_TEMPLATE.format(icon_path="icon.ico")
    rc_file = work_dir / "icon.rc"
    rc_file.write_text(rc_content, encoding="utf-8")
    obj_file = work_dir / "icon.o"
    cmd = [windres, "--input", str(rc_file), "--output", str(obj_file), "--output-format=coff"]
    _logger.info("编译 icon 资源: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace", cwd=work_dir)
    except FileNotFoundError as e:
        _logger.warning("windres 不可用，跳过图标嵌入: %s", e)
        return None
    except subprocess.CalledProcessError as e:
        _logger.warning("icon 资源编译失败，跳过图标嵌入:\n%s", e.stderr)
        return None
    return obj_file
