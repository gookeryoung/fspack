"""安装包生成请求模型：``ReleaseRequest`` / ``SignOptions``.

从 :mod:`fspack.packaging.installer.base` 的编排函数签名中收敛而来：
CLI/编排层的公共构建参数（项目目录/镜像/Python 版本/no_build/dist/extras/tracker）
与签名选项（Windows signtool / macOS codesign / Linux GPG）原以 10+ 个散参数
在 ``build_release``/``build_*_release``/``Installer.build_installer`` 间透传，
封装为 dataclass 后各编排函数签名收敛为 ``(req, *, 差异参数)``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from fspack.config import MirrorConfig

if TYPE_CHECKING:
    # BuildTracker 仅用于类型注解；顶部不导入 fspack.progress 避免连锁加载 rich
    from fspack.progress import BuildTracker

__all__ = ["ReleaseRequest", "SignOptions"]


@dataclass(frozen=True)
class ReleaseRequest:
    """发行包生成请求：CLI → 编排层的公共构建参数.

    字段语义与原 ``build_release`` 散参数一致：

    - ``py_version``：目标 Python 版本，``None`` 用项目配置
    - ``no_build``：用户显式声明 dist 已就绪；dist 缺失时报错而非重建
    - ``dist_dir``：自定义 dist 目录，``None`` 用 ``<project_dir>/dist``
    - ``extras``：CLI ``--extra`` 透传的分组名，``None`` 用 ``[tool.fspack]``
      ``extras`` 配置默认；非 ``None`` 时完全覆盖（仅在触发重新构建时生效）
    - ``tracker``：外部传入的打包阶段汇总表；``None`` 时编排函数自建
      （多格式共享，见 ``build_release``）
    """

    project_dir: Path
    mirror: MirrorConfig
    py_version: str | None = None
    no_build: bool = False
    dist_dir: Path | None = None
    extras: Sequence[str] | None = None
    tracker: BuildTracker | None = None


@dataclass(frozen=True)
class SignOptions:
    """发行包签名选项：Windows signtool / macOS codesign / Linux GPG.

    - ``codesign``：macOS 产物 ad-hoc 签名（``codesign --sign -``），仅对
      ``pkg``/``dmg`` 格式生效
    - ``sign_exe``：Windows exe 代码签名（signtool），需配合 ``sign_exe_certificate``
    - ``sign_deb``：Linux .deb GPG 分离签名，需配合 ``sign_deb_key``

    签名均为分发增强，失败降级 warning 不阻断构建。
    """

    codesign: bool = False
    sign_exe: bool = False
    sign_exe_certificate: Path | None = None
    sign_exe_password: str | None = None
    sign_deb: bool = False
    sign_deb_key: str | None = None


# SignOptions 不可变空单例：作为签名参数默认值（避免函数调用默认值，B008）
_NO_SIGN = SignOptions()
