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

极速 Python 项目打包器。。

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

.. code-block:: python

   import fspack

   # TODO: 添加使用示例

开发
====

.. code-block:: bash

   # 安装开发依赖
   uv sync --extra dev

   # 运行测试
   uv run pytest

   # 类型检查
   uv run pyrefly check .

   # 代码风格
   uv run ruff check .
   uv run ruff format --check .
