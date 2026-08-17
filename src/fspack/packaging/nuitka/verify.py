"""Nuitka 编译产物验证：.pyd/.so 可加载性测试.

本模块是 :class:`fspack.packaging.nuitka.NuitkaCompiler` 的验证 mixin，
仅含 staticmethod/classmethod 无实例状态。通过多继承组合到 :class:`NuitkaCompiler`
facade，所有 ``cls.`` 调用经 MRO 自动派发。

职责边界：

- 模块名推导（``_find_package_root`` 兼容 flat/src layout）
- 批量 import 验证（一次 subprocess 测试所有模块，避免 N 次 subprocess 启动开销）
- 单模块 import 验证（批量测试崩溃时定位损坏的 .pyd）
- 导入失败分类（``_BINARY_LOAD_FAILURE_SNIPPET`` 区分二进制损坏与依赖缺失）

不涉及：环境就绪（见 :mod:`fspack.packaging.nuitka.env`）、
编译流程（见 :mod:`fspack.packaging.nuitka.compile`）。

**为何需要验证**：Nuitka 4.x 在 Python 3.13+ Windows 上忽略 ``CC`` 环境变量自动
回退到 zig 编译器，zig 编译的 .pyd 可能损坏（returncode==0、文件已生成，但运行时
访问违例 0xC0000005）。仅检查文件存在不够，必须实际 import 验证。

**损坏判定标准**（见 :data:`_BINARY_LOAD_FAILURE_SNIPPET`）：验证 subprocess
硬崩溃（访问违例）、或 .pyd 自身加载失败（``ImportError`` 无法定位缺失依赖名，
如 Windows ``DLL load failed while importing``）。**依赖缺失不算损坏**：模块顶层
``import PySide2`` 等第三方依赖在验证环境（sys.path 仅含包根）不存在时抛
``ModuleNotFoundError``，此时 .pyd 二进制已成功加载执行，产物有效，避免误删。
"""

from __future__ import annotations

import glob
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

# 共享 logger 名：测试用 caplog.at_level(..., logger="fspack.packaging.nuitka") 锁定
_logger = logging.getLogger("fspack.packaging.nuitka")

# 逐个验证并发数上限：subprocess 释放 GIL，线程并行启动多个 python 子进程。
# 与 _MAX_COMPILE_WORKERS 一致（4），平衡并行收益与 Windows 资源限制。
_MAX_VERIFY_WORKERS = 4

# 单次 import 验证 subprocess 超时（秒）：模块顶层含 input()/死循环/GUI 启动代码
# 时 subprocess 永不退出，无超时会使构建永久挂起。超时按"该模块验证失败"处理
# （batch 场景返回 None 触发逐个定位；individual 场景记该 .pyd 验证失败保留 .py）。
_IMPORT_TEST_TIMEOUT = 30.0

# 嵌入验证 subprocess 测试脚本的导入失败分类函数：判定失败是否源于 .pyd 二进制自身。
#
# 规则：
# - ``ImportError`` 且无法定位缺失模块名（``name`` 为 None，如 Windows
#   "DLL load failed while importing"）或缺失的正是被测模块自身 → 二进制加载失败（真损坏）
# - ``ModuleNotFoundError.name`` 指向第三方依赖（如 PySide2/pygame，验证环境
#   sys.path 仅含包根不含完整 site-packages）→ 依赖缺失，.pyd 已成功加载执行，有效
# - 非导入异常（模块顶层代码运行时错误）→ .pyd 已成功加载执行，有效
#
# 以字符串形式嵌入 ``-c`` 测试脚本（与 subprocess 启动的 python 无共享状态），
# 批量与逐个测试共用，保证两处分类口径一致。
_BINARY_LOAD_FAILURE_SNIPPET = """\
def _fspack_binary_load_failure(mod, e):
    if isinstance(e, ImportError):
        name = getattr(e, "name", None)
        return name is None or name == mod
    return False
"""


class NuitkaVerify:
    """Nuitka 编译产物验证 mixin：.pyd/.so 可加载性测试.

    所有方法为 staticmethod/classmethod，无实例状态。
    通过 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承组合使用。
    """

    @staticmethod
    def _find_package_root(py_file: Path) -> Path:
        """推导 .py 文件所在包的根目录（第一个无 ``__init__.py`` 的祖先目录）.

        用于 :meth:`_verify_compiled_modules` 自动推导模块名，兼容 flat layout
        与 src layout：

        - ``site-packages/rich/errors.py`` → ``site-packages/``（rich/ 有 __init__.py，
          site-packages/ 无），模块名 ``rich.errors``
        - ``dist/src/src/fspack/builder.py`` → ``dist/src/src/``（fspack/ 有 __init__.py，
          src/ 无），模块名 ``fspack.builder``
        - ``dist/src/main.py`` → ``dist/src/``（main.py 父目录无 __init__.py），模块名 ``main``

        从 .py 文件的父目录开始向上查找，当当前目录无 ``__init__.py`` 时停止，
        该目录即为包根（sys.path 应包含此目录才能 import 该模块）。
        """
        current = py_file.parent
        while (current / "__init__.py").is_file():
            current = current.parent
        return current

    @classmethod
    def _verify_compiled_modules(
        cls: type[NuitkaCompilerProtocol],
        py_exe: Path,
        compiled_files: set[Path],
    ) -> tuple[set[Path], list[Path]]:
        """用 subprocess 批量验证 .pyd 可加载，返回 (可加载的 .py 集合, 损坏 .pyd 路径列表).

        **为何需要 import 验证**：Nuitka 4.x 在 Python 3.13+ Windows 上忽略 ``CC``
        环境变量自动回退到 zig 编译器，zig 编译的 .pyd 可能损坏（returncode==0、
        文件已生成，但运行时访问违例 0xC0000005）。仅检查文件存在不够，必须实际
        import 验证。

        **依赖缺失不算损坏**：验证 subprocess 的 sys.path 仅含包根（无完整
        site-packages），模块顶层 ``import PySide2``/``import lxml`` 等第三方依赖
        缺失时抛 ``ModuleNotFoundError``——此时 .pyd 二进制已成功加载执行，产物
        有效（见 :data:`_BINARY_LOAD_FAILURE_SNIPPET` 分类规则）。仅二进制自身
        加载失败（硬崩溃、``DLL load failed while importing``）才判损坏。

        **批量测试策略**：一次 subprocess 测试所有模块，避免 N 次 subprocess 启动
        开销（100ms × N）。如果批量测试因损坏 .pyd 崩溃（returncode != 0），
        回退到逐个模块测试定位损坏的 .pyd。

        **模块名推导**：对每个 .py 文件调用 :meth:`_find_package_root` 自动推导包根
        （第一个无 ``__init__.py`` 的祖先目录），再用 .py 相对于包根的路径推导模块名。
        这样兼容 flat layout（``site-packages/rich/``）与 src layout
        （``dist/src/src/fspack/``），无需调用方传入 search_root。

        Args:
            py_exe: runtime python 可执行文件（.pyd ABI 绑定 runtime，必须用 runtime 验证）。
            compiled_files: 编译成功的 .py 文件集合。

        Returns:
            (可加载的 .py 文件集合, 损坏 .pyd/.so 产物路径列表)。
            损坏产物的 .py 不在可加载集合中，调用方应保留这些 .py 回退到 .pyc。
            依赖缺失的模块视为可加载（剥离 .py 后运行时由 site-packages 提供依赖）。
        """
        if not compiled_files:
            return set(), []

        # 构建模块名 → .py 文件路径映射，同时收集所有包根（去重）
        module_to_py: dict[str, Path] = {}
        py_to_artifacts: dict[Path, list[Path]] = {}
        package_roots: set[Path] = set()
        for py_file in compiled_files:
            # 自动推导包根（兼容 flat/src layout），不再依赖 search_root 推导模块名
            pkg_root = cls._find_package_root(py_file)
            package_roots.add(pkg_root)
            try:
                rel = py_file.relative_to(pkg_root)
            except ValueError:  # pragma: no cover - pkg_root 必为 py_file 祖先
                continue
            parts = rel.with_suffix("").parts
            module_name = ".".join(parts)
            if module_name.endswith(".__init__"):
                module_name = module_name[:-9]
            module_to_py[module_name] = py_file
            # glob.escape 转义文件名中的 glob 特殊字符（如 "report[v2].py" 的 []）
            stem = glob.escape(py_file.stem)
            artifacts = list(py_file.parent.glob(f"{stem}.*.pyd"))
            artifacts.extend(py_file.parent.glob(f"{stem}.*.so"))
            py_to_artifacts[py_file] = artifacts

        if not module_to_py:  # pragma: no cover - pkg_root 必为 py_file 祖先，module_to_py 不会为空
            # 无法推导模块名，信任编译结果
            return compiled_files, []

        # 一次 subprocess 批量测试所有模块（所有包根加入 sys.path）
        importable_modules = cls._batch_import_test(py_exe, sorted(package_roots), list(module_to_py.keys()))

        # 批量测试崩溃，逐个模块测试定位损坏的 .pyd
        if importable_modules is None:
            _logger.warning("批量验证 .pyd 崩溃，逐个模块测试定位损坏的 .pyd")
            importable_modules = cls._individual_import_test(py_exe, sorted(package_roots), list(module_to_py.keys()))

        # 构建结果：可加载的 .py 集合 + 损坏 .pyd 路径列表
        verified_files: set[Path] = set()
        unverified_artifacts: list[Path] = []
        for module_name, py_file in module_to_py.items():
            if module_name in importable_modules:
                verified_files.add(py_file)
            else:
                _logger.warning("模块 %s 的 .pyd 损坏（无法加载），保留 .py 回退到 .pyc", module_name)
                unverified_artifacts.extend(py_to_artifacts.get(py_file, []))

        return verified_files, unverified_artifacts

    @staticmethod
    def _batch_import_test(
        py_exe: Path,
        search_roots: list[Path],
        module_names: list[str],
    ) -> set[str] | None:
        """一次 subprocess 批量测试模块可加载性，返回二进制有效模块集合.

        subprocess 崩溃（returncode != 0，如访问违例）时返回 None，调用方应回退到
        :meth:`_individual_import_test` 逐个测试定位损坏的 .pyd。

        **返回值语义**：集合含"二进制有效"的模块——成功导入，或导入失败但属
        依赖缺失/模块代码层异常（.pyd 自身已成功加载，见
        :data:`_BINARY_LOAD_FAILURE_SNIPPET`）。仅 .pyd 自身加载失败不进入集合。

        用 ``importlib.import_module`` 而非 ``__import__``：支持含 ``-`` 等特殊字符
        的模块名（如 ``rich._unicode_data.unicode10-0-0``，不是合法 Python 标识符）。

        ``search_roots`` 支持多个包根（src layout 下可能有 ``dist/src/src/`` 与
        ``dist/src/`` 等多个根），测试脚本会把所有根加入 sys.path。

        超时（:data:`_IMPORT_TEST_TIMEOUT`）按验证失败处理：返回 None 让调用方
        回退到逐个测试定位（模块顶层含 input()/死循环/GUI 时 subprocess 永不退出，
        无超时会使构建永久挂起）。
        """
        import json

        # 构造 sys.path 注入代码：所有包根都加入 sys.path
        path_inserts = ";".join(f"sys.path.insert(0, r'{root}')" for root in search_roots)
        # 构造测试脚本：导入所有模块并输出 JSON 结果（分类规则见 _BINARY_LOAD_FAILURE_SNIPPET）
        test_code = (
            f"import sys; {path_inserts}\n"
            "import importlib, json\n"
            f"{_BINARY_LOAD_FAILURE_SNIPPET}"
            f"modules = {module_names!r}\n"
            "results = {}\n"
            "for mod in modules:\n"
            "    try:\n"
            "        importlib.import_module(mod)\n"
            "        results[mod] = True\n"
            "    except Exception as e:\n"
            "        results[mod] = not _fspack_binary_load_failure(mod, e)\n"
            "print('FSPACK_VERIFY_RESULT:' + json.dumps(results))\n"
        )
        try:
            result = subprocess.run(
                [str(py_exe), "-c", test_code],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_IMPORT_TEST_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # 模块顶层含 input()/死循环/GUI 等阻塞代码：按验证失败处理，
            # 返回 None 触发逐个测试定位真正阻塞的模块
            _logger.warning(
                "批量 import 验证超时（%ds），按失败处理并回退逐个定位", int(_IMPORT_TEST_TIMEOUT)
            )
            return None
        if result.returncode != 0:
            # subprocess 崩溃（如访问违例 0xC0000005），无法获取结果
            return None
        # 解析最后一行 JSON 结果（前缀 FSPACK_VERIFY_RESULT: 标识）
        for line in reversed(result.stdout.strip().split("\n")):
            if line.startswith("FSPACK_VERIFY_RESULT:"):
                try:
                    results = json.loads(line[len("FSPACK_VERIFY_RESULT:") :])
                    return {mod for mod, ok in results.items() if ok}
                except json.JSONDecodeError:
                    continue
        return None  # pragma: no cover - 输出格式异常回退到逐个测试

    @staticmethod
    def _individual_import_test(
        py_exe: Path,
        search_roots: list[Path],
        module_names: list[str],
    ) -> set[str]:
        """逐个模块测试可加载性，返回二进制有效模块集合.

        用于 :meth:`_batch_import_test` 崩溃时定位损坏的 .pyd。每个模块独立 subprocess，
        单个模块崩溃不影响其他模块测试。开销 O(N) subprocess 启动，仅在批量测试崩溃时触发。

        **返回值语义**：与 :meth:`_batch_import_test` 一致，集合含"二进制有效"的
        模块（依赖缺失/模块代码层异常均算有效，见
        :data:`_BINARY_LOAD_FAILURE_SNIPPET`）。测试脚本捕获所有可捕获异常并输出
        ``FSPACK_ONE_RESULT:0/1`` 标记后正常退出；仅硬崩溃（访问违例，returncode
        != 0 且无标记输出）判定损坏。

        **并发优化**（iter-137）：用 :class:`ThreadPoolExecutor` 并发启动 subprocess，
        ``max_workers = min(len(modules), :data:`_MAX_VERIFY_WORKERS`)``。
        subprocess 释放 GIL，线程并行启动多个 python 子进程。50 个损坏 .pyd 场景
        从串行 ~5s 降到并发 ~1.25s。

        ``search_roots`` 支持多个包根，测试脚本会把所有根加入 sys.path。

        超时（:data:`_IMPORT_TEST_TIMEOUT`）按该模块验证失败处理（返回 None 不进
        结果集合）：模块顶层含 input()/死循环/GUI 时 subprocess 永不退出，无超时
        会使构建永久挂起；验证失败的模块保留 .py 回退到 .pyc，不影响其他模块。
        """
        path_inserts = ";".join(f"sys.path.insert(0, r'{root}')" for root in search_roots)
        importable: set[str] = set()
        if not module_names:
            return importable

        def _test_one(mod: str) -> str | None:
            # 分类规则嵌入脚本（_BINARY_LOAD_FAILURE_SNIPPET）；所有可捕获异常
            # 均输出标记后 exit 0，仅硬崩溃（无法捕获的访问违例）returncode != 0
            test_code = (
                f"import sys; {path_inserts}\n"
                "import importlib\n"
                f"{_BINARY_LOAD_FAILURE_SNIPPET}"
                "try:\n"
                f"    importlib.import_module({mod!r})\n"
                "    print('FSPACK_ONE_RESULT:1')\n"
                "except Exception as e:\n"
                f"    print('FSPACK_ONE_RESULT:' + ('0' if _fspack_binary_load_failure({mod!r}, e) else '1'))\n"
            )
            try:
                result = subprocess.run(
                    [str(py_exe), "-c", test_code],
                    capture_output=True,
                    check=False,
                    timeout=_IMPORT_TEST_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                # 模块顶层含 input()/死循环/GUI 等阻塞代码：记该模块验证失败
                # （.pyd 判损坏，保留 .py 回退到 .pyc），不阻断其他模块测试
                _logger.warning("模块 %s import 验证超时（%ds），按验证失败处理", mod, int(_IMPORT_TEST_TIMEOUT))
                return None
            if result.returncode != 0:
                # 硬崩溃（访问违例等），判定损坏
                return None
            return mod if b"FSPACK_ONE_RESULT:1" in result.stdout else None

        max_workers = min(len(module_names), _MAX_VERIFY_WORKERS)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for mod in pool.map(_test_one, module_names):
                if mod is not None:
                    importable.add(mod)
        return importable
