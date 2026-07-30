"""Nuitka 编译产物剥离与构建目录清理.

本模块是 :class:`fspack.packaging.nuitka.NuitkaCompiler` 的产物处理 mixin，
仅含 staticmethod/classmethod 无实例状态。通过多继承组合到 ``NuitkaCompiler``
facade，所有 ``cls.`` 调用经 MRO 自动派发到对应 mixin。

职责边界：

- 产物剥离（``_strip_compiled_sources`` 验证 .pyd 可加载后删 .py）
- 构建目录清理（``_cleanup_build_dirs`` 清理 Nuitka 残留 ``.build/`` 目录）

不涉及：编译流程（见 :mod:`fspack.packaging.nuitka.compile`）、
环境就绪（见 :mod:`fspack.packaging.nuitka.env`）、
验证逻辑（见 :mod:`fspack.packaging.nuitka.verify`，通过 ``cls._verify_compiled_modules``
经 MRO 调用）。

从 :mod:`fspack.packaging.nuitka.compile` 拆分而来，降低 ``compile.py`` 行数
与职责复杂度。产物剥离与构建目录清理同属"编译后处理"，独立成 mixin 便于复用与测试。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.progress import StageRecorder

if TYPE_CHECKING:
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

# 共享 logger 名：测试用 caplog.at_level(..., logger="fspack.packaging.nuitka") 锁定
_logger = logging.getLogger("fspack.packaging.nuitka")


class NuitkaStrip:
    """Nuitka 编译产物剥离与构建目录清理 mixin.

    所有方法为 staticmethod/classmethod，无实例状态。
    通过 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承组合使用。

    依赖 :class:`fspack.packaging.nuitka.verify.NuitkaVerify` 提供：
    ``_verify_compiled_modules``（NuitkaVerify 在 MRO 后置，无法 stub 占位，
    用 :class:`NuitkaCompilerProtocol` 类型契约声明跨 mixin 调用签名）。
    """

    @classmethod
    def _strip_compiled_sources(
        cls: type[NuitkaCompilerProtocol],
        compiled_files: set[Path],
        stage: StageRecorder,
        *,
        verify_py_exe: Path | None = None,
        verify_search_root: Path | None = None,
    ) -> int:
        """删除成功编译的 .py 源码（.pyd/.so 已生成可替代），返回删除数.

        **必须验证 .pyd/.so 真的存在才删除 .py**：Nuitka 可能 returncode==0 但未生成
        .pyd（如文件名含 ``-`` 触发 Nuitka 内部静默失败），此时删除 .py 会导致运行时
        ImportError/访问违例。验证产物存在避免误删。

        **可选 import 验证**（``verify_py_exe`` + ``verify_search_root``）：Nuitka 4.x
        在 Python 3.13+ Windows 上忽略 ``CC`` 环境变量自动回退到 zig 编译器，zig 编译的
        .pyd 可能损坏（returncode==0、文件已生成，但运行时访问违例 0xC0000005）。
        提供验证参数时，删除 .py 前用 subprocess 批量 import 验证 .pyd 可加载，
        不可加载的 .pyd 删除产物并保留 .py，回退到 .pyc 加载。

        Nuitka ``--module`` 输出文件名格式：
        - Windows: ``{stem}.cp{major}{minor}-{platform}.pyd``
        - Linux: ``{stem}.cpython-{major}{minor}-{platform}.so``

        用 ``{stem}.*.pyd`` / ``{stem}.*.so`` glob 匹配覆盖所有命名变体。

        失败的 .py 保留：运行时回退到 .pyc 加载，避免编译失败导致 dist/src 无可用代码。
        ``__init__.py`` 不在 ``compiled_files`` 中（收集时已跳过），无需额外检查。
        """
        # 可选 import 验证：防止 zig 编译的损坏 .pyd 导致运行时崩溃
        files_to_strip = compiled_files
        if verify_py_exe is not None and verify_search_root is not None and compiled_files:
            verified_files: set[Path] = set(compiled_files)
            unverified_artifacts: list[Path] = []
            try:
                verified_files, unverified_artifacts = (
                    cls._verify_compiled_modules(  # NuitkaVerify mixin（Protocol 类型契约，运行时 MRO 派发）
                        verify_py_exe, compiled_files
                    )
                )
            except OSError as e:
                # runtime python 不可执行（如测试桩空文件）或 subprocess 启动失败，
                # 跳过验证信任编译结果（运行时 .pyd 加载失败会回退到 .pyc）
                _logger.warning("验证 .pyd 可加载性失败，跳过验证: %s", e)
            # 删除损坏的 .pyd/.so，避免运行时优先加载损坏的产物（.pyd 优先级高于 .pyc）
            for artifact in unverified_artifacts:
                try:
                    artifact.unlink()
                    _logger.warning("删除损坏的 .pyd/.so 产物: %s", artifact)
                except OSError as e:  # pragma: no cover - 文件被并发锁定或权限问题
                    _logger.warning("删除损坏 .pyd/.so 失败 %s: %s", artifact, e)
            files_to_strip = verified_files

        stripped = 0
        for py_file in files_to_strip:
            stem = py_file.stem
            # 检查 .pyd/.so 产物是否真实存在（glob 的 * 跨 . 匹配所有命名变体）
            artifacts = list(py_file.parent.glob(f"{stem}.*.pyd"))
            artifacts.extend(py_file.parent.glob(f"{stem}.*.so"))
            if not artifacts:
                _logger.warning("编译标记成功但未找到 .pyd/.so 产物，保留 .py 避免运行时缺失: %s", py_file)
                continue
            try:
                py_file.unlink()
                stripped += 1
            except OSError as e:
                _logger.warning("删除 .py 失败 %s: %s", py_file, e)
        if stripped:
            stage.skip(stripped)
        return stripped

    @staticmethod
    def _cleanup_build_dirs(base_dir: Path) -> int:
        """清理 Nuitka 残留的 ``<name>.build/`` 目录，返回清理数.

        Nuitka ``--remove-output`` 只在编译成功时清理 ``.build/``，失败时残留。
        残留的 ``.build/`` 目录含 scons 中间文件（.c/.o/.const 等），对最终用户无用，
        且会被下次 ``_collect_py_files`` 扫到（已通过 ``endswith(".build")`` 排除）。
        编译后统一清理避免污染 dist 产物。
        """
        cleaned = 0
        for build_dir in base_dir.rglob("*.build"):
            if not build_dir.is_dir():
                continue
            try:
                shutil.rmtree(build_dir)
                cleaned += 1
            except OSError as e:
                _logger.warning("清理 .build 目录失败 %s: %s", build_dir, e)
        if cleaned:
            _logger.info("清理 Nuitka 残留 .build 目录: %d 个", cleaned)
        return cleaned
