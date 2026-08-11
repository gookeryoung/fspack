"""doctor / cache 子命令参数声明."""

from __future__ import annotations

import argparse


def _add_doctor_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 doctor 子命令：环境诊断 + 模板构建测试 + 性能基准 + 缓存完整性检查.

    无参数时仅执行环境诊断（检查工具可用性与配置）。``--test`` 运行
    ``assets/templates/`` 下所有项目模板的构建，打印汇总结果。``--bench``
    在 ``--test`` 基础上收集性能数据（各阶段耗时、下载量、缓存命中），
    输出性能分析报告，作为后续优化的基准。``--check-cache`` 扫描 wheel
    缓存目录的依赖解析缓存文件，删除损坏文件（iter-128）。
    """
    p = sub.add_parser("doctor", aliases=["d"], help="环境诊断：检查打包工具可用性与配置")
    p.add_argument(
        "--test",
        action="store_true",
        help="运行 assets/templates/ 下所有项目模板构建，打印汇总结果",
    )
    p.add_argument(
        "--bench",
        action="store_true",
        help="运行所有模板构建并收集性能数据，输出性能分析报告（基准评估）",
    )
    p.add_argument(
        "--check-cache",
        action="store_true",
        help="扫描 wheel 缓存目录的依赖解析缓存文件，删除损坏文件，报告 stale/orphan",
    )


def _add_cache_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 cache 子命令：wheel 缓存健康检查与清理（iter-139）.

    ``fsp cache status`` 扫描 ``~/.fspack/cache/wheels`` 下的 ``.deps-*.json``
    依赖解析缓存与 ``*.whl`` wheel 文件，报告：

    - 损坏 deps（JSON 结构非法，扫描时已自动删除）
    - stale deps（引用了缺失 wheel 的 deps 文件，需 ``fsp cache clean`` 清理）
    - 孤儿 wheel（未被任何 deps 引用的 wheel 文件，需 ``fsp cache clean`` 清理）

    ``fsp cache clean`` 删除 stale deps 与孤儿 wheel，``--dry-run`` 仅预览不删除。
    """
    p = sub.add_parser("cache", help="wheel 缓存健康检查与清理")
    cache_sub = p.add_subparsers(dest="cache_action", metavar="<action>", required=True)
    cache_sub.add_parser("status", help="扫描缓存目录健康状态（损坏/stale/orphan）")
    clean_p = cache_sub.add_parser("clean", help="清理 stale deps 与孤儿 wheel 文件")
    clean_p.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览将删除的文件，不实际删除",
    )


__all__ = ["_add_cache_subparser", "_add_doctor_subparser"]
