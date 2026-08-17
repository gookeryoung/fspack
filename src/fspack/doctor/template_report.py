"""模板构建测试报告渲染.

从 :mod:`fspack.doctor.templates` 拆出的渲染层：汇总表格（构建/运行状态/
耗时/产物大小）、运行验证汇总、性能分析排名（构建耗时/启动耗时/产物大小/
瓶颈识别），供 :func:`fspack.doctor.templates.run_doctor_test`/
:func:`fspack.doctor.templates.run_doctor_bench` 在构建完成后调用。
"""

from __future__ import annotations

from fspack.console import console
from fspack.doctor.envs import _format_size
from fspack.doctor.models import TemplateBuildResult, TemplateRunResult

__all__ = [
    "_format_run_status",
    "_print_performance_analysis",
    "_print_run_summary",
    "_print_template_build_summary",
]


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
