fspack
======

把 Python 项目变成可执行文件与安装包 —— 一行命令搞定。

.. image:: https://img.shields.io/pypi/v/fspack
   :target: https://pypi.org/project/fspack/
.. image:: https://github.com/gookeryoung/fspack/actions/workflows/ci.yml/badge.svg
   :target: https://github.com/gookeryoung/fspack/actions/workflows/ci.yml
.. image:: https://img.shields.io/badge/python-3.8%2B-blue.svg
.. image:: https://img.shields.io/badge/license-MIT-green.svg
.. image:: https://img.shields.io/badge/coverage-%E2%89%A595%25-brightgreen.svg

``fsp b`` 一行命令产出 ``.exe``，``fsp p`` 再一行产出 Windows 安装包或 Linux
``.deb``。无需改源码：自动 AST 扫描 import 推断依赖、按需精简 wheel、预编译
字节码加速启动。

.. toctree::
   :maxdepth: 2
   :caption: 指南

   integration
   performance

.. toctree::
   :maxdepth: 2
   :caption: 参考

   architecture
   api
   changelog

30 秒上手
=========

.. code-block:: bash

   pip install fspack
   cd your-project          # 含 pyproject.toml 的 Python 项目
   fsp b                    # 产出 dist/your-app.exe
   fsp p                    # 产出 dist/release/your-app-setup.exe

安装
====

.. code-block:: bash

   pip install fspack

或使用 uv_:

.. code-block:: bash

   uv add fspack

.. _uv: https://docs.astral.sh/uv/

快速上手
========

在 Python 项目根目录（含 ``pyproject.toml``）执行：

.. code-block:: bash

   fsp b    # 打包：生成 dist/<name>.exe 与 dist/runtime/
   fsp r    # 运行验证
   fsp p    # 生成安装包：产出 dist/release/<name>-setup.exe
   fsp c    # 清理：删除 dist/

也可指定项目目录与选项：

.. code-block:: bash

   fsp b /path/to/project --mirror aliyun --py-version 3.11.9 --target windows

完整命令参考、配置项与示例见 `README <https://github.com/gookeryoung/fspack#readme>`_。

开发
====

.. code-block:: bash

   uv sync --extra dev                                          # 安装开发依赖
   uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95 # 测试
   uv run pyrefly check                                         # 类型检查
   uv run ruff check src tests                                  # lint

``make help`` 列出全部快捷命令；多版本测试用 ``make tox``。架构与工作原理见
:doc:`architecture`。
