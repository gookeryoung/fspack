"""init 子命令：从模板创建新项目."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

__all__ = ["_run_init"]


def _run_init(ns: argparse.Namespace) -> None:
    """执行 init/i 子命令：从模板创建新项目.

    分发逻辑：

    - ``--list`` → 打印模板列表后退出
    - ``--template`` 显式指定 → 用指定模板
    - ``--template`` 未指定 → 调 :func:`prompt_template_selection` 交互式选择
      （非 TTY 环境自动回退到 helloworld）
    """
    from fspack.templates.init import init_project, print_template_list, prompt_template_selection

    if ns.list:
        print_template_list()
        return

    if not ns.project_name:
        # 未指定项目名且未 --list：用当前目录名作为项目名
        import logging

        ns.project_name = Path.cwd().name
        logging.getLogger(__name__).info("未指定项目名，使用当前目录名: %s", ns.project_name)

    template_id = ns.template
    if template_id is None:
        # 未指定 --template：交互式选择（非 TTY 自动回退 helloworld）
        try:
            template_id = prompt_template_selection()
        except KeyboardInterrupt:
            from fspack.ui.console import console

            console.rich.print("\n[yellow]已取消[/]")
            sys.exit(1)

    directory = Path(ns.directory).resolve() if ns.directory else None
    try:
        init_project(
            ns.project_name,
            template_id=template_id,
            directory=directory,
            description=ns.description,
            python_version=ns.python_version,
        )
    except ValueError as exc:
        from fspack.ui.console import console

        console.error(str(exc))
        sys.exit(1)
