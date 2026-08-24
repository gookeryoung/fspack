"""Nuitka 编译器 facade：将用户源码 ``.py`` 编译为 ``.pyd`` 本机执行.

本包是 facade，通过 :mod:`fspack.packaging.nuitka.compiler` 的 :class:`NuitkaCompiler`
多继承组合八个职责单一的 mixin：

- :class:`fspack.packaging.nuitka.env.NuitkaEnv` — 环境就绪
  （C 编译器检查、nuitka 安装、pip 可用性、构建机编译环境变量）
- :class:`fspack.packaging.nuitka.standalone.NuitkaStandalone` — standalone python 准备
  （Windows python-build-standalone 下载与缓存）
- :class:`fspack.packaging.nuitka.winlibs.NuitkaWinlibs` — winlibs-mingw 工具链管理
  （Windows 预填充 Nuitka 所需 winlibs gcc 到 ``nuitka-winlibs-mingw`` 缓存目录，
  缓存未命中时下载解压）
- :class:`fspack.packaging.nuitka.ccache.NuitkaCcache` — ccache 管理
  （PATH 查找、本地缓存、预编译二进制下载）
- :class:`fspack.packaging.nuitka.progress.NuitkaProgress` — 并行编译调度
  （线程池并行 ``--mode=module`` 批量编译 .py、全局心跳进度反馈）
- :class:`fspack.packaging.nuitka.compile.NuitkaCompile` — 编译流程
  （单文件 ``--mode=module`` 编译、stamp 缓存、第三方包编译）
- :class:`fspack.packaging.nuitka.strip.NuitkaStrip` — 产物剥离与构建目录清理
  （验证 .pyd 可加载后删 .py、清理 ``.build/`` 残留）
- :class:`fspack.packaging.nuitka.verify.NuitkaVerify` — 编译产物验证
  （.pyd/.so 可加载性测试、批量/单模块 import 验证）

所有方法为 staticmethod/classmethod，无实例状态。``cls.`` 调用经 MRO 自动派发
到对应 mixin，对外暴露统一的 :class:`NuitkaCompiler` API。

参考 RimSort 的 Nuitka 打包方案，用 ``python -m nuitka --mode=module`` 将每个 ``.py``
编译为对应平台的 ``.pyd``（Windows）/ ``.so``（Linux）。运行时 ``.pyd`` 优先级
高于 ``.pyc``，Python 自动加载本机代码版本，执行速度提升 30-50%。

与 RimSort 区别：fspack 仅编译用户源码（``dist/src/``），第三方依赖保持 wheel
解压 + ``.pyc``（构建速度优先）。RimSort 用 Nuitka ``--follow-imports`` 全量编译，
构建耗时几十分钟；fspack 用户源码通常较小，编译时间可控。

公共 API：

- :meth:`NuitkaCompiler.ensure_env`：检查 C 编译器并按目标 Python 版本安装锁定版 nuitka 到本地缓存
- :meth:`NuitkaCompiler.compile_src`：编译 ``dist/src`` 下所有 ``.py`` 为本机模块
- :meth:`NuitkaCompiler.compile_with_stamp`：整合 ensure_env + stamp 缓存 + compile_src 的入口

stamp 缓存（:meth:`NuitkaCompiler.compile_with_stamp`）：重复构建时若
``dist/.nuitka_compile_stamp``（含 ``nuitka_version|py_version|src_fingerprint|entry_rels``）
匹配则跳过整个 Nuitka 阶段（含 ensure_env 与 compile_src），避免重复 subprocess
启动开销与编译耗时。入口文件（``entry_rels``）不编译不删除，保留 ``.py`` 供
入口包装器 ``runpy.run_path()`` 调用。
"""

from __future__ import annotations

# 为兼容测试中 monkeypatch.setattr("fspack.packaging.nuitka.<module>.<attr>", ...) 路径解析，
# facade 显式 import 这些模块（patch 设置的是模块对象的属性，全局生效，对 env/standalone/
# winlibs/ccache/compile/strip/verify 等 mixin 模块同样有效）。
# 详见 rule-01 公开 API 不变约束。
import shutil  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401

from fspack.packaging.nuitka.compiler import NuitkaCompiler

__all__ = ["NuitkaCompiler"]
