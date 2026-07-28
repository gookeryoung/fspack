"""Nuitka 编译器 facade：将用户源码 ``.py`` 编译为 ``.pyd`` 本机执行.

本模块是 facade，通过多继承组合四个职责单一的 mixin：

- :class:`fspack.packaging.nuitka_env.NuitkaEnv` — 环境就绪
  （C 编译器检查、nuitka 安装、pip 可用性、构建机编译环境变量）
- :class:`fspack.packaging.nuitka_standalone.NuitkaStandalone` — standalone python 准备
  （Windows python-build-standalone 下载与缓存）
- :class:`fspack.packaging.nuitka_ccache.NuitkaCcache` — ccache 管理
  （PATH 查找、本地缓存、预编译二进制下载）
- :class:`fspack.packaging.nuitka_compile.NuitkaCompile` — 编译流程
  （单文件 ``--module`` 编译、stamp 缓存、第三方包编译）
- :class:`fspack.packaging.nuitka_strip.NuitkaStrip` — 产物剥离与构建目录清理
  （验证 .pyd 可加载后删 .py、清理 ``.build/`` 残留）
- :class:`fspack.packaging.nuitka_verify.NuitkaVerify` — 编译产物验证
  （.pyd/.so 可加载性测试、批量/单模块 import 验证）

所有方法为 staticmethod/classmethod，无实例状态。``cls.`` 调用经 MRO 自动派发
到对应 mixin，对外暴露统一的 :class:`NuitkaCompiler` API。

参考 RimSort 的 Nuitka 打包方案，用 ``python -m nuitka --module`` 将每个 ``.py``
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
# facade 显式 import 这些模块（patch 设置的是模块对象的属性，全局生效，对 env/standalone/ccache/
# compile/strip/verify 六个 mixin 模块同样有效）。详见 rule-01 公开 API 不变约束。
import shutil  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401

from fspack.packaging.nuitka_ccache import NuitkaCcache
from fspack.packaging.nuitka_compile import NuitkaCompile
from fspack.packaging.nuitka_env import NuitkaEnv
from fspack.packaging.nuitka_standalone import NuitkaStandalone
from fspack.packaging.nuitka_strip import NuitkaStrip
from fspack.packaging.nuitka_verify import NuitkaVerify

__all__ = ["NuitkaCompiler"]


class NuitkaCompiler(NuitkaEnv, NuitkaStandalone, NuitkaCcache, NuitkaStrip, NuitkaCompile, NuitkaVerify):
    """Nuitka 编译器 facade（多继承自 env/standalone/ccache/strip/compile/verify 六个 mixin）.

    所有方法为 staticmethod/classmethod，无实例状态。按 MRO 顺序
    ``NuitkaCompiler → NuitkaEnv → NuitkaStandalone → NuitkaCcache → NuitkaStrip →
    NuitkaCompile → NuitkaVerify → object`` 派发 ``cls.`` 调用到对应 mixin。

    **MRO 顺序设计**：``NuitkaStrip`` 必须在 ``NuitkaCompile`` 前面，否则
    ``NuitkaCompile`` 类内的 ``_strip_compiled_sources`` / ``_cleanup_build_dirs``
    stub 会覆盖 ``NuitkaStrip`` 的真实实现。同理 ``NuitkaStandalone`` /
    ``NuitkaCcache`` 必须在 ``NuitkaCompile`` 前面，避免 ``_ensure_build_python``
    / ``_ensure_ccache`` stub 覆盖真实实现。

    nuitka 装到本地缓存 ``~/.fspack/cache/nuitka/<py_version>/site-packages/``，
    不污染 ``dist/runtime`` 发行产物。编译时用 **standalone python**（非 embed runtime）
    运行 nuitka，避免 embed python 不完整导致 reExecute 进程衍生。

    Windows 编译 Python 来源：python-build-standalone Windows 版（完整 CPython 发行版，
    含 .py 源码），缓存到 ``~/.fspack/cache/python/<py_version>/python/python.exe``。
    Linux 直接用 runtime 的 standalone python（已是完整发行版）。

    用临时脚本文件而非 ``-c``：Nuitka 的 ``reExecuteNuitka`` 无条件访问
    ``sys.modules["__main__"].__file__``，``-c`` 模式下该属性不存在会
    ``AttributeError``。

    公共 API：

    - :meth:`ensure_env`：检查 C 编译器并按目标 Python 版本安装锁定版 nuitka 到本地缓存
    - :meth:`compile_src`：编译 ``dist/src`` 下所有 ``.py`` 为本机模块
    - :meth:`compile_with_stamp`：整合 ensure_env + stamp 缓存 + compile_src 的入口
    """
