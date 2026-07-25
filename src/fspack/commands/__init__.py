"""fspack 子命令实现：clean / run.

build 与 package 子命令因属薄包装层已删除，cli.py 直接调用
:func:`fspack.builder.build` 与 :func:`fspack.packaging.installer.build_release`。
"""
