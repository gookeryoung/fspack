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

fspack 让你的 Python 项目秒变可分发的桌面应用。无需改一行代码，``fsp b`` 一行命令
产出 ``.exe``，``fsp p`` 再一行产出 Windows 安装包或 Linux ``.deb``。自动分析依赖、
精简体积、预编译加速，开箱即用。

.. toctree::
   :maxdepth: 2
   :caption: 指南

   integration

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

就这样。你的 Python 项目已经变成可以分发给别人双击运行的桌面应用了。

为什么选 fspack
===============

.. list-table::
   :widths: 30 70
   :header-rows: 1

   * - 你想要的
     - fspack 给你的
   * - 一行命令打包
     - ``fsp b`` 生成可执行文件，``fsp p`` 生成安装包，cargo 风格两字母短命令
   * - 不改源码
     - 自动 AST 扫描 import 推断依赖，无需手动声明打包配置
   * - 小体积安装包
     - 自动精简 wheel、预编译 ``.pyc``、可选剥离 ``.py``
   * - 跨平台分发
     - Windows 出 ``.exe`` + NSIS 安装包，Linux 出 ``.deb`` + ``.tar.gz``，支持交叉编译
   * - 双击就能跑
     - 内置便携运行时，用户机无需装 Python；Windows 安装包含快捷方式与卸载器
   * - 首次启动快
     - 默认预编译字节码，``--nuitka`` 可本机编译提速 30-50%
   * - 多入口项目
     - 一个项目生成多个 exe（cli/gui/web），共享运行时与依赖
   * - 国内网络友好
     - 默认清华镜像，``--mirror`` 一键切换阿里/华为源

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

   # 1. 打包：生成 dist/<name>.exe 与 dist/runtime/
   fsp b

   # 2. 运行验证：直接跑打包产物
   fsp r

   # 3. 生成安装包：产出 dist/release/<name>-setup.exe
   fsp p

   # 4. 清理：删除 dist/
   fsp c

也可指定项目目录与选项：

.. code-block:: bash

   fsp b /path/to/project --mirror aliyun --py-version 3.11.9 --target windows

完整命令参考、配置项与示例见 `README <https://github.com/gookeryoung/fspack#readme>`_。

开发
====

.. code-block:: bash

   # 安装开发依赖
   uv sync --extra dev

   # 运行测试（含覆盖率，阈值 95%）
   uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95

   # 类型检查
   uv run pyrefly check

   # 代码风格
   uv run ruff check src tests
   uv run ruff format --check src tests

项目提供 ``make help`` 列出全部快捷命令；多版本测试可用 ``make tox``。
架构与工作原理见 :doc:`architecture`。
