"""``fsp doctor --test`` / ``--bench`` 模板构建测试.

从 ``assets/templates/`` 加载所有项目模板，逐个复制到临时目录并执行
:func:`fspack.builder.build`，收集成功/失败/耗时/产物大小/运行验证结果，
输出汇总表格。``--bench`` 额外启用 ``profile=True`` 输出各阶段耗时报告，
并通过 :mod:`fspack.doctor_bench` 保存基准并与历史横向对比。

运行验证统一用超时策略处理 CLI/GUI/Web 应用，无需依赖 ``app_type``：

- 进程自行退出且退出码 ``0`` → 成功（CLI 正常执行完成）
- 进程自行退出且退出码非 ``0`` → 失败（启动崩溃，捕获 stderr 首行）
- 超时未退出 → 视为成功（GUI/Web 进入事件循环不退出），主动终止

debug 模式优先用 embed python + 入口包装器（模拟 ``fsp r --debug``）：
console 子系统 stdout 可见，wrapper 设置 Qt 插件路径、Tcl/Tk 环境变量、
site-packages sys.path 等，避免 GUI 应用因环境变量缺失启动失败。debug
模式不可用时回退直跑 loader exe（Linux 下 ``.exe`` 用 wine）。
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
from fspack.doctor_bench import _save_and_compare_bench
from fspack.doctor_envs import _dir_size, _format_size
from fspack.doctor_models import TemplateBuildResult, TemplateRunResult

if TYPE_CHECKING:
    from fspack.templates.registry import Template

__all__ = [
    "_build_debug_cmd",
    "_build_run_cmd",
    "_build_single_template",
    "_find_debug_python",
    "_find_dist_exe",
    "_find_wrapper",
    "_format_run_status",
    "_print_performance_analysis",
    "_print_run_summary",
    "_print_template_build_summary",
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


def _find_debug_python(proj_dir: Path) -> Path | None:
    """查找 debug 模式用的 embed python 路径.

    Windows 用 ``dist/runtime/python.exe``，Linux/macOS 用
    ``dist/runtime/python/bin/python3.X``（standalone python）。
    """
    from fspack.platform import Platform, detect_platform

    dist = proj_dir / "dist"
    if detect_platform() is Platform.WINDOWS:
        py = dist / "runtime" / "python.exe"
        return py if py.is_file() else None
    bin_dir = dist / "runtime" / "python" / "bin"
    pys = sorted(bin_dir.glob("python3.*"))
    return pys[0] if pys else None


def _find_wrapper(proj_dir: Path, name: str) -> Path | None:
    """查找入口包装器 ``dist/_entry_<name>.py`` 路径."""
    wrapper = proj_dir / "dist" / f"_entry_{name}.py"
    return wrapper if wrapper.is_file() else None


def _build_debug_cmd(proj_dir: Path, name: str) -> tuple[list[str], dict[str, str]] | None:
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
    :return: ``(cmd, env)`` 或 ``None``（wrapper/embed python 缺失，调用方回退直跑 exe）
    """
    from fspack.platform import Platform, detect_platform

    py = _find_debug_python(proj_dir)
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
    shutil.copytree(template.dir, proj_dir, dirs_exist_ok=True)

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
    entry_count = len(list(dist_dir.glob("*.exe"))) if dist_dir.is_dir() else 0

    # 构建成功后解析项目入口：多入口项目产出的 exe 名是 [tool.fspack.entries]
    # 的键（如 cli/gui/web），不等于 template.name。用 ProjectInfo.default_entry
    # 取默认入口名（GUI 优先、同类型按字母排序，与 `fsp r` 默认行为一致），
    # 避免多入口项目跳过运行验证。
    entry_name = ProjectInfo.from_dir(proj_dir).default_entry.name

    # 运行验证：优先用 debug 模式（embed python + wrapper），模拟 `fsp r --debug`：
    # console 子系统 stdout 可见，wrapper 设置 Qt 插件路径、Tcl/Tk 环境变量、
    # site-packages sys.path 等，避免 GUI 应用因环境变量缺失启动失败。
    # debug 模式不可用（wrapper/embed python 缺失）时回退直跑 loader exe。
    debug = _build_debug_cmd(proj_dir, entry_name)
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


def _print_template_build_summary(results: list[TemplateBuildResult], *, bench: bool) -> None:
    """打印模板构建汇总表格与性能分析.

    :param results: 所有模板构建结果
    :param bench: ``True`` 时额外输出性能分析（耗时排名、产物大小排名）
    """
    from rich.table import Table

    console.rich.print()
    table = Table(title="模板构建汇总", show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("模板", style="cyan")
    table.add_column("构建", justify="center")
    table.add_column("运行", justify="center")
    table.add_column("耗时", justify="right")
    table.add_column("产物大小", justify="right")
    table.add_column("入口数", justify="right")
    if bench:
        table.add_column("启动耗时", justify="right")
        table.add_column("错误信息", style="dim")

    succeeded = 0
    total_time = 0.0
    total_size = 0
    run_ok = 0
    run_fail = 0
    run_skip = 0
    for i, r in enumerate(results, 1):
        build_str = "[green]√ 成功[/green]" if r.success else "[red]× 失败[/red]"
        if r.success:
            succeeded += 1
            total_time += r.duration_sec
            total_size += r.dist_size
        run_str, err_msg = _format_run_status(r)
        if r.success:
            if r.run_result is None:
                run_skip += 1
            elif r.run_result.success:
                run_ok += 1
            else:
                run_fail += 1
        # bench 模式错误信息：构建失败显示构建错误，构建成功但运行失败显示运行错误
        error_msg = r.error if not r.success else err_msg
        row = [
            str(i),
            r.template_id,
            build_str,
            run_str,
            f"{r.duration_sec:.1f}s",
            _format_size(r.dist_size) if r.dist_size else "-",
            str(r.entry_count) if r.entry_count else "-",
        ]
        if bench:
            # 启动耗时：应用调用响应速度（run_result.duration_sec）
            run_dur = f"{r.run_result.duration_sec:.2f}s" if r.run_result else "-"
            row.append(run_dur)
            row.append(error_msg)
        table.add_row(*row)

    console.rich.print(table)
    console.rich.print()

    # 汇总统计
    failed = len(results) - succeeded
    if failed:
        console.warn(f"构建完成：{succeeded} 成功 / {failed} 失败 / {len(results)} 总计")
    else:
        console.success(f"构建完成：{succeeded}/{len(results)} 全部成功")

    _print_run_summary(succeeded, run_ok, run_fail, run_skip)

    if succeeded > 0:
        avg_time = total_time / succeeded
        console.rich.print(f"  总耗时 {total_time:.1f}s | 平均 {avg_time:.1f}s | 总产物 {_format_size(total_size)}")

    # 性能分析（仅 --bench 模式）
    if bench and succeeded > 1:
        _print_performance_analysis(results)


def _print_run_summary(succeeded: int, run_ok: int, run_fail: int, run_skip: int) -> None:
    """打印运行验证汇总：构建成功的模板参与验证，区分成功/失败/跳过.

    :param succeeded: 构建成功的模板数
    :param run_ok: 运行验证成功数（含超时视为 GUI 事件循环正常）
    :param run_fail: 运行验证失败数（退出码非 0 或启动失败）
    :param run_skip: 跳过运行验证数（未找到可执行文件）
    """
    run_total = run_ok + run_fail
    if run_total == 0:
        if succeeded > 0 and run_skip > 0:
            console.warn(f"运行验证：{run_skip} 个模板跳过（未找到可执行文件）")
        return
    if run_fail > 0:
        console.warn(f"运行验证：{run_ok} 成功 / {run_fail} 失败 / {run_total} 验证 / {run_skip} 跳过")
    else:
        console.success(f"运行验证：{run_ok}/{run_total} 全部通过（{run_skip} 跳过）")


def _format_run_status(result: TemplateBuildResult) -> tuple[str, str]:
    """格式化运行验证状态为 rich 标记字符串.

    :param result: 模板构建结果
    :return: (运行状态字符串, 错误信息)。运行状态字符串含 rich 标记：
        - 构建失败 → ``"-"``（不运行验证）
        - ``run_result`` 为 ``None`` → ``"[dim]跳过[/]"``（未找到 exe）
        - 成功且超时 → ``"[green]√ 超时[/]"``（GUI/Web 事件循环正常）
        - 成功未超时 → ``"[green]√ 成功[/]"``（CLI 正常退出码 0）
        - 失败 → ``"[red]× 失败[/]"``
    """
    if not result.success:
        return ("-", "")
    rr = result.run_result
    if rr is None:
        return ("[dim]跳过[/]", "")
    if rr.success:
        if rr.timed_out:
            return ("[green]√ 超时[/]", "")
        return ("[green]√ 成功[/]", "")
    return ("[red]× 失败[/]", rr.error)


def _print_performance_analysis(results: list[TemplateBuildResult]) -> None:
    """打印性能分析：构建耗时排名、启动耗时排名、产物大小排名、瓶颈识别.

    :param results: 所有模板构建结果（仅成功的参与排名）
    """
    from rich.table import Table

    ok_results = [r for r in results if r.success]
    if len(ok_results) < 2:
        return

    console.rich.print()
    console.step("性能分析")

    # 构建耗时排名
    by_time = sorted(ok_results, key=lambda r: r.duration_sec, reverse=True)
    time_table = Table(title="构建耗时排名（降序）", show_lines=False)
    time_table.add_column("#", justify="right", style="dim")
    time_table.add_column("模板", style="cyan")
    time_table.add_column("耗时", justify="right")
    time_table.add_column("占比", justify="right")
    total = sum(r.duration_sec for r in ok_results)
    for i, r in enumerate(by_time, 1):
        ratio = r.duration_sec / total * 100 if total > 0 else 0
        marker = " [red](最慢)[/red]" if i == 1 else ""
        time_table.add_row(str(i), r.template_id, f"{r.duration_sec:.1f}s", f"{ratio:.1f}%{marker}")
    console.rich.print(time_table)

    # 启动耗时排名（应用调用响应速度）
    # 用 (build_result, run_result) 元组解构收窄类型：列表推导过滤 None 后，
    # pyrefly 仍认为 r.run_result 可能为 None，元组解构让 run_result 成为
    # 非 None 的 TemplateRunResult，消除 union-attr 抑制。
    run_pairs: list[tuple[TemplateBuildResult, TemplateRunResult]] = [
        (r, rr) for r in ok_results if (rr := r.run_result) is not None and rr.duration_sec > 0
    ]
    if len(run_pairs) >= 2:
        by_run = sorted(run_pairs, key=lambda pair: pair[1].duration_sec, reverse=True)
        run_table = Table(title="启动耗时排名（降序，应用调用响应速度）", show_lines=False)
        run_table.add_column("#", justify="right", style="dim")
        run_table.add_column("模板", style="cyan")
        run_table.add_column("启动耗时", justify="right")
        run_table.add_column("占比", justify="right")
        total_run = sum(rr.duration_sec for _, rr in run_pairs)
        for i, (r, rr) in enumerate(by_run, 1):
            dur = rr.duration_sec
            ratio = dur / total_run * 100 if total_run > 0 else 0
            marker = " [red](最慢)[/red]" if i == 1 else ""
            run_table.add_row(str(i), r.template_id, f"{dur:.2f}s", f"{ratio:.1f}%{marker}")
        console.rich.print(run_table)

    # 产物大小排名
    by_size = sorted(ok_results, key=lambda r: r.dist_size, reverse=True)
    size_table = Table(title="产物大小排名（降序）", show_lines=False)
    size_table.add_column("#", justify="right", style="dim")
    size_table.add_column("模板", style="cyan")
    size_table.add_column("大小", justify="right")
    for i, r in enumerate(by_size, 1):
        marker = " [red](最大)[/red]" if i == 1 else ""
        size_table.add_row(str(i), r.template_id, _format_size(r.dist_size) + marker)
    console.rich.print(size_table)

    # 瓶颈识别
    slowest = by_time[0]
    fastest = by_time[-1]
    if fastest.duration_sec > 0:
        ratio = slowest.duration_sec / fastest.duration_sec
        console.rich.print(
            f"\n最慢模板 [cyan]{slowest.template_id}[/cyan] ({slowest.duration_sec:.1f}s) "
            f"是最快 [cyan]{fastest.template_id}[/cyan] ({fastest.duration_sec:.1f}s) 的 {ratio:.1f} 倍"
        )
