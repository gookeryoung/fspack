"""doctor 子命令：环境诊断、模板构建测试与基准对比."""

from __future__ import annotations

import argparse
import sys

__all__ = ["_run_doctor"]


def _run_doctor(ns: argparse.Namespace) -> None:
    """执行 doctor 子命令：环境诊断 + 可选的模板构建测试/基准剖析/缓存检查.

    无 ``--test``/``--check-cache`` 时仅执行环境诊断，输出三色诊断报告。
    ``--test`` 运行所有模板构建并打印汇总结果。``-P``/``-PO``/``-PC``
    （需 ``--test``）与 ``fsp b -P/-PC``、``fsp r -P/-PC`` 对齐：启用各
    模板构建阶段耗时报告，聚合落盘单个基准剖析日志并与历史对比。
    ``--check-cache`` 扫描 wheel 缓存目录的依赖解析缓存文件并删除损坏
    文件（iter-128，可与 ``--test`` 组合使用）。
    """
    from pathlib import Path

    from fspack.doctor import (
        print_doctor_report,
        run_doctor,
        run_doctor_cache_check,
        run_doctor_test,
    )
    from fspack.exceptions import ProjectError
    from fspack.packaging.profile_log import ProfileOptions

    test = getattr(ns, "test", False)
    profile = getattr(ns, "profile", False)
    profile_out = getattr(ns, "profile_out", None)
    profile_compare = getattr(ns, "profile_compare", None)
    # 参数校验前置：诊断报告需数秒，先报参数错误再跑诊断
    if (profile or profile_out or profile_compare) and not test:
        raise ProjectError("--profile/--profile-out/--profile-compare 需配合 --test 使用")
    if (profile_out or profile_compare) and not profile:
        raise ProjectError("--profile-out/--profile-compare 需配合 --profile 使用")

    # --bench-compare 独立路径：CI 门禁场景不需要环境诊断，跳过直接运行对比
    if getattr(ns, "bench_compare", False) and not test and not getattr(ns, "check_cache", False):
        from fspack.doctor.benchmark_compare import (
            DEFAULT_CATEGORIES,
            DEFAULT_THRESHOLD,
            compare_entry,
        )

        bench_dir = Path(ns.bench_dir).resolve() if getattr(ns, "bench_dir", None) else None
        bench_threshold = getattr(ns, "bench_threshold", None) or DEFAULT_THRESHOLD
        bench_no_cats = getattr(ns, "bench_no_categories", False)
        bench_list = getattr(ns, "bench_list_categories", False)

        categories = None if bench_no_cats else DEFAULT_CATEGORIES

        rc = compare_entry(
            bench_dir=bench_dir,
            threshold=bench_threshold,
            categories=categories,
            list_categories=bench_list,
        )
        sys.exit(rc)

    report = run_doctor()
    print_doctor_report(report)

    if getattr(ns, "check_cache", False):
        run_doctor_cache_check()

    if test:
        run_doctor_test(
            ProfileOptions(
                enabled=profile,
                out=Path(profile_out).resolve() if profile_out else None,
                compare=profile_compare,
            )
        )
