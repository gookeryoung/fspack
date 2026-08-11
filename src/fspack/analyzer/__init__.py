"""AST 依赖分析：扫描 import，分类标准库/本地/第三方.

facade 子包：编排 :mod:`fspack.analyzer.ast_scan`（AST 解析）与
:mod:`fspack.analyzer.fingerprint`（源码指纹）完成依赖分析。本模块保留
:func:`analyze_dependencies` 的并行调度逻辑与本地包识别，以及进程池 worker
函数 :func:`_parse_file_worker`（须在 ``fspack.analyzer`` 命名空间保持模块级
可解析以支持 pickle 跨进程传递）。

同时扫描 QML 文件（``.qml``）中的 ``import QtXxx`` 语句，将 QML 运行时
依赖映射为 Qt 子模块名（如 ``QtQuick`` → ``Quick``），补充 AST 静态分析
无法发现的 QML 运行时依赖——QML 引擎加载 ``qml/QtQuick.2/qtquick2plugin.dll``
时依赖 ``Qt5Quick.dll``，但 Python 入口仅 ``import PySide2.QtQml`` 不会
触发 ``Quick`` 子模块保留，导致 DLL 缺失。
"""

from __future__ import annotations

import ast
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

from fspack.analyzer.ast_scan import (
    _QT_PYTHON_PACKAGES,
    _STDLIB,
    STDLIB_FALLBACK,
    _qml_module_to_qt_sub,
    collect_imports,
    collect_imports_and_submodules,
    collect_submodule_imports,
    parse_qml_imports,
)
from fspack.analyzer.fingerprint import (
    _is_excluded,
    source_fingerprint,
)
from fspack.config import DependencyReport

__all__ = [
    "STDLIB_FALLBACK",
    "_qml_module_to_qt_sub",
    "analyze_dependencies",
    "collect_imports",
    "collect_imports_and_submodules",
    "collect_submodule_imports",
    "parse_qml_imports",
    "source_fingerprint",
]

_logger = logging.getLogger(__name__)


def _local_packages(src_dir: Path, project_name: str) -> set[str]:
    """识别项目本地包/模块名（顶层 .py 与含 __init__.py 的目录）.

    用 :func:`os.scandir` 替代 :meth:`Path.iterdir`，避免 ``Path`` 包装
    开销与重复 stat 调用：``DirEntry.is_file``/``is_dir`` 复用枚举时的 stat
    缓存（Windows ``WIN32_FIND_DATA`` / Linux ``d_ino``）。
    """
    local: set[str] = {project_name}
    for entry in os.scandir(src_dir):
        name = entry.name
        if entry.is_file() and name.endswith(".py"):
            local.add(name[:-3])
        elif entry.is_dir() and (src_dir / name / "__init__.py").is_file():
            local.add(name)
    return local


def analyze_dependencies(  # noqa: PLR0912
    src_dir: Path,
    project_name: str,
    declared: tuple[str, ...],
    data_dirs: tuple[str, ...] = (),
) -> DependencyReport:
    """扫描 src_dir 下所有 .py 与 .qml，分类 import 为标准库/本地/第三方。

    自动排除 dist/build/.venv 等构建产物与缓存目录，避免扫描到已解包的
    embed python 或 python-build-standalone 标准库源码导致误报依赖。

    ``data_dirs`` 为 ``[tool.fspack] data-dirs`` 配置的数据资源目录树（相对
    ``src_dir`` 的 POSIX 路径），其下 ``.py`` 是模板/前端产物等数据资源，
    不应被 AST 扫描误判为项目依赖（如 fspack 打包自身时，``assets/init_templates``
    下的 tkinter 模板不应让 fspack 依赖 tkinter）。

    文件数超过 :data:`_PARALLEL_THRESHOLD` 时使用 :class:`ProcessPoolExecutor`
    并行解析（CPU 密集 ``ast.parse``），大项目显著提速。小项目走串行路径
    避免进程池启动开销（Windows spawn 约 100-200ms，需足够工作量摊销）。

    QML 文件（``.qml``）中的 ``import QtXxx`` 语句会被解析并映射为 Qt 子模块名
    （如 ``QtQuick`` → ``Quick``），加入对应 Qt 绑定包（PySide2/PySide6/PyQt5/PyQt6）
    的子模块集合——QML 引擎加载插件时依赖 ``Qt5Quick.dll`` 等 C 层 DLL，但 Python
    入口仅 ``import PySide2.QtQml`` 不会触发 ``Quick`` 子模块保留，AST 无法发现
    此运行时依赖。
    """
    resolved_data_dirs = tuple((src_dir / Path(rel)).resolve() for rel in data_dirs)
    py_files: list[Path] = [py for py in src_dir.rglob("*.py") if not _is_excluded(py, src_dir, resolved_data_dirs)]

    all_imports: list[str] = []  # 非标准库顶层导入（local + third_party）
    all_stdlib: list[str] = []  # 标准库顶层导入（worker/串行已分离）
    all_submodules: dict[str, set[str]] = {}
    all_errors: list[tuple[str, str]] = []  # AST 解析失败记录 (abs_path, error_msg)（iter-138）

    if len(py_files) >= _PARALLEL_THRESHOLD:
        _parse_parallel(py_files, all_imports, all_stdlib, all_submodules, all_errors)
    else:
        _parse_serial(py_files, all_imports, all_stdlib, all_submodules, all_errors)

    # 扫描 QML 文件提取 QtQuick 等 QML 运行时依赖（AST 无法发现）
    # 仅当项目 import 了 Qt 绑定包时才扫描，避免非 Qt 项目无谓 I/O
    imported_qt_pkgs = _QT_PYTHON_PACKAGES & set(all_imports)
    if imported_qt_pkgs:
        qml_files: list[Path] = [
            qml for qml in src_dir.rglob("*.qml") if not _is_excluded(qml, src_dir, resolved_data_dirs)
        ]
        qml_qt_subs: set[str] = set()
        for qml_file in qml_files:
            # 防御性 try/except：parse_qml_imports 内部已 catch OSError，但其他异常
            # （如权限错误、文件系统异常）不应阻塞依赖分析主流程（iter-138）
            try:
                qml_qt_subs.update(parse_qml_imports(qml_file))
            except OSError as e:
                _logger.warning("QML 文件解析失败，跳过: %s: %s", qml_file, e)
        if qml_qt_subs:
            for qt_pkg in imported_qt_pkgs:
                all_submodules.setdefault(qt_pkg, set()).update(qml_qt_subs)

    local = _local_packages(src_dir, project_name)
    stdlib: list[str] = []
    third: list[str] = []
    local_imports: list[str] = []
    seen: set[str] = set()
    # all_imports 已由 worker/串行分离掉标准库，此处仅需区分 local vs third_party
    for imp in all_imports:
        if imp in seen:
            continue
        seen.add(imp)
        if imp in local:
            local_imports.append(imp)
        else:
            third.append(imp)
    # 标准库导入去重保序（worker/串行已分离，主进程无需再分类）
    seen_std: set[str] = set()
    for imp in all_stdlib:
        if imp in seen_std:
            continue
        seen_std.add(imp)
        stdlib.append(imp)
    ast_submodules = {
        pkg: frozenset(subs) for pkg, subs in all_submodules.items() if pkg not in local and pkg not in _STDLIB
    }
    # AST 错误格式化为 "<相对 src_dir 路径>: <错误信息>"，供上层向用户提示
    ast_errors = tuple(_format_ast_errors(src_dir, all_errors))
    return DependencyReport(
        declared=declared,
        ast_third_party=tuple(third),
        ast_stdlib=tuple(stdlib),
        ast_local=tuple(local_imports),
        ast_submodules=ast_submodules,
        ast_errors=ast_errors,
    )


def _format_ast_errors(src_dir: Path, errors: list[tuple[str, str]]) -> list[str]:
    """将 AST 错误 ``(abs_path, error_msg)`` 格式化为 ``"<相对 src_dir 路径>: <错误信息>"``.

    相对路径转换失败（如不同盘符）回退到绝对路径。iter-138 引入：worker 返回绝对路径
    与错误信息元组，主进程统一格式化为相对路径供用户阅读。
    """
    formatted: list[str] = []
    for abs_path, msg in errors:
        try:
            rel = Path(abs_path).relative_to(src_dir).as_posix()
        except ValueError:
            rel = abs_path
        formatted.append(f"{rel}: {msg}")
    return formatted


# 并行解析阈值：低于此文件数走串行，避免进程池启动开销
# Windows spawn 启动 ~100-200ms，需足够工作量摊销；Linux fork 较快可更低
_PARALLEL_THRESHOLD = 200

# 并行解析整体超时（秒）：``as_completed(timeout=)`` 从调用起算的总等待时间，
# 任一 future 未就绪则抛 ``TimeoutError``。实测 500 文件 P99 <30s（8 核），
# 300s 裕量覆盖慢速 CI 与病态输入（深度嵌套 AST）。iter-127 引入。
# iter-138 改用 ``submit`` + ``as_completed``：单个 worker 卡死不阻塞其他 worker
# 的结果聚合（``map(timeout=)`` 在首个 future 卡死时丢弃后续已完成结果）。
_PARSE_TOTAL_TIMEOUT = 300.0


# worker 进程状态：由 :func:`_init_parse_worker` 设置，避免每个 worker 重新
# 构建 :data:`STDLIB_FALLBACK`（3.8/3.9 ~200 元素 frozenset）。用 dict 容器
# 避免 ``global`` 语句（ruff PLW0603），worker 通过 ``_WORKER_STATE["stdlib"]``
# 读取预加载的 :data:`_STDLIB`。主进程不使用此容器——主进程直接用模块级
# :data:`_STDLIB`（:mod:`fspack.analyzer.ast_scan`）。
_WORKER_STATE: dict[str, frozenset[str]] = {"stdlib": frozenset()}


def _init_parse_worker(stdlib: frozenset[str]) -> None:
    """进程池 worker initializer：预加载 ``_STDLIB`` 到 worker 全局.

    由 :class:`ProcessPoolExecutor` 在每个 worker 进程启动时调用一次，
    将主进程已构建的 :data:`_STDLIB` 传递给 worker，避免 worker 重新构建
    （3.8/3.9 的 :data:`STDLIB_FALLBACK` 构建开销）并确保与主进程分类一致。

    worker 启动时已通过 spawn import :mod:`fspack.analyzer`（连带加载
    :mod:`fspack.analyzer.ast_scan`），initializer 在此之后执行，使第一次
    :func:`_parse_file_worker` 调用时 ``_WORKER_STATE["stdlib"]`` 已就绪，
    无需模块属性查找。
    """
    _WORKER_STATE["stdlib"] = stdlib


def _parse_file_worker(py: str) -> tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]:
    """进程池 worker：解析单个 .py 文件返回 ``(非标准库导入, 标准库导入, 子模块字典, AST 错误列表)``.

    用 worker 全局 :data:`_WORKER_STATE` （由 :func:`_init_parse_worker` 设置）
    将顶层导入分离为标准库与非标准库，减少主进程分类循环工作量与 IPC 数据量
    （标准库导入通常占少数，主进程仅需区分 local vs third_party）。

    ``_WORKER_STATE["stdlib"]`` 为空时（主进程直接调用、或 initializer 未设置）
    回退到模块级 :data:`_STDLIB`，保证主进程直接调用（如单元测试）也能正确分离。

    错误文件返回空结果与错误列表 ``([], [], {}, [(py, error_msg)])``（iter-138）：
    不再静默跳过，记录 ``(绝对路径 str, 错误信息)`` 元组供主进程格式化报告。
    模块级函数确保可 pickle 跨进程传递；接收 ``str`` 路径（比 ``Path`` 序列化更轻量）。

    用 :meth:`Path.read_bytes` + :func:`ast.parse(bytes)`，避免 Python 层
    decode 中间步骤（详见 :func:`_parse_serial`）。
    """
    try:
        tree = ast.parse(Path(py).read_bytes())
    except (SyntaxError, OSError) as e:
        return [], [], {}, [(py, str(e))]
    tops, subs = collect_imports_and_submodules(tree)
    stdlib_ref = _WORKER_STATE["stdlib"] or _STDLIB
    stdlib_tops: list[str] = []
    non_stdlib_tops: list[str] = []
    for top in tops:
        if top in stdlib_ref:
            stdlib_tops.append(top)
        else:
            non_stdlib_tops.append(top)
    return non_stdlib_tops, stdlib_tops, subs, []


def _parse_serial(
    py_files: list[Path],
    all_imports: list[str],
    all_stdlib: list[str],
    all_submodules: dict[str, set[str]],
    all_errors: list[tuple[str, str]],
) -> None:
    """串行解析所有 .py 文件，结果合并到 ``all_imports`` / ``all_stdlib`` / ``all_submodules`` / ``all_errors``.

    用模块级 :data:`_STDLIB` 将顶层导入分离为标准库（``all_stdlib``）与非标准库
    （``all_imports``），与 :func:`_parse_file_worker` 的 worker 分离逻辑一致，
    使主进程分类循环仅需区分 local vs third_party。

    AST 解析失败（SyntaxError/OSError）记录到 ``all_errors``（iter-138）：
    不再静默跳过，记录 ``(绝对路径 str, 错误信息)`` 元组供主进程格式化报告。

    用 :meth:`Path.read_bytes` + :func:`ast.parse(bytes)`，避免 Python 层
    ``decode("utf-8")`` 中间步骤——``ast.parse`` 内部用 C 实现解码，比
    显式 ``str.decode`` 快约 5-10%。基线测试 50 文件场景下可见微收益。
    """
    for py in py_files:
        try:
            tree = ast.parse(py.read_bytes())
        except (SyntaxError, OSError) as e:
            all_errors.append((str(py), str(e)))
            continue
        tops, subs = collect_imports_and_submodules(tree)
        for top in tops:
            if top in _STDLIB:
                all_stdlib.append(top)
            else:
                all_imports.append(top)
        for pkg, sub_set in subs.items():
            all_submodules.setdefault(pkg, set()).update(sub_set)


def _interleave_by_size(py_files: list[Path], num_chunks: int) -> list[Path]:
    """按文件大小 interleave 重排，使连续分块（``map(chunksize=)``）工作量均衡.

    按文件大小降序排序后，按 ``sized[i::num_chunks]`` 形成 ``num_chunks`` 组并拼接。
    每组含大、中、小文件混合（第 0 组含最大、第 num_chunks 大、第 2*num_chunks 大...），
    使 ``map(chunksize=len//num_chunks)`` 连续切分时每个 chunk 的总工作量大致均衡，
    避免大文件扎堆在某个 chunk 导致该 worker 成为瓶颈（iter-134）。

    ``Path.stat`` 失败的文件按 size=0 处理（不阻塞解析）。
    """
    if num_chunks <= 1 or len(py_files) <= 1:
        return list(py_files)
    sized = sorted(
        py_files,
        key=lambda p: p.stat().st_size if p.exists() else 0,
        reverse=True,
    )
    interleaved: list[Path] = []
    for i in range(num_chunks):
        interleaved.extend(sized[i::num_chunks])
    return interleaved


def _parse_parallel(
    py_files: list[Path],
    all_imports: list[str],
    all_stdlib: list[str],
    all_submodules: dict[str, set[str]],
    all_errors: list[tuple[str, str]],
) -> None:
    """进程池并行解析 .py 文件（CPU 密集 ``ast.parse``）。

    ``chunksize`` 按 CPU 核心数与文件数自适应，减少 IPC 调度开销。
    文件经 :func:`_interleave_by_size` 重排，使每个 chunk 含大小文件混合，
    避免大文件扎堆（iter-134）。

    ``ProcessPoolExecutor`` 用 ``initializer=_init_parse_worker`` 在 worker
    启动时预加载 :data:`_STDLIB` 到 worker 全局，worker 内分离标准库导入，
    减少主进程分类循环（iter-134）。

    **超时防护**（iter-127）：``as_completed(timeout=)`` 设整体超时
    :data:`_PARSE_TOTAL_TIMEOUT`（300s）。超时抛 ``TimeoutError``，
    已处理的结果保留（依赖分析可能不完整但不会无限阻塞），warning 提示用户。
    超时不回退串行（若 ast.parse 真卡死，串行同样会卡死）。

    **单 worker 卡死不阻塞**（iter-138）：改用 ``submit`` + ``as_completed``
    替代 ``map(timeout=)``。``map`` 按提交顺序迭代结果，首个 worker 卡死时
    即使后续 worker 已完成也无法获取结果；``as_completed`` 按完成顺序 yield，
    卡死的 worker 不影响其他已完成 worker 的结果聚合。超时后未完成的 future
    被 cancel（已运行的无法取消，``with`` 块退出时 ``shutdown`` 等待）。
    """
    cpu_count = os.cpu_count() or 4
    interleaved = _interleave_by_size(py_files, cpu_count * 4)
    with ProcessPoolExecutor(
        max_workers=cpu_count,
        initializer=_init_parse_worker,
        initargs=(_STDLIB,),
    ) as pool:
        futures = [pool.submit(_parse_file_worker, str(p)) for p in interleaved]
        completed = 0
        try:
            for future in as_completed(futures, timeout=_PARSE_TOTAL_TIMEOUT):
                non_stdlib_tops, stdlib_tops, subs, errors = future.result()
                all_imports.extend(non_stdlib_tops)
                all_stdlib.extend(stdlib_tops)
                for pkg, sub_set in subs.items():
                    all_submodules.setdefault(pkg, set()).update(sub_set)
                all_errors.extend(errors)
                completed += 1
        except FuturesTimeoutError:
            pending = len(futures) - completed
            _logger.warning(
                "AST 并行解析超时（%ds），%d/%d 个文件未完成，依赖分析可能不完整",
                int(_PARSE_TOTAL_TIMEOUT),
                pending,
                len(py_files),
            )
            # 取消未完成的 future（已运行的无法取消，避免新任务启动）
            for f in futures:
                if not f.done():
                    f.cancel()
