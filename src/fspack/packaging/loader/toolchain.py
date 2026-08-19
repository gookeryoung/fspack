"""交叉编译工具链发现与 Windows 资源段编译.

从 :mod:`fspack.packaging.loader.compile` 拆分而来，聚集「工具链」职责：

- 编译器可执行名常量（mingw gcc / windres / gcc / clang）
- :func:`_find_mingw_gcc` / :func:`_find_windres`：编译器发现（交叉前缀优先）
- :func:`_compile_resource_obj`：windres 编译 Windows 资源段（icon + 版本信息 + manifest）
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from fspack.packaging.loader.resource import (
    LoaderVersionInfo,
    generate_app_manifest,
    generate_resource_rc,
)

__all__ = ["LINUX_GCC", "MACOS_CLANG", "MINGW_GCC", "MINGW_WINDRES"]

# 共享 logger 名：与 loader 包各子模块一致，测试 caplog 按 logger 名过滤
_logger = logging.getLogger("fspack.packaging.loader")
MINGW_GCC = "x86_64-w64-mingw32-gcc"
MINGW_WINDRES = "x86_64-w64-mingw32-windres"
LINUX_GCC = "gcc"
MACOS_CLANG = "clang"

# windres 单次资源编译超时（秒）：单 rc 文件实测 <1s，120s 裕量覆盖杀软
# 扫描延迟；超时与编译失败同路径降级（warning + 跳过资源段，不阻断构建）
_WINDRES_TIMEOUT = 120.0


def _find_windres() -> str:
    """查找可用的 windres，优先交叉前缀，回退无前缀.

    Windows mingw64 发行版通常命名 ``windres``（无前缀），Linux 交叉编译
    环境命名 ``x86_64-w64-mingw32-windres``（带前缀）。两者都查找不到时
    返回默认名，让后续 subprocess 报 FileNotFoundError。
    """
    for name in (MINGW_WINDRES, "windres"):
        if shutil.which(name):
            return name
    return MINGW_WINDRES


def _find_mingw_gcc() -> str | None:
    """查找可用的 mingw gcc，优先交叉前缀.

    与 :func:`_find_windres` 类似但回退受限：Windows 原生 mingw64 发行版
    （MSYS2、WinLibs、chocolatey mingw 包）通常命名 ``gcc``（无前缀），
    仅在 ``sys.platform == "win32"`` 时回退 ``"gcc"``。Linux/macOS 的
    ``gcc`` 是 host 编译器（产出 ELF/Mach-O 而非 PE），交叉构建 Windows
    exe 时不能回退，无 mingw 前缀编译器时返回 ``None``（调用方据此判
    ``available()`` 为 False）。
    """
    if shutil.which(MINGW_GCC):
        return MINGW_GCC
    if sys.platform == "win32" and shutil.which("gcc"):
        return "gcc"
    return None


def _compile_resource_obj(
    icon: Path | None,
    work_dir: Path,
    *,
    version_info: LoaderVersionInfo | None = None,
) -> Path | None:
    """用 windres 编译 Windows 资源（icon + 版本信息 + manifest）为 COFF ``.o``，返回路径.

    生成 ``resource.rc``（icon 引用 + VS_VERSIONINFO + manifest 引用）与
    ``app.manifest``，windres 编译为 ``resource.o`` 供 gcc 链接到 exe。

    - ``icon`` 非 None 且文件存在时复制到 work_dir 并在 rc 中引用；不存在则 warning
      并跳过 icon（其余资源仍编译）
    - ``version_info`` 非 None 时 rc 含 VERSIONINFO 块；为 None 时省略版本信息
    - manifest 总是生成并引用（asInvoker + DPI + supportedOS），单独嵌入即降低可疑度

    windres 不可用时 warning 并返回 None（exe 仍可编译，仅无资源段）。windres
    编译失败（如 rc 语法错误）时同样返回 None，不阻断构建。
    """
    windres = _find_windres()
    if not shutil.which(windres):
        _logger.warning("未找到 windres，跳过资源嵌入（版本信息/manifest/icon，请安装 mingw-w64）")
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    # icon 复制到 work_dir（windres 解析相对路径，统一文件名 icon.ico）
    has_icon = False
    if icon is not None and icon.is_file():
        shutil.copy2(icon, work_dir / "icon.ico")
        has_icon = True
    elif icon is not None:
        _logger.warning("icon 文件不存在，跳过图标嵌入（其余资源仍编译）: %s", icon)
    # 写 resource.rc 与 app.manifest（UTF-8；rc 顶部 #pragma code_page(65001) 声明编码，
    # 使中文 CompanyName/FileDescription 被 windres 正确转为 UTF-16LE 存入 PE 资源段）
    rc_content = generate_resource_rc(version_info, has_icon=has_icon)
    rc_file = work_dir / "resource.rc"
    rc_file.write_text(rc_content, encoding="utf-8")
    manifest_name = version_info.name if version_info is not None else "app"
    manifest_version = version_info.version if version_info is not None else "1.0.0"
    manifest_content = generate_app_manifest(manifest_name, manifest_version)
    manifest_file = work_dir / "app.manifest"
    manifest_file.write_text(manifest_content, encoding="utf-8")
    obj_file = work_dir / "resource.o"
    cmd = [windres, "--input", str(rc_file), "--output", str(obj_file), "--output-format=coff"]
    _logger.info("编译 Windows 资源: %s", " ".join(cmd))
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=work_dir,
            timeout=_WINDRES_TIMEOUT,
        )
    except FileNotFoundError as e:
        _logger.warning("windres 不可用，跳过资源嵌入: %s", e)
        return None
    except subprocess.TimeoutExpired:
        _logger.warning("资源编译超时（%ds），跳过资源嵌入", int(_WINDRES_TIMEOUT))
        return None
    except subprocess.CalledProcessError as e:
        _logger.warning("资源编译失败，跳过资源嵌入:\n%s", e.stderr)
        return None
    return obj_file
