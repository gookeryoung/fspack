"""cache 子命令：缓存健康检查与清理."""

from __future__ import annotations

import argparse

__all__ = ["_run_cache"]


def _run_cache(ns: argparse.Namespace) -> None:
    """执行 cache 子命令：缓存健康检查与清理.

    iter-139 引入：仅扫描 wheels。
    iter-148 扩展：默认扫描全部 cache 类型（wheels/embed/standalone/nuitka/loaders/
    ccache/tkinter），``--target <name>`` 限定单类型，``--stale`` 启用过期文件清理。

    ``fsp cache status [--target <name>]`` 扫描缓存目录健康状态并渲染详细报告。
    ``fsp cache clean [--dry-run] [--stale] [--target <name>]`` 清理损坏文件与
    wheels 的 stale/orphan，``--stale`` 额外清理非 wheels 类型的过期文件。
    """
    from fspack.doctor import run_cache_clean, run_cache_status

    action = getattr(ns, "cache_action", None)
    target = getattr(ns, "target", None)
    if action == "status":
        run_cache_status(target=target, full_verify=getattr(ns, "verify", False))
    elif action == "clean":
        run_cache_clean(
            dry_run=getattr(ns, "dry_run", False),
            include_stale=getattr(ns, "stale", False),
            target=target,
        )
    # argparse 已用 required=True 保证 cache_action 必填，到这里 action 必非 None
