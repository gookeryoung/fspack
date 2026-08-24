"""运行已打包项目：``fsp r`` 实现.

Linux 下 ``.exe`` 用 wine 运行，原生无后缀可执行文件直跑；Windows 直跑 ``.exe``。
``--debug`` 模式绕过 loader exe，用 embed python 直接执行入口包装器，使 GUI
应用（Windows subsystem）的 stdout/stderr 可见，便于排查启动失败。
``--profile`` 模式注入打点环境变量（loader/wrapper/importtime），流式采集
stderr 并在退出后打印启动耗时汇总（见 :mod:`fspack.runner_profile`），并按
:class:`ProfileOptions` 落盘启动剖析日志与历史对比（见
:mod:`fspack.packaging.profile_log`）。
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fspack.config import AppType, EntryPoint, ProjectInfo
from fspack.config.versions import _split_t_suffix
from fspack.exceptions import FspackError
from fspack.packaging.profile_log import RUN_LOG_KIND, ProfileOptions
from fspack.runner_profile import PROFILE_ENV, run_with_profile

__all__ = ["RunOptions", "run"]

_logger = logging.getLogger(__name__)

# frozen 不可变单例，作为 RunOptions.profile 的默认值安全共享
_DEFAULT_PROFILE = ProfileOptions()


@dataclass(frozen=True)
class RunOptions:
    """运行选项（``fsp r`` 的调试模式/入口选择/启动剖析开关）.

    - ``debug``：绕过 loader exe，用 embed python 直跑入口包装器，
      使 GUI 应用的 stdout/stderr 可见
    - ``entry``：多入口项目中要运行的入口名；``None`` 用默认入口
    - ``profile``：启动剖析选项（打点 + 日志落盘/对比）；默认值是不可变
      的 :class:`ProfileOptions`，可安全共享
    """

    debug: bool = False
    entry: str | None = None
    profile: ProfileOptions = _DEFAULT_PROFILE


def run(
    project: Path,
    rest_args: list[str] | None = None,
    options: RunOptions | None = None,
) -> None:
    """运行 dist 下的可执行文件。

    ``options.debug=True`` 时绕过 loader exe，用 embed python 直接跑入口
    脚本，使 GUI 应用（Windows subsystem）的 stdout/stderr 可见。

    ``options.entry`` 指定多入口项目中要运行的入口名（与 ``[project.scripts]``/
    ``[tool.fspack.entries]`` 键匹配）；单入口项目或 ``entry=None`` 时使用默认入口。

    ``options.profile.enabled=True`` 时注入 ``FSPACK_LOADER_VERBOSE``/
    ``FSPACK_TIMING``/``PYTHONPROFILEIMPORTTIME`` 环境变量激活三级打点，
    子进程退出后打印启动耗时汇总（loader 阶段/环境准备/import 细分/用户
    入口执行），定位启动性能优化点。旧 dist 的 wrapper 无 timing 打点时
    汇总缺 wrapper 段，重新构建后完整。

    ``options.profile``（需 ``enabled=True``）：剖析数据落盘为 JSON 日志
    （默认 ``<项目>/.benchmarks/fsp-r-<时间戳>.json``，``out`` 可指定目录
    或 ``.json`` 文件）；``compare`` 为 ``"last"`` 时与最近一次启动剖析
    日志对比，否则按基准文件路径对比（差异表格标红回归/标绿改善）。
    """
    opts = options or RunOptions()
    info = ProjectInfo.from_dir(project)
    rest = rest_args or []
    ep = _select_entry(info, opts.entry)
    profile = opts.profile.enabled
    if opts.debug:
        cmd = _build_debug_cmd(project, ep, info.py_version) + rest
        debug_env: dict[str, str] = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if profile:
            debug_env.update(PROFILE_ENV)
        if platform.system() != "Windows":
            debug_env["PYTHONHOME"] = str(Path(project) / "dist" / "runtime" / "python")
        env: dict[str, str] | None = debug_env
    else:
        exe = _find_exe(project, ep.name)
        if exe is None:
            raise FspackError(f"未找到已构建的可执行文件: {project}/dist/{ep.name}[.exe]（请先执行 fsp b）")
        cmd = _build_cmd(exe) + rest
        env = {**os.environ, **PROFILE_ENV} if profile else None
    _logger.info("运行入口 %s: %s", ep.name, " ".join(cmd))
    if profile:
        debug = opts.debug
        profile_opts = opts.profile

        def _on_summary(data: dict[str, Any]) -> None:
            log_data = _run_log_data(data, info, ep.name, debug)
            _save_and_compare_run_profile(log_data, Path(project), profile_opts)

        returncode = run_with_profile(cmd, env, on_summary=_on_summary)
    else:
        completed = subprocess.run(cmd, check=False, env=env)
        returncode = completed.returncode
    if returncode != 0:
        if ep.app_type in (AppType.GUI, AppType.WEB) and not opts.debug:
            _logger.warning(
                "%s 应用输出被 Windows subsystem 吞掉，如需查看输出请用 `fspack r --debug`",
                ep.app_type.value.upper(),
            )
        raise FspackError(f"程序退出码非零: {returncode}")


def _run_log_data(
    data: dict[str, Any],
    info: ProjectInfo,
    entry_name: str,
    debug: bool,
) -> dict[str, Any]:
    """把剖析采集数据组装为启动剖析日志 dict（schema ``fspack/run-profile/1``）.

    采集数据内部为毫秒，日志统一秒（与构建日志 wall_time 单位一致），
    保留 4 位小数。
    """
    # 延迟导入：profile_log 的对比渲染链触发 rich 加载，仅在 profile 运行时执行
    from fspack.packaging.profile_log import RUN_PROFILE_LOG_SCHEMA

    log: dict[str, Any] = {
        "schema": RUN_PROFILE_LOG_SCHEMA,
        "created": datetime.now().isoformat(timespec="seconds"),
        "project": {"name": info.name, "version": info.version},
        "python": sys.version.split()[0],
        "platform": platform.system().lower(),
        "entry": entry_name,
        "debug": debug,
        "wall_time": round(data["wall_ms"] / 1000.0, 4),
        "returncode": data["returncode"],
        "stages": [{"name": n, "elapsed": round(ms / 1000.0, 4)} for n, ms in data["stages"]],
        "top_imports": [{"name": n, "elapsed": round(ms / 1000.0, 4)} for n, ms in data["top_imports"]],
        "entry_imports": [{"name": n, "elapsed": round(ms / 1000.0, 4)} for n, ms in data["entry_imports"]],
        "top_self": [{"name": n, "elapsed": round(ms / 1000.0, 4)} for n, ms in data["top_self"]],
    }
    # 父进程侧未细分拆分（有首/末行实测才有）：head=进程创建与映像加载、
    # tail=进程收尾、blind=其余盲区；含管道延迟噪声，仅供逐次诊断不参与对比
    if "gap_breakdown" in data:
        log["gap_breakdown"] = {k: round(v / 1000.0, 4) for k, v in data["gap_breakdown"].items()}
    return log


def _save_and_compare_run_profile(log_data: dict[str, Any], project: Path, opts: ProfileOptions) -> None:
    """落盘启动剖析日志并按需渲染历史对比（``--profile-out``/``--profile-compare``）.

    与构建侧 :func:`fspack.packaging.pipeline.executor._save_and_compare_profile`
    对称：落盘默认目录 ``<项目>/.benchmarks/``（前缀 ``fsp-r-`` 与构建日志
    ``fsp-b-`` 区分）；对比逻辑复用
    :func:`fspack.packaging.profile_log.compare_with_baseline`（日志类别
    :data:`RUN_LOG_KIND`：前缀过滤 + 5ms 阶段显著阈值 + 中文文案标签）。
    """
    # 延迟导入：profile_log 触发 rich 渲染链加载，仅在 profile 运行时执行
    # （RUN_LOG_KIND 已在顶部导入；rich 渲染链在 compare/save 函数体内才触发）
    from fspack.packaging.profile_log import (
        DEFAULT_LOG_DIR,
        RUN_LOG_PREFIX,
        compare_with_baseline,
        save_profile_log,
    )

    default_dir = project / DEFAULT_LOG_DIR
    log_path = save_profile_log(log_data, opts.out or default_dir, prefix=RUN_LOG_PREFIX)
    _logger.info("启动剖析日志已写入: %s", log_path)
    compare_with_baseline(log_path, default_dir, opts.compare, kind=RUN_LOG_KIND)


def _select_entry(info: ProjectInfo, entry: str | None) -> EntryPoint:
    """从项目入口中选择要运行的入口。

    ``entry=None`` 时按 GUI 优先、同类型按字母排序选默认入口
    （见 :attr:`ProjectInfo.default_entry`）；``entry`` 非空时按名匹配，
    未找到则报错列出可用入口。
    """
    all_entries = info.all_entries
    if entry is None:
        ep = info.default_entry
        if len(all_entries) > 1:
            names = ", ".join(e.name for e in all_entries)
            _logger.info("多入口项目未指定 --entry，使用默认入口 %s（可用: %s）", ep.name, names)
        return ep
    for ep in all_entries:
        if ep.name == entry:
            return ep
    available = ", ".join(ep.name for ep in all_entries)
    raise FspackError(f"未找到入口: {entry}（可用入口: {available}）")


def _find_exe(project: Path, name: str) -> Path | None:
    """按当前平台查找 dist 下的可执行文件。

    Linux 优先找原生无后缀可执行文件，回退 .exe（wine 运行）；
    Windows 找 .exe。
    """
    dist = Path(project) / "dist"
    if platform.system() == "Linux":
        native = dist / name
        if native.is_file():
            return native
    win = dist / f"{name}.exe"
    if win.is_file():
        return win
    return None


def _build_cmd(exe: Path) -> list[str]:
    """构造运行命令：Linux 下 .exe 用 wine，原生可执行文件直跑."""
    if exe.suffix == ".exe" and platform.system() == "Linux":
        wine = shutil.which("wine") or "wine"
        return [wine, str(exe)]
    return [str(exe)]


def _build_debug_cmd(project: Path, ep: EntryPoint, py_version: str | None = None) -> list[str]:
    """构造调试命令：用 embed python 直跑入口包装器（绕过 GUI loader）。

    Windows 用 ``dist/runtime/python.exe``，Linux 用 ``dist/runtime/python/bin/python3.X``。
    embed python 是 console 子系统，print 输出可见；运行 ``dist/_entry_<name>.py``
    包装器（与 loader 一致），由 wrapper 设置 sys.path、Qt 插件路径与包上下文
    后调 :func:`runpy.run_module`/:func:`runpy.run_path` 执行用户入口，使相对
    导入可用。

    Linux 侧用 glob 枚举 ``bin/`` 下条目后按候选集精确匹配（``pythonX.Y`` >
    ``pythonX`` > ``python3`` > ``python``），避免 ``python3.11-config`` 等
    带后缀工具被 ``sorted(glob)[0]`` 误选为解释器。
    """
    dist = Path(project) / "dist"
    wrapper = dist / f"_entry_{ep.name}.py"
    if not wrapper.is_file():
        raise FspackError(f"未找到入口包装器: {wrapper}（请先执行 fsp b）")
    if platform.system() == "Windows":
        py = dist / "runtime" / "python.exe"
    else:
        py = _find_bin_python(dist, py_version)
        if py is None:
            raise FspackError(f"未找到 embed python: {dist / 'runtime' / 'python' / 'bin'}（请先执行 fsp b）")
    if not py.is_file():
        raise FspackError(f"未找到 embed python: {py}（请先执行 fsp b）")
    return [str(py), str(wrapper)]


def _find_bin_python(dist: Path, py_version: str | None) -> Path | None:
    """从 ``dist/runtime/python/bin`` 中按候选集选取 python 可执行文件.

    有版本信息时 glob 仅作枚举手段，从排序结果中选首个名字落在候选集内的条目：
    ``python<major>.<minor>`` > ``python<major>`` > ``python3`` > ``python``，
    排除 ``python3.11-config`` 等带后缀工具（其字典序可能先于裸解释器）。
    无版本信息（或候选集未命中）时回退到首个无 ``-`` 后缀的 ``python*`` 条目
    （如 ``python3.11``，同样排除 ``-config`` 等工具），与旧 sorted-glob 行为兼容。
    """
    bin_dir = dist / "runtime" / "python" / "bin"
    entries = sorted(bin_dir.glob("python*"))
    if py_version:
        # 剥离 free-threaded build 的 t 后缀：astral-sh standalone 二进制名
        # 在 free-threaded 变体下为 python3.13t（与 python.org 一致）
        base, is_t = _split_t_suffix(py_version)
        major, minor = [*base.split("."), "", ""][:2]
        suffix = "t" if is_t else ""
        candidates = {f"python{major}.{minor}{suffix}", f"python{major}", "python3", "python"}
        exact = next((p for p in entries if p.name in candidates), None)
        if exact is not None:
            return exact
    return next((p for p in entries if "-" not in p.name), None)
