"""``fsp doctor --test`` / ``--bench`` 模板构建测试.

从 ``assets/templates/`` 加载所有项目模板，逐个复制到临时目录并执行
:func:`fspack.builder.build`，收集成功/失败/耗时/产物大小/运行验证结果，
输出汇总表格。``--bench`` 额外启用 ``profile=True`` 输出各阶段耗时报告，
并通过 :mod:`fspack.doctor.bench` 保存基准并与历史横向对比。

运行验证统一用超时策略处理 CLI/GUI/Web 应用，无需依赖 ``app_type``：

- 进程自行退出且退出码 ``0`` → 成功（CLI 正常执行完成）
- 进程自行退出且退出码非 ``0`` → 失败（启动崩溃，捕获 stderr 首行）
- 超时未退出 → 视为成功（GUI/Web 进入事件循环不退出），主动终止

debug 模式优先用 embed python + 入口包装器（模拟 ``fsp r --debug``）：
console 子系统 stdout 可见，wrapper 设置 Qt 插件路径、Tcl/Tk 环境变量、
site-packages sys.path 等，避免 GUI 应用因环境变量缺失启动失败。debug
模式不可用时回退直跑 loader exe（Linux 下 ``.exe`` 用 wine）。

汇总表格与性能分析渲染已拆分至 :mod:`fspack.doctor.template_report`。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from fspack.console import console
from fspack.doctor.bench import _save_and_compare_bench
from fspack.doctor.envs import _dir_size, _format_size
from fspack.doctor.models import TemplateBuildResult, TemplateRunResult
from fspack.doctor.template_report import _print_template_build_summary
from fspack.templates.registry import _TEMPLATE_SKIP_DIRS, _TEMPLATE_SKIP_SUFFIXES

if TYPE_CHECKING:
    from fspack.templates.registry import Template

__all__ = [
    "_build_debug_cmd",
    "_build_run_cmd",
    "_build_single_template",
    "_find_debug_python",
    "_find_dist_exe",
    "_find_wrapper",
    "_run_template",
    "run_doctor_bench",
    "run_doctor_test",
]

_logger = logging.getLogger(__name__)

# 运行验证超时（秒）：CLI 应用通常 <1s 退出，GUI/Web 进入事件循环不退出。
# 5s 给慢启动足够余量，超时后视为「启动成功」（GUI 正常运行）并主动终止。
_RUN_TIMEOUT_SEC = 5.0

# 终止进程后的等待时间（秒）：terminate 后给进程 2s 清理，仍不退出则 kill。
_TERMINATE_GRACE_SEC = 2.0

# copytree 复制模板时跳过的目录：在 registry 扫描过滤（node_modules/__pycache__ 等）
# 基础上追加构建产物目录（dist/deploy 为 ``fsp b`` 与前端构建输出）。模板源目录中
# 这些均为本地测试残留（git 已忽略），带入 doctor 临时环境会破坏构建语义——
# node_modules 含 pnpm 硬链接且 virtualStoreDir 路径不匹配，触发交互式询问在
# 非 TTY 下挂起直至超时；dist/deploy 非空会让前端阶段误判产物就绪跳过构建。
_COPY_IGNORE_DIRS: frozenset[str] = _TEMPLATE_SKIP_DIRS | frozenset({"dist", "deploy"})


def _copy_ignore(_src: str, names: list[str]) -> set[str]:
    """copytree ignore 回调：跳过本地开发/构建残留目录与编译产物文件.

    :param _src: copytree 传入的源目录（未使用）
    :param names: 当前层级目录条目名列表
    :return: 应忽略的条目名集合
    """
    return {n for n in names if n in _COPY_IGNORE_DIRS or Path(n).suffix in _TEMPLATE_SKIP_SUFFIXES}


def _find_dist_exe(proj_dir: Path, name: str) -> Path | None:
    """在 ``proj_dir/dist/`` 下查找项目可执行文件.

    与 :func:`fspack.runner._find_exe` 同逻辑，但接收 ``proj_dir`` 而非
    project（doctor 在临时工作目录下构建，project 路径即 ``proj_dir``）。

    Linux 优先原生无后缀可执行文件，回退 ``.exe``（用 wine 运行）；
    Windows/macOS 仅查 ``.exe``。

    :param proj_dir: 项目根目录（含 ``dist/``）
    :param name: 可执行文件名（取自 ``pyproject.toml`` 的 ``name``）
    :return: 可执行文件路径或 ``None``（未找到）
    """
    from fspack.platform import Platform, detect_platform

    dist = proj_dir / "dist"
    candidates: list[Path]
    if detect_platform() is Platform.LINUX:
        candidates = [dist / name, dist / f"{name}.exe"]
    else:
        candidates = [dist / f"{name}.exe"]
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _build_run_cmd(exe: Path) -> list[str]:
    """构造运行命令：Linux 下 ``.exe`` 用 wine，原生可执行文件直跑.

    与 :func:`fspack.runner._build_cmd` 同逻辑，独立于 runner 模块以避免
    doctor ↔ runner 循环依赖。wine 不在 PATH 时回退字符串 ``"wine"``，
    :func:`_run_template` 会捕获 :class:`FileNotFoundError` 报告未安装。

    仅作为 debug 模式不可用时的回退方案（直跑 loader exe）。
    """
    from fspack.platform import Platform, detect_platform

    if exe.suffix == ".exe" and detect_platform() is Platform.LINUX:
        wine = shutil.which("wine") or "wine"
        return [wine, str(exe)]
    return [str(exe)]


def _find_debug_python(proj_dir: Path, py_version: str | None = None) -> Path | None:
    """查找 debug 模式用的 embed python 路径.

    Windows 用 ``dist/runtime/python.exe``，Linux/macOS 用
    ``dist/runtime/python/bin/python3.X``（standalone python）。

    Linux/macOS 侧用 glob 枚举 ``bin/`` 下条目后按候选集精确匹配
    （``pythonX.Y`` > ``pythonX`` > ``python3`` > ``python``），避免
    ``python3.11-config`` 等带后缀工具被 ``sorted(glob)[0]`` 误选。
    """
    from fspack.platform import Platform, detect_platform

    dist = proj_dir / "dist"
    if detect_platform() is Platform.WINDOWS:
        py = dist / "runtime" / "python.exe"
        return py if py.is_file() else None
    # 延迟导入：复用 runner 的候选集匹配实现，避免模块级拉起 runner 及其重依赖
    from fspack.runner import _find_bin_python

    return _find_bin_python(dist, py_version)


def _find_wrapper(proj_dir: Path, name: str) -> Path | None:
    """查找入口包装器 ``dist/_entry_<name>.py`` 路径."""
    wrapper = proj_dir / "dist" / f"_entry_{name}.py"
    return wrapper if wrapper.is_file() else None


def _build_debug_cmd(
    proj_dir: Path,
    name: str,
    py_version: str | None = None,
) -> tuple[list[str], dict[str, str]] | None:
    """构造 debug 模式运行命令：embed python + 入口包装器.

    模拟 ``fsp r --debug`` 行为：用 console 子系统的 embed python 直跑
    ``_entry_<name>.py`` 包装器，使 stdout/stderr 可见（GUI subsystem
    下被 Windows 吞掉），且 wrapper 设置 Qt 插件路径、Tcl/Tk 环境变量、
    site-packages sys.path 等，避免 GUI 应用（PySide2/PyQt5/tkinter）
    因环境变量缺失启动失败。

    与直跑 loader exe 的差异：

    - debug 模式用 console 子系统，``print`` 输出可见，便于诊断
    - Linux 用原生 standalone python（不需 wine），避免 wine 下 GUI 应用
      缺 X11/Qt 插件路径问题
    - wrapper 显式设置 ``PYTHONHOME``（Linux）使 standalone python 找到标准库

    :param proj_dir: 项目根目录（含 ``dist/``）
    :param name: 入口名（``pyproject.toml`` 的 ``name``）
    :param py_version: 项目 Python 版本（如 ``3.11.9``），用于 Linux/macOS
        侧 ``bin/`` 下解释器候选集精确匹配；``None`` 时仅匹配通用候选名
    :return: ``(cmd, env)`` 或 ``None``（wrapper/embed python 缺失，调用方回退直跑 exe）
    """
    from fspack.platform import Platform, detect_platform

    py = _find_debug_python(proj_dir, py_version)
    wrapper = _find_wrapper(proj_dir, name)
    if py is None or wrapper is None:
        return None
    cmd = [str(py), str(wrapper)]
    env: dict[str, str] = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if detect_platform() is not Platform.WINDOWS:
        env["PYTHONHOME"] = str(proj_dir / "dist" / "runtime" / "python")
    return cmd, env


def _run_template(
    cmd: list[str],
    env: Mapping[str, str] | None = None,
    *,
    timeout: float = _RUN_TIMEOUT_SEC,
) -> TemplateRunResult:
    """运行已构建的可执行文件，验证可调用性.

    统一用超时策略处理 CLI/GUI/Web 应用，无需依赖 ``app_type`` 字段：

    - 进程自行退出且退出码 ``0`` → 成功（CLI 正常执行完成）
    - 进程自行退出且退出码非 ``0`` → 失败（启动崩溃，捕获 stderr 首行）
    - 超时未退出 → 视为成功（GUI/Web 进入事件循环不退出），
      主动 ``terminate`` + ``kill``，``exit_code=None``

    :param cmd: 运行命令（debug 模式为 ``[python, wrapper]``，回退为 ``[exe]``/``[wine, exe]``）
    :param env: 环境变量（debug 模式含 ``PYTHONHOME``/``PYTHONUNBUFFERED``），``None`` 继承当前环境
    :param timeout: 超时秒数（默认 :data:`_RUN_TIMEOUT_SEC`）
    :return: 运行验证结果
    """
    start = time.perf_counter()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except (OSError, ValueError) as exc:
        elapsed = time.perf_counter() - start
        return TemplateRunResult(
            success=False,
            timed_out=False,
            exit_code=None,
            duration_sec=elapsed,
            error=f"启动失败: {exc}",
        )

    try:
        _stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 超时未退出 → 视为 GUI/Web 事件循环正常运行，主动终止
        proc.terminate()
        try:
            proc.communicate(timeout=_TERMINATE_GRACE_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        elapsed = time.perf_counter() - start
        _logger.debug("模板运行超时（视为 GUI/Web 事件循环正常）: %s", " ".join(cmd))
        return TemplateRunResult(
            success=True,
            timed_out=True,
            exit_code=None,
            duration_sec=elapsed,
        )

    elapsed = time.perf_counter() - start
    if proc.returncode == 0:
        return TemplateRunResult(
            success=True,
            timed_out=False,
            exit_code=0,
            duration_sec=elapsed,
        )
    stderr_first = (stderr or "").splitlines()[0] if stderr else ""
    if len(stderr_first) > 200:
        stderr_first = stderr_first[:197] + "..."
    _logger.warning("模板运行失败 %s: 退出码 %s", " ".join(cmd), proc.returncode)
    return TemplateRunResult(
        success=False,
        timed_out=False,
        exit_code=proc.returncode,
        duration_sec=elapsed,
        error=stderr_first or f"退出码 {proc.returncode}",
    )


def _build_single_template(  # pragma: no cover
    template: Template,
    work_dir: Path,
    *,
    bench: bool = False,
) -> TemplateBuildResult:
    """构建单个模板项目，返回结果.

    :param template: 项目模板（统一 :class:`Template`，含 ``dir`` 字段用于 ``copytree``）
    :param work_dir: 临时工作目录（构建在此目录下的子目录进行）
    :param bench: ``True`` 时启用 ``profile=True``，输出详细性能报告
    """
    from fspack.builder import build
    from fspack.config import BuildOptions, ProjectInfo, get_mirror
    from fspack.platform import detect_platform

    proj_dir = work_dir / template.id
    shutil.copytree(template.dir, proj_dir, dirs_exist_ok=True, ignore=_copy_ignore)

    opts = BuildOptions(no_size_report=True)
    mirror = get_mirror()
    target = detect_platform()

    start = time.perf_counter()
    try:
        build(
            proj_dir,
            mirror,
            target=target,
            options=opts,
            profile=bench,
        )
    except Exception as e:
        elapsed = time.perf_counter() - start
        err_msg = str(e)[:200]
        _logger.warning("模板 %s 构建失败: %s", template.id, err_msg)
        return TemplateBuildResult(
            template_id=template.id,
            success=False,
            duration_sec=elapsed,
            error=err_msg,
        )

    elapsed = time.perf_counter() - start
    dist_dir = proj_dir / "dist"
    dist_size = _dir_size(dist_dir) if dist_dir.is_dir() else 0

    # 构建成功后解析项目入口：多入口项目产出的 exe 名是 [tool.fspack.entries]
    # 的键（如 cli/gui/web），不等于 template.name。用 ProjectInfo.default_entry
    # 取默认入口名（GUI 优先、同类型按字母排序，与 `fsp r` 默认行为一致），
    # 避免多入口项目跳过运行验证。
    info = ProjectInfo.from_dir(proj_dir)
    entry_name = info.default_entry.name

    # 入口计数：按项目声明的入口逐个检查 dist 顶层产物（<name>.exe 或无后缀
    # <name>）。Linux/macOS 产物无 .exe 后缀，仅统计 *.exe 会漏计；且多入口
    # 项目（cli/gui/web）非默认入口的无后缀产物也须计入，故按入口名精确匹配
    if dist_dir.is_dir():
        entry_count = sum(
            1 for ep in info.all_entries if (dist_dir / f"{ep.name}.exe").is_file() or (dist_dir / ep.name).is_file()
        )
    else:
        entry_count = 0

    # 运行验证：优先用 debug 模式（embed python + wrapper），模拟 `fsp r --debug`：
    # console 子系统 stdout 可见，wrapper 设置 Qt 插件路径、Tcl/Tk 环境变量、
    # site-packages sys.path 等，避免 GUI 应用因环境变量缺失启动失败。
    # debug 模式不可用（wrapper/embed python 缺失）时回退直跑 loader exe。
    debug = _build_debug_cmd(proj_dir, entry_name, info.py_version)
    if debug is not None:
        cmd, env = debug
        run_result = _run_template(cmd, env)
    else:
        exe = _find_dist_exe(proj_dir, entry_name)
        if exe is not None:
            run_result = _run_template(_build_run_cmd(exe))
        else:
            run_result = None
        if exe is None and entry_count > 0:
            _logger.debug("模板 %s 未找到入口 %s 的可执行文件，跳过运行验证", template.id, entry_name)

    return TemplateBuildResult(
        template_id=template.id,
        success=True,
        duration_sec=elapsed,
        dist_size=dist_size,
        entry_count=entry_count,
        run_result=run_result,
    )


def run_doctor_test() -> None:  # pragma: no cover
    """运行所有项目模板构建，打印汇总结果.

    从 ``assets/templates/`` 加载所有项目模板，逐个复制到临时目录并执行
    :func:`fspack.builder.build`，收集成功/失败/耗时，输出汇总表格。

    用于验证打包流程对所有模板项目的兼容性，CI 中可作为回归门禁。
    """
    from fspack.templates.project_template import ProjectTemplate

    templates = ProjectTemplate.list_all()
    if not templates:
        console.warn("未找到项目模板")
        return

    console.step(f"模板构建测试（{len(templates)} 个模板）")
    results: list[TemplateBuildResult] = []

    with tempfile.TemporaryDirectory(prefix="fsp-doctor-test-") as tmp:
        work_dir = Path(tmp)
        for i, tpl in enumerate(templates, 1):
            console.rich.print(f"[cyan][{i}/{len(templates)}][/cyan] 构建 {tpl.id} ...")
            result = _build_single_template(tpl, work_dir, bench=False)
            results.append(result)
            if result.success:
                console.rich.print(
                    f"  [green]√[/green] 成功 ({result.duration_sec:.1f}s, {_format_size(result.dist_size)})"
                )
            else:
                console.rich.print(f"  [red]×[/red] 失败: {result.error}")

    _print_template_build_summary(results, bench=False)


def run_doctor_bench() -> None:  # pragma: no cover
    """运行所有项目模板构建，收集性能数据，输出性能分析报告.

    与 :func:`run_doctor_test` 相同的构建流程，但每个模板启用
    ``profile=True``，输出详细的各阶段耗时报告。最后打印汇总表格与
    性能分析（耗时排名、产物大小排名、总时间）。

    用于建立性能基准，后续优化措施可与此基准对比评估效果。
    """
    from fspack.templates.project_template import ProjectTemplate

    templates = ProjectTemplate.list_all()
    if not templates:
        console.warn("未找到项目模板")
        return

    console.step(f"性能基准测试（{len(templates)} 个模板）")
    results: list[TemplateBuildResult] = []

    with tempfile.TemporaryDirectory(prefix="fsp-doctor-bench-") as tmp:
        work_dir = Path(tmp)
        for i, tpl in enumerate(templates, 1):
            console.rich.print(f"[cyan][{i}/{len(templates)}][/cyan] 基准构建 {tpl.id} ...")
            result = _build_single_template(tpl, work_dir, bench=True)
            results.append(result)
            if result.success:
                console.rich.print(
                    f"  [green]√[/green] 成功 ({result.duration_sec:.1f}s, {_format_size(result.dist_size)})"
                )
            else:
                console.rich.print(f"  [red]×[/red] 失败: {result.error}")

    _print_template_build_summary(results, bench=True)
    _save_and_compare_bench(results)
