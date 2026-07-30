"""安装包生成 facade：Windows NSIS / Linux tar.gz + .deb / macOS .pkg + .dmg / 跨平台 zip 便携包.

本包是 facade，将原 ``installer.py`` 单文件拆分为子包：

- :mod:`fspack.packaging.installer.base`：``Installer`` 抽象基类与通用编排流程
  （``build_installer``/``build_linux_installer``/``build_release`` 调度 + 公共辅助
  ``_run_stage``/``_prepare_dist``/``_check_exe``/``_release_base`` 等）
- :mod:`fspack.packaging.installer.nsis`：NSIS 脚本生成与编译（Windows）
- :mod:`fspack.packaging.installer.linux`：tar.gz 便携包与 .deb 安装包（Linux）
- :mod:`fspack.packaging.installer.macos`：.pkg 安装包与 .dmg 磁盘镜像（macOS）
- :mod:`fspack.packaging.installer.zip`：跨平台 zip 便携包

显式 ``import subprocess`` 是为了兼容测试中的
``monkeypatch.setattr("fspack.packaging.installer.subprocess.run", ...)`` 等 patch 路径——
patch 设置的是模块对象的属性，因标准库模块为单例，全局生效，对 base/nsis/linux/macos/zip
五个子模块内的 subprocess 调用同样有效。

``build_release`` 按 ``--format`` 调度生成一种或多种格式产物：
``auto``（平台默认）/``zip``（跨平台便携包）/``nsis``（Windows 安装包）/
``tar.gz``（Linux 便携包）/``deb``（Linux 安装包）/``pkg``（macOS 安装包）/
``dmg``（macOS 磁盘镜像）/``all``（平台全部）。
"""

from __future__ import annotations

# 显式导入标准库模块：兼容测试中 ``fspack.packaging.installer.subprocess.run`` 的 patch 路径。
# subprocess 为单例模块，patch 设置属性后对 base/nsis/linux/macos/zip 同样生效。
import subprocess  # noqa: F401

# re-export 公开 API 与私有辅助：保持 ``from fspack.packaging.installer import X`` 路径兼容
from fspack.packaging.installer.base import (  # noqa: F401
    _DIST_INTERMEDIATE_EXCLUDES,
    _VALID_FORMATS,
    Installer,
    _check_exe,
    _exe_exists,
    _exe_path,
    _prepare_dist,
    _py_tag,
    _release_base,
    _resolve_formats,
    _run_stage,
    build,
    build_installer,
    build_linux_installer,
    build_release,
)
from fspack.packaging.installer.linux import (  # noqa: F401
    LinuxInstaller,
    build_deb,
    build_deb_release,
    build_tarball,
    build_tarball_release,
    sign_deb_file,
)
from fspack.packaging.installer.macos import (
    MacInstaller,
    build_dmg,
    build_dmg_release,
    build_mac_installer,
    build_pkg,
    build_pkg_release,
)
from fspack.packaging.installer.nsis import (  # noqa: F401
    NsisInstaller,
    compile_installer,
    generate_nsis_script,
    sign_exe_file,
    sign_exe_files,
)
from fspack.packaging.installer.zip import (  # noqa: F401
    _make_zip,
    build_zip,
)

__all__ = [
    "Installer",
    "LinuxInstaller",
    "MacInstaller",
    "NsisInstaller",
    "build_deb",
    "build_deb_release",
    "build_dmg",
    "build_dmg_release",
    "build_installer",
    "build_linux_installer",
    "build_mac_installer",
    "build_pkg",
    "build_pkg_release",
    "build_release",
    "build_tarball",
    "build_tarball_release",
    "build_zip",
    "compile_installer",
    "generate_nsis_script",
]
