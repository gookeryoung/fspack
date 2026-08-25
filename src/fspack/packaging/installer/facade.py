"""函数式入口与 ``--format`` 调度：``build_installer``/``build_linux_installer``/``build_release``.

从 :mod:`fspack.packaging.installer.base` 拆分而来，聚集「发行包调度」职责：

- ``build_installer``/``build_linux_installer``/``build_mac_installer``：函数式
  API，委托对应平台子类（nsis/linux/macos）
- ``_resolve_formats``：``--format`` 取值解析（auto/all/单一格式 + 平台兼容校验）
- ``build_release``：按 ``--format`` 调度生成一种或多种格式产物

``build_release`` 支持的格式：
``auto``（平台默认）/``zip``（跨平台便携包）/``7z``（跨平台高压缩便携包，
需系统 7-Zip）/``nsis``（Windows 安装包）/``tar.gz``（Linux 便携包）/
``deb``（Linux 安装包）/``pkg``（macOS 安装包）/``dmg``（macOS 磁盘镜像）/
``all``（平台全部）。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fspack.console import console
from fspack.exceptions import InstallerError
from fspack.packaging.installer.request import _NO_SIGN, ReleaseRequest, SignOptions
from fspack.platform import Platform, detect_platform
from fspack.progress import BuildTracker

__all__ = ["_VALID_FORMATS", "_resolve_formats", "build_installer", "build_linux_installer", "build_release"]

# 发行包格式取值校验
_VALID_FORMATS = ("auto", "zip", "7z", "nsis", "tar.gz", "deb", "pkg", "dmg", "all")

# 归档类格式：staging 目录同名（<base>），多归档场景共享 staging 消除重复 copytree
_ARCHIVE_FORMATS = frozenset({"tar.gz", "zip", "7z"})


def build_installer(req: ReleaseRequest, *, sign: SignOptions = _NO_SIGN) -> Path:
    """编排：可选 build → 生成 NSIS 脚本 → 编译安装包，返回安装包路径。"""
    return NsisInstaller.build_installer(req, sign=sign)


def build_linux_installer(req: ReleaseRequest) -> Path:
    """编排：可选 build → tar.gz 便携包 → .deb 安装包，返回 .deb 路径。"""
    return LinuxInstaller.build_installer(req)


def _resolve_formats(fmt: str, target: Platform) -> list[str]:
    """将 ``--format`` 取值解析为具体格式列表。

    - ``auto``：平台默认（Windows=nsis，Linux=tar.gz+deb，macOS=pkg+dmg），向后兼容
    - ``all``：平台全部（Windows=nsis+zip+7z，Linux=tar.gz+deb+zip+7z，
      macOS=pkg+dmg+zip+7z）
    - 单一格式：校验平台兼容性（nsis 仅 Windows，tar.gz/deb 仅 Linux，
      pkg/dmg 仅 macOS，zip/7z 跨平台）
    """
    if fmt not in _VALID_FORMATS:
        raise InstallerError(f"未知 --format 取值: {fmt}，可选: {', '.join(_VALID_FORMATS)}")
    # auto / all 按平台查表
    platform_defaults: dict[Platform, tuple[list[str], list[str]]] = {
        Platform.WINDOWS: (["nsis"], ["nsis", "zip", "7z"]),
        Platform.MACOS: (["pkg", "dmg"], ["pkg", "dmg", "zip", "7z"]),
        Platform.LINUX: (["tar.gz", "deb"], ["tar.gz", "deb", "zip", "7z"]),
    }
    defaults, all_formats = platform_defaults[target]
    if fmt == "auto":
        return defaults
    if fmt == "all":
        return all_formats
    # 单一格式：校验平台兼容性
    if fmt == "nsis" and target is not Platform.WINDOWS:
        raise InstallerError("NSIS 安装包仅支持 Windows 目标")
    if fmt in ("tar.gz", "deb") and target is not Platform.LINUX:
        raise InstallerError(f"{fmt} 格式仅支持 Linux 目标")
    if fmt in ("pkg", "dmg") and target is not Platform.MACOS:
        raise InstallerError(f"{fmt} 格式仅支持 macOS 目标")
    return [fmt]


def build_release(
    req: ReleaseRequest,
    *,
    target: Platform | None = None,
    fmt: str = "auto",
    sign: SignOptions = _NO_SIGN,
) -> list[Path]:
    """按 ``--format`` 调度生成发行包，返回产物路径列表。

    多格式时按 ``_resolve_formats`` 顺序逐个生成，每次复用同一 dist（首个格式
    内部触发 build，后续格式 ``no_build=True`` 跳过 build 直接打包）。返回的
    列表顺序与生成顺序一致。

    归档类格式（tar.gz/zip/7z）的 staging 目录同名（``<base>``），多归档
    场景共享：首个归档保留 staging、中间归档复用并保留、末个归档复用并清理，
    消除中间的全量 copytree（如 Linux ``all`` 场景 tar.gz/zip/7z 仅首格式
    copytree 一次）。

    所有格式共享同一 ``BuildTracker``（覆盖 ``req.tracker``），最终统一渲染
    「打包阶段汇总」表（与 ``build()`` 的「构建阶段汇总」对应）。单格式函数
    （``build_zip`` 等）单独调用时各自渲染汇总表。

    Args:
        req: 公共构建参数（project_dir/mirror/py_version/no_build/dist_dir/extras）
        target: 目标平台，``None`` 时用当前平台
        fmt: ``--format`` 取值（auto/zip/7z/nsis/tar.gz/deb/pkg/dmg/all）
        sign: 签名选项（codesign 仅 pkg/dmg，sign_exe 仅 nsis，sign_deb 仅 deb）
    """
    resolved_target = target or detect_platform()
    formats = _resolve_formats(fmt, resolved_target)
    tracker = BuildTracker(title="打包阶段汇总")
    # 归档类格式共享 staging：<base> 同名（tar.gz/zip/7z），多归档场景复用消除重复 copytree
    archive_formats = [f for f in formats if f in _ARCHIVE_FORMATS]
    share_staging = len(archive_formats) >= 2
    outputs: list[Path] = []
    for index, f in enumerate(formats):
        # 首个格式负责 build（沿用 req.no_build），后续格式跳过 build 复用同一 dist；
        # extras 仅在首个格式（可能触发 build）透传，后续格式复用 dist 无需 extras
        fmt_req = replace(
            req, no_build=req.no_build or index > 0, extras=req.extras if index == 0 else None, tracker=tracker
        )
        # 归档类格式在共享 staging 场景的旗标：首个保留、中间复用并保留、末个复用并清理
        # （单归档或非归档格式均走默认 False，行为与单格式直调一致）
        if share_staging and f in _ARCHIVE_FORMATS:
            keep = f != archive_formats[-1]
            reuse = f != archive_formats[0]
        else:
            keep = reuse = False
        if f == "zip":
            outputs.append(_facade.build_zip(fmt_req, target=resolved_target, keep_staging=keep, reuse_staging=reuse))
        elif f == "7z":
            outputs.append(
                _facade.build_sevenzip(fmt_req, target=resolved_target, keep_staging=keep, reuse_staging=reuse)
            )
        elif f == "nsis":
            outputs.append(NsisInstaller.build_installer(fmt_req, sign=sign))
        elif f == "tar.gz":
            outputs.append(_facade.build_tarball_release(fmt_req, keep_staging=keep))
        elif f == "deb":
            outputs.append(_facade.build_deb_release(fmt_req, sign=sign))
        elif f == "pkg":
            outputs.append(_facade.build_pkg_release(fmt_req, codesign=sign.codesign))
        elif f == "dmg":
            outputs.append(_facade.build_dmg_release(fmt_req, codesign=sign.codesign))
    console.rich.print(tracker.summary())
    return outputs


# ---- 子模块 re-export（末尾导入避免循环依赖）----
# 子类模块从 base/dist_prep 导入 Installer 基类与公共辅助，须在所有定义之后导入。

# 通过 facade 解析可 patch 函数：兼容测试 monkeypatch "fspack.packaging.installer.build_zip"
# 等函数路径。build_release 内部调用经 ``_facade.<fn>`` 在运行时动态查找，使 patch 生效。
import fspack.packaging.installer as _facade  # noqa: E402
from fspack.packaging.installer.linux import LinuxInstaller  # noqa: E402
from fspack.packaging.installer.nsis import NsisInstaller  # noqa: E402
