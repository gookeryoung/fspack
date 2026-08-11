"""dry-run 打包计划打印：Rich Table 结构化输出项目/依赖/选项信息.

从 :mod:`fspack.packaging.pipeline` 拆分。用于 ``--dry-run`` 模式在构建前
预览打包配置，不执行任何写操作。

唯一公开函数 :func:`_print_build_plan` 接收已解析的 BuildContext +
DependencyReport，输出 4 张 Rich Table：项目信息、依赖分析、私有包源、构建选项。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.table import Table

from fspack.config import DependencyReport
from fspack.console import console
from fspack.platform import Platform

if TYPE_CHECKING:
    from fspack.packaging.pipeline.context import BuildContext

__all__ = ["_print_build_plan"]


def _print_build_plan(ctx: BuildContext, report: DependencyReport) -> None:  # noqa: PLR0912
    """打印打包计划（``--dry-run`` 模式），不执行任何写操作.

    输出内容：

    - 项目基本信息：名称、版本、入口类型、入口数
    - 目标平台与 Python 版本
    - runtime 来源：Windows=embed python / Linux=python-build-standalone
    - loader 编译器：Windows=mingw-w64 / Linux=gcc
    - 缓存目录路径
    - 依赖分析：声明依赖数、AST 发现第三方数、未声明依赖（missing）数
    - 镜像源配置
    - 构建选项摘要（Nuitka/ccache/pyc_strip/no_site 等）
    """
    info = ctx.info
    target = ctx.cfg.target

    console.step("打包计划（dry-run，不执行实际构建）")

    # 基本信息
    basic_table = Table(title="项目信息", show_lines=False)
    basic_table.add_column("字段", style="cyan", no_wrap=True)
    basic_table.add_column("值", style="white")
    basic_table.add_row("项目名", info.name)
    basic_table.add_row("版本", info.version)
    basic_table.add_row("应用类型", info.app_type.value)
    entries = info.all_entries
    if len(entries) > 1:
        basic_table.add_row("入口数", f"{len(entries)} (多入口)")
        for ep in entries:
            basic_table.add_row(f"  - {ep.name}", f"{ep.module}.py ({ep.app_type.value})")
    else:
        basic_table.add_row("入口", f"{info.entry_module}.py")
    basic_table.add_row("目标平台", target.value)
    basic_table.add_row("Python 版本", info.py_version)
    runtime_source = "embed python" if target is Platform.WINDOWS else "python-build-standalone"
    basic_table.add_row("runtime 来源", runtime_source)
    if target is Platform.WINDOWS:
        loader_compiler = "mingw-w64"
    elif target is Platform.MACOS:
        loader_compiler = "clang"
    else:
        loader_compiler = "gcc"
    basic_table.add_row("loader 编译器", loader_compiler)
    basic_table.add_row("缓存目录", str(ctx.cfg.embed_cache_dir.parent))
    basic_table.add_row("镜像源", f"{ctx.cfg.mirror.name} ({ctx.cfg.mirror.pypi_index})")
    console.rich.print(basic_table)

    # 依赖分析
    console.rich.print()
    dep_table = Table(title="依赖分析", show_lines=False)
    dep_table.add_column("类别", style="cyan", no_wrap=True)
    dep_table.add_column("数量", justify="right")
    dep_table.add_column("详情")
    dep_table.add_row("声明依赖", str(len(info.dependencies)), ", ".join(info.dependencies) or "(无)")
    if ctx.opts.extras:
        dep_table.add_row(
            "启用 extras",
            str(len(ctx.opts.extras)),
            ", ".join(sorted(ctx.opts.extras)),
        )
        dep_table.add_row(
            "扩展后依赖",
            str(len(report.declared)),
            ", ".join(report.declared) or "(无)",
        )
    dep_table.add_row(
        "AST 第三方",
        str(len(report.ast_third_party)),
        ", ".join(sorted(report.ast_third_party)) or "(无)",
    )
    dep_table.add_row(
        "未声明 (missing)",
        str(len(report.missing)),
        ", ".join(report.missing) if report.missing else "(无)",
    )
    dep_table.add_row("AST 标准库", str(len(report.ast_stdlib)), "(已识别)")
    dep_table.add_row("AST 本地模块", str(len(report.ast_local)), "(已识别)")
    console.rich.print(dep_table)

    # 私有包源
    if info.extra_index_urls or info.find_links:
        console.rich.print()
        src_table = Table(title="私有包源", show_lines=False)
        src_table.add_column("类型", style="cyan", no_wrap=True)
        src_table.add_column("路径")
        for url in info.extra_index_urls:
            src_table.add_row("extra-index-url", url)
        for link in info.find_links:
            src_table.add_row("find-links", link)
        console.rich.print(src_table)

    # 构建选项
    console.rich.print()
    opts_table = Table(title="构建选项", show_lines=False)
    opts_table.add_column("选项", style="cyan", no_wrap=True)
    opts_table.add_column("值", justify="center")
    opts_table.add_row("Nuitka 编译", "启用" if ctx.opts.nuitka else "关闭")
    if ctx.opts.nuitka:
        opts_table.add_row("ccache", "启用" if ctx.opts.ccache else "关闭")
        if ctx.opts.nuitka_packages:
            opts_table.add_row("nuitka-packages", ", ".join(ctx.opts.nuitka_packages))
    opts_table.add_row("字节码预编译", "关闭" if ctx.opts.no_pyc else "启用")
    opts_table.add_row("pyc 优化级别", str(ctx.opts.pyc_optimize))
    opts_table.add_row("剥离 .py (pyc_strip)", "启用" if ctx.opts.pyc_strip else "关闭")
    opts_table.add_row("标准库精简", "关闭" if ctx.opts.no_stdlib_trim else "启用")
    opts_table.add_row("禁用 site.py", "是" if ctx.opts.no_site else "否")
    if ctx.opts.keep_modules:
        opts_table.add_row("显式保留模块", ", ".join(sorted(ctx.opts.keep_modules)))
    console.rich.print(opts_table)

    # 汇总
    console.rich.print()
    wheel_count = len(info.dependencies)
    console.success(
        f"打包计划就绪：{info.name} {info.version} → {target.value} / Python {info.py_version} / "
        f"{len(entries)} 入口 / {wheel_count} 声明依赖"
    )
    console.rich.print("[dim]以上为 dry-run 预览，未执行任何下载/编译/复制。去掉 --dry-run 执行实际构建。[/]")
