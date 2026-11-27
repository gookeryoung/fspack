"""fspack CLI 包.

对外保持旧模块路径的兼容入口：

- ``fspack.cli.main`` / ``fspack.cli.build_parser``
- 既有测试/外部脚本引用的私有辅助（``_run_build``/``_run_recursive``/
  ``_drop_separator``/``_parse_lazy_imports``/``_resolve_mirror`` 等）

``__main__`` 守卫不是可选项：``pyproject.toml`` 的 ``[project.scripts]`` 声明
``fspack.cli:main``，而 fspack 打包时忽略 ``:function`` 段、用
``runpy.run_module(<entry module>, run_name="__main__")`` 执行整个入口模块。
入口解析到包级 ``fspack/cli/__init__.py`` 时 wrapper 的模块名为
``fspack.cli.__init__``，缺少本守卫则包体只被导入、``main()`` 从不被调用，
产物 ``fsp -V``/``fsp -h`` 与所有子命令静默退出（退出码 0、无输出）。
"""

from __future__ import annotations

from fspack.cli.commands.build import _in_ci_environment as _in_ci_environment
from fspack.cli.commands.build import _parse_lazy_imports as _parse_lazy_imports
from fspack.cli.commands.build import _parse_target as _parse_target
from fspack.cli.commands.build import _resolve_build_targets as _resolve_build_targets
from fspack.cli.commands.build import _resolve_mirror as _resolve_mirror
from fspack.cli.commands.build import _run_build as _run_build
from fspack.cli.commands.cache import _run_cache as _run_cache
from fspack.cli.commands.clean import _run_clean as _run_clean
from fspack.cli.commands.doctor import _run_doctor as _run_doctor
from fspack.cli.commands.init import _run_init as _run_init
from fspack.cli.commands.manifest import _run_diff as _run_diff
from fspack.cli.commands.manifest import _run_generate as _run_generate
from fspack.cli.commands.manifest import _run_manifest as _run_manifest
from fspack.cli.commands.package import _run_package as _run_package
from fspack.cli.commands.run import _drop_separator as _drop_separator
from fspack.cli.commands.run import _run_run as _run_run
from fspack.cli.commands.verify import _run_verify as _run_verify
from fspack.cli.dispatch import _dispatch as _dispatch
from fspack.cli.main import _JSON_COMMANDS as _JSON_COMMANDS
from fspack.cli.main import _emit_json as _emit_json
from fspack.cli.main import _help_snapshot_usable as _help_snapshot_usable
from fspack.cli.main import _try_fast_path as _try_fast_path
from fspack.cli.main import main as main
from fspack.cli.parser import build_parser as build_parser
from fspack.cli.recursive import _format_error as _format_error
from fspack.cli.recursive import _format_project_path as _format_project_path
from fspack.cli.recursive import _print_recursive_summary as _print_recursive_summary
from fspack.cli.recursive import _run_recursive as _run_recursive
from fspack.cli.recursive import discover_subprojects as discover_subprojects

__all__ = [
    "build_parser",
    "discover_subprojects",
    "main",
]

if __name__ == "__main__":
    main()
