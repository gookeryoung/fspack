fspack
======

One-command Python packaging — build executables and installers.

.. note::

   This documentation is being progressively translated to English.
   Some reference guides (CLI, Configuration, etc.) are currently in Chinese
   and will be translated in upcoming releases.

.. image:: https://img.shields.io/pypi/v/fspack
   :target: https://pypi.org/project/fspack/
.. image:: https://github.com/gookeryoung/fspack/actions/workflows/ci.yml/badge.svg
   :target: https://github.com/gookeryoung/fspack/actions/workflows/ci.yml
.. image:: https://img.shields.io/badge/python-3.10%2B-blue.svg
.. image:: https://img.shields.io/badge/license-MIT-green.svg
.. image:: https://img.shields.io/badge/coverage-%E2%89%A595%25-brightgreen.svg

``fsp b`` produces ``.exe`` with one command, ``fsp p`` produces a Windows
installer or Linux ``.deb`` with another. No source code changes needed:
automatic AST ``import`` analysis infers dependencies, smart wheel slimming,
pre-compiled bytecode for fast startup.

.. toctree::
   :maxdepth: 2
   :caption: Guides

   integration
   offline
   distribution
   performance

.. toctree::
   :maxdepth: 2
   :caption: Reference

   cli
   configuration
   architecture
   api
   changelog

30-Second Quick Start
=====================

.. code-block:: bash

   pip install fspack
   cd your-project          # Python project with pyproject.toml
   fsp b                    # Produces dist/your-app.exe
   fsp p                    # Produces dist/release/your-app-setup.exe

Installation
============

.. code-block:: bash

   pip install fspack

Or with uv_:

.. code-block:: bash

   uv add fspack

.. _uv: https://docs.astral.sh/uv/

Usage
=====

From your Python project root (with ``pyproject.toml``):

.. code-block:: bash

   fsp b    # Build: produces dist/<name>.exe and dist/runtime/
   fsp r    # Run and verify
   fsp p    # Create installer: produces dist/release/<name>-setup.exe
   fsp c    # Clean up dist/

Specify project directory and options:

.. code-block:: bash

   fsp b /path/to/project --mirror aliyun --py-version 3.11.9 --target windows

Full command reference: :doc:`cli`. Configuration options: :doc:`configuration`.

Development
===========

.. code-block:: bash

   uv sync --extra dev                                          # Install dev dependencies
   uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95 # Tests
   uv run pyrefly check                                         # Type check
   uv run ruff check src tests                                  # Lint

Run ``make help`` for all shortcuts; multi-version tests with ``make tox``.
Architecture and implementation details: :doc:`architecture`.
