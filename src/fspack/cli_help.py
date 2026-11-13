"""``fspack --help`` 的静态快照（非 TTY 快速路径用）.

``build_parser()`` 在 3.14 上约 59ms（400+ 个 action 的注册与冲突检查），是
``fsp --help`` 冷启动的最大单项。非交互场景（CI/脚本/管道）不需要按终端宽度
自适应换行，直接输出静态文本可省去 parser 构建。

**维护约定：** 修改 :mod:`fspack.cli_parser` 的任何 help 文案/参数后，运行
``python -c "from fspack.cli_parser import build_parser; print(build_parser().format_help(), end='')"``
重新生成本文件；``tests/test_cli.py::test_help_snapshot_matches_parser`` 会在
快照与 parser 输出不一致时失败（仅快照对应的 Python 版本上校验）。

argparse 的 section 标题与换行在不同 Python 次版本间存在差异（如 3.9 的
``optional arguments:``、3.10+ 的 ``options:``），因此快照绑定生成时的次版本
:data:`HELP_SNAPSHOT_PY`；其它版本一律回退 ``build_parser()``，正确性优先。
"""

from __future__ import annotations

# 生成 HELP_TEXT 的 Python 次版本；与此不符的解释器不启用快照路径
HELP_SNAPSHOT_PY: tuple[int, int] = (3, 14)

HELP_TEXT = """usage: fspack [-h] [-V] [-v] [-q] [--json] <command> ...

极速 Python 打包器（cargo 风格短命令）。

positional arguments:
  <command>
    build (b)     打包项目
    run (r)       运行已打包项目
    clean (c)     清理 dist/
    package (p)   生成发行包
    init (i)      从模板创建新项目
    manifest (m)  产物清单生成与差异对比
    doctor (d)    环境诊断：检查打包工具可用性与配置
    cache         缓存健康检查与清理
    verify        校验产物二进制格式与目标架构（PE/ELF/Mach-O）

options:
  -h, --help      show this help message and exit
  -V, --version   show program's version number and exit
  -v, --verbose   显示 DEBUG 级别日志（含外部工具原始输出）
  -q, --quiet     静默模式：只输出错误与最终结果
  --json          在 stdout 输出单行 JSON 信封：build/package/clean/init/doctor/verify
                  有，run/manifest/cache 不输出（自带主输出），人读输出去 stderr
"""
