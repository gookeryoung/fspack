fspack
======

极速 Python 项目打包器。

.. toctree::
   :maxdepth: 2
   :caption: 目录

   api
   changelog
   integration

简介
====

fspack 将 Python 项目打包为可执行文件与跨平台安装包：用 embed python（Windows）
或 python-build-standalone（Linux）提供运行时，C loader 配置环境并调用用户脚本，
NSIS 生成 Windows 安装包、dpkg-deb 生成 Linux .deb 与 tar.gz 便携包。命令风格参考
cargo，常用操作均可用两字母短命令完成。

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

   # 打包当前项目（生成 dist/<name>.exe 与 dist/runtime/）
   fsp b

   # 运行已打包项目
   fsp r

   # 生成安装包到 dist/release/（Windows: <name>-setup.exe / Linux: .deb + tar.gz）
   fsp p

   # 清理 dist/
   fsp c

也可指定项目目录与选项：

.. code-block:: bash

   fsp b /path/to/project --mirror aliyun --py-version 3.11.9 --target windows

完整命令参考与工作原理见 `README <https://github.com/gookeryoung/fspack#readme>`_。

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
