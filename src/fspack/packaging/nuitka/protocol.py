"""NuitkaCompiler mixin 跨类调用接口契约.

定义 :class:`fspack.packaging.nuitka.NuitkaCompiler` 各 mixin 间跨类调用的
方法签名。各 mixin 的 classmethod 用 ``cls: type[NuitkaCompilerProtocol]``
注解替代裸 ``cls``，让 pyrefly 能解析跨 mixin 的 ``cls.<method>()`` 调用，
消除 ``# type: ignore[attr-defined]`` 抑制与 NuitkaCompile 顶部的 stub 方法占位。

设计要点：

- Protocol 仅在类型检查期生效，运行时无开销（``runtime_checkable`` 仅用于
  ``isinstance`` 检查，此处不用）
- Protocol 声明 NuitkaCompiler facade 的所有方法（含各 mixin 提供的 +
  NuitkaCompile 自身的），按"提供者"分组注释
- 各 mixin 的 classmethod 第一个参数注解为 ``type[NuitkaCompilerProtocol]``，
  pyrefly 据此解析 ``cls.<method>()`` 调用（含跨 mixin 与同 mixin 内调用）
- ``NuitkaCompiler`` facade 多继承组合各 mixin，运行时 MRO 派发真实实现，
  Protocol 不参与运行时

为何不用 stub 方法占位：

NuitkaCompile 顶部原有 10 个 stub 方法（``raise NotImplementedError``），
但 NuitkaStrip 调 ``cls._verify_compiled_modules``（NuitkaVerify 提供）不能
stub——NuitkaStrip 在 MRO 中位于 NuitkaVerify 前面，stub 会覆盖 NuitkaVerify
的真实实现破坏运行时。Protocol 方案统一所有 mixin 的类型声明，无需 stub。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from fspack.config import MirrorConfig
from fspack.platform import Platform
from fspack.progress import StageRecorder


class NuitkaCompilerProtocol(Protocol):  # pragma: no cover - 纯类型契约，无运行时代码
    """NuitkaCompiler facade 与各 mixin 间跨类调用的接口契约.

    各 mixin 的 classmethod 用 ``cls: type[NuitkaCompilerProtocol]`` 注解，
    pyrefly 据此解析 ``cls.<method>()`` 跨 mixin 与同 mixin 内调用。Protocol
    声明的方法按"提供者 mixin"分组，签名与真实实现一致（含默认参数与关键字参数）。

    运行时由 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承 MRO 派发
    到对应 mixin 的真实实现，Protocol 不参与运行时。
    """

    # ==== NuitkaEnv 提供（环境就绪）====

    @staticmethod
    def _nuitka_cache_dir(cache_root: Path, py_version: str) -> Path:
        """nuitka 缓存目录：``cache_root / py_version / site-packages``."""
        ...

    @staticmethod
    def _is_nuitka_cached(cache_dir: Path) -> bool:
        """检查缓存目录是否有 nuitka 包."""
        ...

    @staticmethod
    def _runtime_python(runtime_dir: Path, py_version: str, target: Platform) -> Path:
        """解析 runtime python 可执行文件路径."""
        ...

    @staticmethod
    def _build_compile_env(target: Platform, ccache_exe: Path | None) -> dict[str, str]:
        """构建编译环境变量（CC/CFLAGS）."""
        ...

    @classmethod
    def ensure_env(
        cls,
        cache_root: Path,
        py_version: str,
        target: Platform,
        mirror: MirrorConfig,
        *,
        stage: StageRecorder,
    ) -> str:
        """检查 C 编译器并安装锁定版 nuitka 到本地缓存."""
        ...

    # ==== NuitkaStandalone 提供（standalone python 准备）====

    @classmethod
    def _ensure_build_python(
        cls,
        cache_root: Path,
        py_version: str,
        target: Platform,
        *,
        stage: StageRecorder,
    ) -> Path:
        """准备 standalone python（Windows 专用）."""
        ...

    # ==== NuitkaWinlibs 提供（winlibs-mingw 工具链管理）====

    @staticmethod
    def _winlibs_gcc_dir(nuitka_ver: str) -> Path:
        """返回 winlibs gcc 缓存目录（不含 gcc.exe 自身）."""
        ...

    @classmethod
    def ensure_winlibs_mingw(
        cls,
        py_version: str,
        stage: StageRecorder,
    ) -> Path:
        """确保 Nuitka 所需的 winlibs-mingw 工具链就绪，返回下载缓存根目录."""
        ...

    @staticmethod
    def _download_and_extract_winlibs(nuitka_ver: str, gcc_dir: Path, gcc_exe: Path) -> None:
        """下载 winlibs zip 并解压到 gcc_dir，验证 gcc.exe 就位."""
        ...

    # ==== NuitkaCcache 提供（ccache 管理）====

    @classmethod
    def _ensure_ccache(cls, cache_root: Path, target: Platform, stage: StageRecorder) -> Path | None:
        """下载或查找 ccache 可执行文件."""
        ...

    # ==== NuitkaStrip 提供（产物剥离与构建目录清理）====

    @classmethod
    def _strip_compiled_sources(
        cls,
        compiled_files: set[Path],
        stage: StageRecorder,
        *,
        verify_py_exe: Path | None = None,
        verify_search_root: Path | None = None,
    ) -> int:
        """删除成功编译的 .py 源码，返回删除数."""
        ...

    @staticmethod
    def _cleanup_build_dirs(base_dir: Path) -> int:
        """清理 Nuitka 残留 .build/ 目录."""
        ...

    # ==== NuitkaVerify 提供（编译产物验证）====

    @classmethod
    def _verify_compiled_modules(
        cls,
        py_exe: Path,
        compiled_files: set[Path],
    ) -> tuple[set[Path], list[Path]]:
        """批量验证 .pyd 可加载，返回 (可加载 .py 集合, 损坏 .pyd 路径列表)."""
        ...

    # ==== NuitkaCompile 提供（编译流程）====

    @staticmethod
    def _stream_compile(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        """运行 nuitka 编译命令，实时流式输出."""
        ...

    @classmethod
    def compile_src(  # noqa: PLR0913
        cls,
        src_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        nuitka_cache: Path,
        *,
        stage: StageRecorder,
        build_python_exe: Path | None = None,
        entry_rels: frozenset[str] | None = None,
        ccache: bool = False,
        cache_root: Path | None = None,
        skip_files: frozenset[str] | None = None,
    ) -> list[str]:
        """编译 src_dir 下所有 .py 为 .pyd/.so，返回失败文件相对 POSIX 路径列表."""
        ...

    @classmethod
    def compile_packages(  # noqa: PLR0913
        cls,
        site_packages: Path,
        packages: tuple[str, ...],
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        nuitka_cache: Path,
        *,
        stage: StageRecorder,
        build_python_exe: Path | None = None,
        ccache: bool = False,
        cache_root: Path | None = None,
    ) -> None:
        """编译 site-packages 中指定的第三方包."""
        ...

    @classmethod
    def _resolve_compile_python(
        cls,
        build_python_exe: Path | None,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        stage: StageRecorder,
    ) -> Path | None:
        """解析编译用 python 路径."""
        ...

    @staticmethod
    def _collect_py_files(
        src_dir: Path,
        entry_rels: frozenset[str] | None,
        skip_files: frozenset[str] | None = None,
    ) -> list[Path]:
        """收集待编译的 .py 文件，排除上次失败文件."""
        ...

    @staticmethod
    def _create_bootstrap_script(nuitka_cache: Path) -> Path:
        """创建临时 bootstrap 脚本注入 sys.path 调用 nuitka."""
        ...

    @classmethod
    def _compile_files(  # noqa: PLR0913
        cls,
        py_exe: Path,
        bootstrap_script: Path,
        py_files: list[Path],
        stage: StageRecorder,
        *,
        target: Platform,
        ccache_exe: Path | None = None,
    ) -> tuple[set[Path], list[Path]]:
        """逐个编译 .py 文件，返回 (成功编译的文件集合, 失败文件路径列表)."""
        ...

    @staticmethod
    def _stamp_path(dist_dir: Path) -> Path:
        """返回 Nuitka 编译 stamp 文件路径."""
        ...

    @staticmethod
    def _stamp_key(
        src_dir: Path,
        nuitka_version: str,
        py_version: str,
        entry_rels: frozenset[str] | None = None,
        nuitka_packages: tuple[str, ...] = (),
    ) -> str:
        """计算 Nuitka 编译 stamp 键."""
        ...

    @classmethod
    def compile_with_stamp(  # noqa: PLR0913
        cls,
        src_dir: Path,
        dist_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        mirror: MirrorConfig,
        cache_root: Path,
        *,
        stage: StageRecorder,
        entry_rels: frozenset[str] | None = None,
        ccache: bool = False,
        nuitka_packages: tuple[str, ...] = (),
    ) -> None:
        """整合 ensure_env + standalone python + stamp 缓存 + compile_src 的入口."""
        ...

    # ==== NuitkaEnv 提供的辅助（pip/C 编译器检查 + ensure_pip 子步骤）====

    @staticmethod
    def _check_c_compiler(target: Platform) -> None:
        """检查目标平台 C 编译器是否可用，不可用则抛 NuitkaError."""
        ...

    @staticmethod
    def _has_pip(python_exe: str) -> bool:
        """检查 python 解释器是否有可用的 pip 模块."""
        ...

    @staticmethod
    def _try_ensurepip(python_exe: str) -> bool:
        """第一轮自救：``python -m ensurepip --default-pip`` 安装 pip."""
        ...

    @staticmethod
    def _try_uv_install_pip() -> bool:
        """第二轮自救：``uv pip install pip`` 安装 pip 到当前 venv."""
        ...

    @classmethod
    def _ensure_pip_available(cls, python_exe: str) -> None:
        """确保构建机 python 有 pip 模块，缺失则两轮自救，仍失败则抛异常."""
        ...

    # ==== NuitkaStandalone 提供的辅助（缓存目录推导 + 下载/解压）====

    @staticmethod
    def _build_python_cache_dir(cache_root: Path, py_version: str) -> Path:
        """返回 standalone python 缓存目录：``cache_root / py_version``."""
        ...

    @staticmethod
    def _build_python_exe(build_python_dir: Path, py_version: str, target: Platform) -> Path:
        """返回 standalone python 可执行文件路径."""
        ...

    @staticmethod
    def _host_python_exe(py_version: str) -> Path | None:
        """构建机 python 可直接运行 nuitka 时返回其路径，否则返回 None."""
        ...

    @classmethod
    def _download_standalone_python(
        cls,
        build_python_dir: Path,
        standalone_version: str,
        stage: StageRecorder,
    ) -> Path:
        """确保 standalone tarball 就绪（共享缓存优先），返回归档路径."""
        ...

    @classmethod
    def _extract_standalone_python(
        cls,
        build_python_dir: Path,
        standalone_version: str,
    ) -> None:
        """解压 standalone python 并提升内层目录到缓存根."""
        ...

    # ==== NuitkaCcache 提供的辅助（下载解压）====

    @staticmethod
    def _download_and_extract_ccache(url: str, ccache_dir: Path, target: Platform) -> None:
        """下载 ccache 归档并解压二进制到 ``ccache_dir``."""
        ...

    # ==== NuitkaVerify 提供的辅助（包根推导 + 批量/逐个导入测试）====

    @staticmethod
    def _find_package_root(py_file: Path) -> Path:
        """推导 .py 文件所在包的根目录（无 ``__init__.py`` 的祖先目录）."""
        ...

    @staticmethod
    def _batch_import_test(
        py_exe: Path,
        search_roots: list[Path],
        module_names: list[str],
    ) -> set[str] | None:
        """一次 subprocess 批量测试模块可加载性，崩溃时返回 None."""
        ...

    @staticmethod
    def _individual_import_test(
        py_exe: Path,
        search_roots: list[Path],
        module_names: list[str],
    ) -> set[str]:
        """逐个模块 subprocess 测试可加载性，返回可加载模块集合."""
        ...
