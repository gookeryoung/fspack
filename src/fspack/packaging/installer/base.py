"""安装包生成基类与阶段工具：``Installer`` 抽象基类、``_run_stage``/``_run_tool``.

本模块原为 ``installer.py`` 单文件，子包化后再拆为三层：

- :mod:`fspack.packaging.installer.base`（本模块）：``Installer`` ABC 与通用编排流程
  （``build_installer()`` → 校验 → ``build_package``）、阶段/外部工具执行辅助
- :mod:`fspack.packaging.installer.dist_prep`：dist 准备（``_prepare_dist``）、
  exe 校验、发行包命名、staging 归档
- :mod:`fspack.packaging.installer.facade`：函数式入口
  （``build_installer``/``build_linux_installer``）与 ``--format`` 调度（``build_release``）
- :mod:`fspack.packaging.installer.request`：``ReleaseRequest``/``SignOptions`` 请求模型

平台专属实现拆分到子模块：
- :mod:`fspack.packaging.installer.nsis`：NSIS 脚本生成与编译（Windows）
- :mod:`fspack.packaging.installer.linux`：tar.gz 便携包与 .deb 安装包（Linux）
- :mod:`fspack.packaging.installer.macos`：.pkg 安装包与 .dmg 磁盘镜像（macOS）
- :mod:`fspack.packaging.installer.zip`：跨平台 zip 便携包
"""

from __future__ import annotations

import abc
import logging
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable, TypeVar

from fspack.config import ProjectInfo
from fspack.console import console
from fspack.exceptions import InstallerError
from fspack.packaging.installer.dist_prep import _prepare_dist
from fspack.packaging.installer.request import ReleaseRequest
from fspack.platform import Platform
from fspack.progress import BuildTracker, spinner

__all__ = ["Installer", "_run_stage", "_run_tool"]

_logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _run_stage(
    tracker: BuildTracker,
    name: str,
    fn: Callable[[], _T],
    *,
    detail: str = "",
) -> _T:
    """执行单阶段并用 ``tracker.stage`` 包装，同时显示 ``console.step`` 实时反馈。

    打包阶段（生成脚本/编译安装包/打 zip 等）统一用此函数包装，确保耗时与项数
    进入 ``BuildTracker`` 汇总表。``console.step`` 提供实时反馈，``tracker.stage``
    累积统计数据，两者职责分离不冲突。
    """
    with tracker.stage(name) as st:
        with spinner(name):
            result = fn()
        st.processed()
        if detail:
            st.set_detail(detail)
    return result


def _run_tool(
    cmd: list[str],
    *,
    not_found_msg: str,
    fail_prefix: str,
    cwd: Path | None = None,
    produces: Path | None = None,
) -> None:
    """执行外部命令行工具，统一异常处理与产物校验，失败抛 :class:`InstallerError`.

    汇聚 dpkg-deb / gpg / makensis / signtool / pkgbuild / hdiutil / codesign
    等外部工具调用的相同 try/except 骨架：``FileNotFoundError`` 转 ``not_found_msg``
    （工具未安装），``CalledProcessError`` 转 ``{fail_prefix}:\\n{stderr}``（执行失败）。

    Args:
        cmd: 命令与参数列表（如 ``["dpkg-deb", "--build", ...]``）
        not_found_msg: 工具未找到时的完整异常消息（含安装建议）
        fail_prefix: 命令执行失败时异常消息前缀（后接 ``:\\n<stderr>``）
        cwd: 子进程工作目录（如 makensis 需在 .nsi 所在目录执行），``None`` 时继承当前目录
        produces: 命令应产出的文件路径，非 ``None`` 时命令成功后校验其存在，
            缺失抛 ``InstallerError``（makensis 静默失败兜底）

    Raises:
        InstallerError: 工具未找到、命令返回非零、或 ``produces`` 声明的产物缺失
    """
    _logger.info("执行: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace", cwd=cwd)
    except FileNotFoundError as e:
        raise InstallerError(not_found_msg) from e
    except subprocess.CalledProcessError as e:
        raise InstallerError(f"{fail_prefix}:\n{e.stderr or e.stdout}") from e
    if produces is not None and not produces.is_file():
        raise InstallerError(f"{cmd[0]} 未产出安装包: {produces}")


class Installer(abc.ABC):
    """安装包生成器基类。

    封装通用编排流程：可选 ``build()`` → 校验可执行文件 → :meth:`build_package`。

    子类实现：
    - :meth:`target_platform`：目标平台（决定 ``build()`` 的 target 参数）
    - :meth:`exe_filename`：可执行文件名（Windows 为 ``<name>.exe``，Linux 为 ``<name>``）
    - :meth:`build_package`：生成具体安装包，返回产物路径

    子类可按需重写 :meth:`build_installer` 透传差异参数（如 nsis 的
    ``sign``、macos 的 ``codesign``，keyword-only 保持调用方兼容）。
    """

    @classmethod
    @abc.abstractmethod
    def target_platform(cls) -> Platform:
        """目标平台。"""

    @classmethod
    @abc.abstractmethod
    def exe_filename(cls, info: ProjectInfo) -> str:
        """返回可执行文件名（用于校验已构建产物存在）。"""

    @classmethod
    @abc.abstractmethod
    def build_package(
        cls,
        dist_dir: Path,
        info: ProjectInfo,
        release_dir: Path,
        *,
        tracker: BuildTracker,
    ) -> Path:
        """生成安装包，返回产物路径。"""

    @classmethod
    def build_installer(cls, req: ReleaseRequest) -> Path:
        """编排：可选 build → 校验可执行文件 → build_package，返回安装包路径。

        ``req.extras`` 为 CLI ``--extra`` 透传的分组名列表，``None`` 时用
        ``[tool.fspack] extras`` 配置默认；非 ``None`` 时完全覆盖配置默认
        （集合语义，与 ``build`` 子命令一致）。仅在需要重新构建时生效，
        dist 已就绪时复用构建结果，extras 不再生效。

        ``req.tracker`` 为 ``None`` 时自建 ``BuildTracker`` 并在编排结束后渲染
        汇总表；外部传入时由调用方渲染（多格式共享场景见 ``build_release``）。
        """
        own_tracker = req.tracker is None
        tk = req.tracker or BuildTracker(title="打包阶段汇总")
        dist, info = _prepare_dist(replace(req, tracker=tk), cls.target_platform())
        exe = dist / cls.exe_filename(info)
        if not exe.is_file():
            raise InstallerError(f"未找到已构建的可执行文件: {exe}（请先执行 fsp b）")
        release = dist / "release"
        result = cls.build_package(dist, info, release, tracker=tk)
        if own_tracker:
            console.rich.print(tk.summary())
        return result
