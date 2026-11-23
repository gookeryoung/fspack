"""递归 build/package：子项目发现、逐项目执行与汇总."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

__all__ = ["discover_subprojects"]

# 递归扫描子项目时跳过的目录名。
# 与 analyzer._EXCLUDED_DIRS 语义不同：本集合用于"找 pyproject.toml"（不进入
# .venv/dist/build 等），_EXCLUDED_DIRS 用于"扫描 .py/.qml 源码"（额外排除
# examples/tests/docs/templates 等开发期目录）。两者各自独立维护，不强行合并
# 以避免语义混淆。
_RECURSIVE_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "dist",
        "build",
        ".git",
        "__pycache__",
        ".venv",
        ".tox",
        ".fspack",
        ".trae",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".pyrefly_cache",
        ".uv-cache",
        "htmlcov",
        "node_modules",
    }
)


def discover_subprojects(root: Path) -> list[Path]:
    """递归扫描 root 目录下所有含 ``pyproject.toml`` 的子项目（含 root 自身）.

    跳过 :data:`_RECURSIVE_SKIP_DIRS` 中的开发期目录（如 ``.venv``/``dist``/
    ``build``/``.git``），避免误识别这些目录下的 ``pyproject.toml``（如
    ``.venv`` 内 pip 的 ``pyproject.toml``）。

    返回按路径字母序排序的子项目目录列表（含 root 自身，若 root 含
    ``pyproject.toml``），便于稳定输出与可重复构建。

    用 :func:`os.scandir` 递归遍历，``DirEntry`` 复用枚举时的 stat 缓存
    减少 stat 调用。子目录递归前先检查目录名是否在跳过集合中。
    """
    projects: list[Path] = []
    seen: set[Path] = set()

    def _scan(current: Path) -> None:
        # 多次调用同一目录去重（理论不会，但兜底防御符号链接循环）
        try:
            real = current.resolve()
        except OSError:
            return
        if real in seen:
            return
        seen.add(real)

        if (current / "pyproject.toml").is_file():
            projects.append(current)

        try:
            # with 显式关闭 scandir 迭代器：排序 key 抛异常时不依赖 GC 回收句柄
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name in _RECURSIVE_SKIP_DIRS or entry.name.endswith(".egg-info"):
                continue
            _scan(Path(entry.path))

    _scan(root)
    return projects


def _run_recursive(root: Path, action: str, ns: argparse.Namespace) -> int:
    """递归执行 build/package 子命令，返回退出码（0=全部成功，1=有失败）.

    扫描 root 下所有子项目，依次调用 ``_run_build`` 或 ``_run_package``。
    单项目失败时打印错误并继续，最后汇总成功/失败列表。

    输出格式示例::

        > 递归扫描子项目：发现 3 个
        > [1/3] 构建 app1 ...
        √ app1 构建成功
        > [2/3] 构建 app2 ...
        × app2 构建失败: <错误>
        > [3/3] 构建 app3 ...
        √ app3 构建成功

        递归构建汇总：成功 2，失败 1
        失败项目：
          - app2: <错误摘要>

    退出码：全部成功返回 0，任一失败返回 1（便于 CI 检测）。
    """
    # 从包级命名空间取执行函数：既保持 ``fspack.cli._run_build`` 的 monkeypatch
    # 兼容，也避免递归模块在 import 期拉起 builder/installer。
    from fspack.cli import _run_build, _run_package
    from fspack.ui.console import console

    projects = discover_subprojects(root)
    total = len(projects)
    if total == 0:
        console.warn(f"未在 {root} 下发现含 pyproject.toml 的子项目")
        return 0

    console.step(f"递归扫描子项目：发现 {total} 个")
    succeeded: list[tuple[Path, str]] = []  # (project, summary)
    failed: list[tuple[Path, str]] = []  # (project, error_message)

    action_verb = "构建" if action == "build" else "打包"

    for index, project in enumerate(projects, 1):
        rel = _format_project_path(project, root)
        console.step(f"[{index}/{total}] {action_verb} {rel} ...")
        try:
            if action == "build":
                _run_build(project, ns)
            else:
                _run_package(project, ns)
            succeeded.append((project, rel))
            console.success(f"{rel} {action_verb}成功")
        except SystemExit:
            # _run_build/_run_package 不主动调 sys.exit，但防御性捕获
            raise
        except KeyboardInterrupt:
            # Ctrl+C 必须立即中断整个递归构建，不得记为单项目失败后继续循环
            raise
        except BaseException as exc:
            err_msg = _format_error(exc)
            failed.append((project, f"{rel}: {err_msg}"))
            console.error(f"{rel} {action_verb}失败: {err_msg}")
            import logging

            logging.getLogger("fspack.cli").debug("%s 完整错误堆栈", rel, exc_info=exc)

    _print_recursive_summary(action_verb, succeeded, failed)
    return 1 if failed else 0


def _format_project_path(project: Path, root: Path) -> str:
    """格式化子项目路径用于显示：相对 root 的路径，root 自身显示为 '.'."""
    try:
        rel = project.relative_to(root)
    except ValueError:
        return str(project)
    return "." if str(rel) == "." else rel.as_posix()


def _format_error(exc: BaseException) -> str:
    """格式化异常为单行错误消息（去除换行，截断超长消息）."""
    msg = str(exc).strip() or exc.__class__.__name__
    # 取首行，避免多行错误消息破坏汇总表
    first_line = msg.splitlines()[0]
    if len(first_line) > 200:
        first_line = first_line[:197] + "..."
    return first_line


def _print_recursive_summary(
    action_verb: str, succeeded: list[tuple[Path, str]], failed: list[tuple[Path, str]]
) -> None:
    """打印递归执行汇总：成功数、失败数、失败项目列表."""
    from fspack.ui.console import console

    console.rich.print()  # 空行分隔
    console.step(f"递归{action_verb}汇总：成功 {len(succeeded)}，失败 {len(failed)}")
    if failed:
        console.error("失败项目：")
        for _project, msg in failed:
            console.rich.print(f"  - {msg}")
    elif succeeded:
        console.success(f"全部 {len(succeeded)} 个子项目{action_verb}成功")
