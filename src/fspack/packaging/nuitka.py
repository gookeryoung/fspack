"""Nuitka 编译器：将用户源码 ``.py`` 编译为 ``.pyd`` 本机执行.

参考 RimSort 的 Nuitka 打包方案，用 ``python -m nuitka --module`` 将每个 ``.py``
编译为对应平台的 ``.pyd``（Windows）/ ``.so``（Linux）。运行时 ``.pyd`` 优先级
高于 ``.pyc``，Python 自动加载本机代码版本，执行速度提升 30-50%。

与 RimSort 区别：fspack 仅编译用户源码（``dist/src/``），第三方依赖保持 wheel
解压 + ``.pyc``（构建速度优先）。RimSort 用 Nuitka ``--follow-imports`` 全量编译，
构建耗时几十分钟；fspack 用户源码通常较小，编译时间可控。

Nuitka 必须安装在 runtime python 环境中（非构建机 python）。可用性检查失败时
告警并跳过编译，回退到 ``.pyc`` 模式。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from fspack.platform import Platform
from fspack.progress import StageRecorder

__all__ = ["NuitkaCompiler"]

_logger = logging.getLogger(__name__)


class NuitkaCompiler:
    """Nuitka 编译器：将用户源码编译为本机 ``.pyd``/``.so``.

    公共 API：

    - :meth:`is_available`：检查 runtime python 是否已安装 nuitka
    - :meth:`compile_src`：编译 ``dist/src`` 下所有 ``.py`` 为本机模块
    """

    @staticmethod
    def is_available(runtime_py: Path) -> bool:
        """检查 runtime python 是否已安装 nuitka 包.

        Args:
            runtime_py: runtime python 可执行文件路径（如 ``runtime/python.exe``）。

        Returns:
            已安装返回 ``True``，否则 ``False``。
        """
        if not runtime_py.is_file():
            return False
        result = subprocess.run(
            [str(runtime_py), "-c", "import nuitka"],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    @staticmethod
    def _runtime_python(runtime_dir: Path, py_version: str, target: Platform) -> Path:
        """解析 runtime python 可执行文件路径.

        Windows: ``runtime/python.exe``
        Linux: ``runtime/python/bin/python<major>.<minor>``
        """
        if target is Platform.WINDOWS:
            return runtime_dir / "python.exe"
        major, minor = py_version.split(".")[:2]
        return runtime_dir / "python" / "bin" / f"python{major}.{minor}"

    @staticmethod
    def compile_src(
        src_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        *,
        stage: StageRecorder,
    ) -> None:
        """编译 ``src_dir`` 下所有 ``.py`` 为 ``.pyd``/``.so``，编译后删除 ``.py`` 源码.

        步骤：

        1. 解析 runtime python 路径并检查 nuitka 可用性，不可用则告警并跳过
        2. 用 runtime python 调用 ``python -m nuitka --module`` 逐个编译 ``.py``
        3. 删除 ``.py`` 源码（保留 ``__init__.py`` 维持包标识，避免 PEP 420
           命名空间包导致 ``.pyd`` 不被识别为包成员）
        4. 清理 Nuitka 临时构建文件（``.build/`` 目录）

        单文件编译失败仅告警不中断，已成功编译的 ``.pyd`` 仍可用。``.py`` 删除
        策略与 :func:`fspack.builder._strip_py_sources` 一致：保留 ``__init__.py``
        维持包标识。

        Args:
            src_dir: 用户源码目录（``dist/src``）。
            runtime_dir: runtime 根目录（含 ``python.exe`` 或 ``python/bin/``）。
            py_version: Python 完整版本号（如 ``3.11.9``）。
            target: 目标平台（决定 runtime python 路径）。
            stage: 阶段记录器，记录编译项数与跳过数。
        """
        py_exe = NuitkaCompiler._runtime_python(runtime_dir, py_version, target)
        if not py_exe.is_file():
            _logger.warning("Nuitka 编译跳过: runtime python 未就绪 %s", py_exe)
            stage.set_detail("runtime python 未就绪，跳过")
            return

        if not NuitkaCompiler.is_available(py_exe):
            _logger.warning(
                "Nuitka 编译跳过: runtime python 未安装 nuitka，请用 '%s -m pip install nuitka' 安装",
                py_exe,
            )
            stage.set_detail("nuitka 未安装，跳过（回退到 .pyc 模式）")
            return

        py_files = sorted(src_dir.rglob("*.py"))
        if not py_files:
            stage.set_detail("无 .py 文件可编译")
            return

        # Nuitka 编译参数：
        # --module: 编译为可导入模块（.pyd/.so），不生成独立 exe
        # --output-dir: 输出目录与源码同目录（保持包结构）
        # --no-pyi-file: 不生成 .pyi 类型存根（运行时不需要）
        # --remove-output: 编译后删除临时构建文件（.build/ 目录）
        # --quiet: 静默模式，减少日志输出
        compiled = 0
        failed = 0
        for py_file in py_files:
            result = subprocess.run(
                [
                    str(py_exe),
                    "-m",
                    "nuitka",
                    "--module",
                    f"--output-dir={py_file.parent}",
                    "--no-pyi-file",
                    "--remove-output",
                    "--quiet",
                    str(py_file),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                compiled += 1
                stage.processed()
            else:
                failed += 1
                _logger.warning("Nuitka 编译失败 %s: %s", py_file, result.stderr.strip()[:200])

        # 删除非 __init__.py 的 .py 源码（保留包标识），与 pyc_strip 策略一致
        stripped = 0
        for py_file in py_files:
            if py_file.name == "__init__.py":
                continue
            try:
                py_file.unlink()
                stripped += 1
            except OSError as e:
                _logger.warning("删除 .py 失败 %s: %s", py_file, e)
        if stripped:
            stage.skip(stripped)

        # Nuitka 临时构建目录由 --remove-output 自动清理，无需额外处理

        if failed:
            stage.set_detail(f"编译 {compiled} 个，失败 {failed} 个，剥离 {stripped} 个 .py")
        else:
            stage.set_detail(f"编译 {compiled} 个，剥离 {stripped} 个 .py")
