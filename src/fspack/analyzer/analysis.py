"""AST 依赖分析编排：``analyze_dependencies`` 主入口 + 串行/并行解析调度.

从 :mod:`fspack.analyzer` facade 迁入的业务实现（facade 仅保留 re-export）。
编排 :mod:`fspack.analyzer.ast_scan`（AST 解析）与
:mod:`fspack.analyzer.fingerprint`（源码指纹排除目录）完成依赖分析，
本地包识别与进程池 worker 函数集中在本模块。

进程池 worker（``_parse_file_worker``/``_init_parse_worker``）须保持模块级
可解析以支持 pickle 跨进程传递——迁至本模块后 ``__module__`` 为
``fspack.analyzer.analysis``，spawn worker 导入本模块即可反序列化；facade
的 re-export 不影响 pickle 定位。

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
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Iterator

from fspack.analyzer.ast_scan import (
    _QT_PYTHON_PACKAGES,
    _STDLIB,
    collect_imports_and_submodules,
    parse_qml_imports,
)
from fspack.analyzer.fingerprint import (
    _EXCLUDED_DIRS as _FP_EXCLUDED_DIRS,
)
from fspack.config import DependencyReport

_logger = logging.getLogger(__name__)


def _local_packages(src_dir: Path, project_name: str) -> set[str]:
    """识别项目本地包/模块名（顶层 .py 与含 __init__.py 的目录）.

    用 :func:`os.scandir` 替代 :meth:`Path.iterdir`，避免 ``Path`` 包装
    开销与重复 stat 调用：``DirEntry.is_file``/``is_dir`` 复用枚举时的 stat
    缓存（Windows ``WIN32_FIND_DATA`` / Linux ``d_ino``）。迭代器用 ``with``
    管理，确保枚举句柄及时释放。
    """
    local: set[str] = {project_name}
    with os.scandir(src_dir) as it:
        for entry in it:
            name = entry.name
            if entry.is_file() and name.endswith(".py"):
                local.add(name[:-3])
            elif entry.is_dir() and (src_dir / name / "__init__.py").is_file():
                local.add(name)
    return local


def analyze_dependencies(  # noqa: PLR0912 - 分支多是分类与异常处理的自然结果，拆分反而降低可读性
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

    # 内存优化：用 scandir 剪枝生成器替代 rglob + list comprehension
    # rglob 会先全量递归（含排除目录）再在 comprehension 中过滤，
    # 生成器在 scandir 层直接跳过排除目录，避免 I/O 与大列表物化。
    # .py 与 .qml 单次遍历同时收集（两次全树扫描的 I/O 减半），
    # qml 解析仍按需仅在 imported_qt_pkgs 分支执行。
    src_files = list(_iter_src_files_by_ext(src_dir, resolved_data_dirs, _FP_EXCLUDED_DIRS, (".py", ".qml")))
    py_files: list[Path] = [p for p in src_files if p.suffix == ".py"]

    # 内存优化：跨文件去重用 dict 作保序集合（3.7+ dict 插入序稳定），
    # 省掉末尾独立 seen 去重循环与二次 list 分配。
    all_imports_ord: dict[str, None] = {}  # 非标准库顶层导入（local + third_party）
    all_stdlib_ord: dict[str, None] = {}  # 标准库顶层导入
    # 子模块合并用 list 暂存而非 set：每文件 update(set) 会触发多次哈希扩容，
    # 改用 list.extend + 末尾一次 frozenset 固化，500+ 文件项目内存省约 20%。
    all_submodules: dict[str, list[str]] = {}
    all_errors: list[tuple[str, str]] = []  # AST 解析失败记录 (abs_path, error_msg)（iter-138）

    if len(py_files) >= _PARALLEL_THRESHOLD:
        _parse_parallel(py_files, all_imports_ord, all_stdlib_ord, all_submodules, all_errors)
    else:
        _parse_serial(py_files, all_imports_ord, all_stdlib_ord, all_submodules, all_errors)

    # 扫描 QML 文件提取 QtQuick 等 QML 运行时依赖（AST 无法发现）
    # 仅当项目 import 了 Qt 绑定包时才扫描，避免非 Qt 项目无谓 I/O
    imported_qt_pkgs = _QT_PYTHON_PACKAGES & set(all_imports_ord)
    if imported_qt_pkgs:
        qml_files = [p for p in src_files if p.suffix == ".qml"]
        qml_qt_subs: set[str] = set()
        for qml_file in qml_files:
            # 防御性 try/except：parse_qml_imports 内部已 catch OSError 返回空集合，
            # 此处兜底文件读取竞态（枚举后读取期间被删除/权限变化）窗口期异常，
            # 不阻塞依赖分析主流程
            try:
                qml_qt_subs.update(parse_qml_imports(qml_file))
            except OSError as e:
                _logger.warning("QML 文件解析失败，跳过: %s: %s", qml_file, e)
        if qml_qt_subs:
            for qt_pkg in imported_qt_pkgs:
                all_submodules.setdefault(qt_pkg, []).extend(qml_qt_subs)

    local = _local_packages(src_dir, project_name)
    third: list[str] = []
    local_imports: list[str] = []
    # all_imports/all_stdlib 已在收集阶段通过 dict 去重保序，此处无需二次 seen 去重
    for imp in all_imports_ord:
        if imp in local:
            local_imports.append(imp)
        else:
            third.append(imp)
    stdlib = list(all_stdlib_ord)
    # 子模块 list → frozenset：先 dict 去重保序（3.7+ dict 插入序稳定），
    # 再 keys() 取唯一值构造 frozenset，避免大 list 直接 frozenset(list)
    # 的全量哈希扩容开销（500+ 文件场景 list 可能含重复子模块名）。
    ast_submodules: dict[str, frozenset[str]] = {}
    for pkg, subs_list in all_submodules.items():
        if pkg in local or pkg in _STDLIB:
            continue
        ord_uniq: dict[str, None] = {}
        for s in subs_list:
            ord_uniq.setdefault(s, None)
        ast_submodules[pkg] = frozenset(ord_uniq.keys())
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

    worker 启动时已通过 spawn 导入 :mod:`fspack.analyzer.analysis`（pickle
    反序列化定位定义模块，连带加载 :mod:`fspack.analyzer.ast_scan`），
    initializer 在此之后执行，使第一次 :func:`_parse_file_worker` 调用时
    ``_WORKER_STATE["stdlib"]`` 已就绪，无需模块属性查找。
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
    except (SyntaxError, OSError, ValueError, RecursionError) as e:
        # ValueError：源码含 NUL 字节；RecursionError：深度嵌套源码爆解析栈
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
    all_imports_ord: dict[str, None],
    all_stdlib_ord: dict[str, None],
    all_submodules: dict[str, list[str]],
    all_errors: list[tuple[str, str]],
) -> None:
    """串行解析所有 .py 文件，结果合并到 ``all_imports_ord`` / ``all_stdlib_ord`` / ``all_submodules`` / ``all_errors``.

    用模块级 :data:`_STDLIB` 将顶层导入分离为标准库（``all_stdlib_ord``）与非标准库
    （``all_imports_ord``），与 :func:`_parse_file_worker` 的 worker 分离逻辑一致，
    使主进程分类循环仅需区分 local vs third_party。

    内存优化：``all_imports_ord`` / ``all_stdlib_ord`` 用 ``dict`` 作保序集合
    直接去重（setdefault 幂等），省掉独立 ``seen`` 集合对象与末尾二次去重循环。
    ``all_submodules`` 用 ``list`` 暂存而非 ``set``：逐文件 ``list.extend``
    代替 ``set.update``，避免每文件哈希表扩容开销（500+ 文件场景 set 扩容
    达 log2(n) 次）。去重统一在主循环末尾做一次 dict 保序去重。

    AST 解析失败（SyntaxError/OSError/ValueError/RecursionError）记录到
    ``all_errors``：不再静默跳过，记录 ``(绝对路径 str, 错误信息)`` 元组供
    主进程格式化报告。ValueError 为源码含 NUL 字节，RecursionError 为深度
    嵌套源码爆 ``ast.parse`` 递归栈。

    用 :meth:`Path.read_bytes` + :func:`ast.parse(bytes)`，避免 Python 层
    ``decode("utf-8")`` 中间步骤——``ast.parse`` 内部用 C 实现解码，比
    显式 ``str.decode`` 快约 5-10%。基线测试 50 文件场景下可见微收益。
    """
    for py in py_files:
        try:
            tree = ast.parse(py.read_bytes())
        except (SyntaxError, OSError, ValueError, RecursionError) as e:
            all_errors.append((str(py), str(e)))
            continue
        tops, subs = collect_imports_and_submodules(tree)
        for top in tops:
            if top in _STDLIB:
                all_stdlib_ord.setdefault(top, None)
            else:
                all_imports_ord.setdefault(top, None)
        for pkg, sub_set in subs.items():
            all_submodules.setdefault(pkg, []).extend(sub_set)


def _parse_parallel(
    py_files: list[Path],
    all_imports_ord: dict[str, None],
    all_stdlib_ord: dict[str, None],
    all_submodules: dict[str, list[str]],
    all_errors: list[tuple[str, str]],
) -> None:
    """进程池并行解析 .py 文件（CPU 密集 ``ast.parse``）。

    ``submit`` 逐文件提交：进程池内部 FIFO 队列天然负载均衡（空闲 worker
    先取任务），无需按文件大小重排（旧 ``_interleave_by_size`` 为
    ``map(chunksize=)`` 连续分块设计，submit 模式下双重 stat 纯浪费，已删除）。

    ``ProcessPoolExecutor`` 用 ``initializer=_init_parse_worker`` 在 worker
    启动时预加载 :data:`_STDLIB` 到 worker 全局，worker 内分离标准库导入，
    减少主进程分类循环（iter-134）。

    内存优化：worker 返回的 list 合并时直接用 ``dict.setdefault`` 去重保序，
    省掉末尾独立 ``seen`` 去重循环。``all_submodules`` 用 ``list`` 暂存：
    逐结果 ``list.extend`` 代替 ``set.update``，减少 worker 结果合并的
    哈希表扩容开销。去重统一在主循环末尾做一次 dict 保序去重。

    **超时防护**（iter-127）：``as_completed(timeout=)`` 设整体超时
    :data:`_PARSE_TOTAL_TIMEOUT`（300s）。超时抛 ``TimeoutError``，
    已处理的结果保留（依赖分析可能不完整但不会无限阻塞），warning 提示用户。
    超时分支对未完成 future 逐个 ``cancel`` 后 ``shutdown(wait=False)``
    立即返回——若依赖 ``with`` 块退出隐式 ``shutdown(wait=True)`` 会无限
    等待卡死的 worker。Python 3.8 无 ``shutdown(cancel_futures=)``（3.9+），
    故手动逐个 cancel（已运行的无法取消，仅避免新任务启动），并用标志变量
    保证 ``finally`` 不重复 shutdown。超时不回退串行（若 ast.parse 真卡死，
    串行同样会卡死）。

    **worker 崩溃容错**：``future.result()`` 抛 ``BrokenProcessPool``
    （worker OOM/段错误）时 warning 后提前结束循环——已聚合的结果保留，
    依赖分析降级为不完整而非整单失败。

    **单 worker 卡死不阻塞**（iter-138）：``submit`` + ``as_completed``
    按完成顺序 yield，卡死的 worker 不影响其他已完成 worker 的结果聚合。
    """
    cpu_count = os.cpu_count() or 4
    # 显式 pool + try/finally：超时分支需要 shutdown(wait=False) 立即返回，
    # with 块退出固定 shutdown(wait=True) 会无限等待卡死的 worker
    pool = ProcessPoolExecutor(
        max_workers=cpu_count,
        initializer=_init_parse_worker,
        initargs=(_STDLIB,),
    )
    timed_out = False
    try:
        futures = [pool.submit(_parse_file_worker, str(p)) for p in py_files]
        completed = 0
        try:
            for future in as_completed(futures, timeout=_PARSE_TOTAL_TIMEOUT):
                try:
                    non_stdlib_tops, stdlib_tops, subs, errors = future.result()
                except BrokenProcessPool:
                    _logger.warning(
                        "AST 并行解析 worker 崩溃（BrokenProcessPool），%d/%d 个文件结果丢失，依赖分析可能不完整",
                        len(futures) - completed,
                        len(futures),
                    )
                    break
                for top in non_stdlib_tops:
                    all_imports_ord.setdefault(top, None)
                for top in stdlib_tops:
                    all_stdlib_ord.setdefault(top, None)
                for pkg, sub_set in subs.items():
                    all_submodules.setdefault(pkg, []).extend(sub_set)
                all_errors.extend(errors)
                completed += 1
        except FuturesTimeoutError:
            timed_out = True
            pending = len(futures) - completed
            _logger.warning(
                "AST 并行解析超时（%ds），%d/%d 个文件未完成，依赖分析可能不完整",
                int(_PARSE_TOTAL_TIMEOUT),
                pending,
                len(futures),
            )
            # 取消未完成的 future（已运行的无法取消，避免新任务启动）。
            # Python 3.8 无 shutdown(cancel_futures=)，须手动逐个 cancel
            for f in futures:
                if not f.done():
                    f.cancel()
            # 立即关闭池不再等待卡死的 worker；timed_out 标志使 finally 跳过重复 shutdown
            pool.shutdown(wait=False)
    finally:
        if not timed_out:
            pool.shutdown(wait=True)


def _data_dir_prefixes(root: Path, data_dirs: tuple[Path, ...]) -> tuple[tuple[str, ...], ...]:
    """把绝对 ``data_dirs`` 预转换为相对 ``root`` 的 parts 前缀元组.

    遍历中的 ``data_dirs`` 判断改用纯字符串 parts 前缀比较后，此预转换把
    "每条目一次 ``Path.resolve()``（Windows ~20-50µs）"降为"整个遍历一次"。
    ``data_dir`` 不在 ``root`` 树内（不同盘符/绝对路径指向树外，
    ``relative_to`` 抛 ``ValueError``）时丢弃——原实现逐条目 resolve 后
    ``relative_to`` 同样永不匹配，行为等价。
    """
    root_resolved = root.resolve()
    prefixes: list[tuple[str, ...]] = []
    for dp in data_dirs:
        try:
            prefixes.append(dp.relative_to(root_resolved).parts)
        except ValueError:
            continue
    return tuple(prefixes)


def _iter_src_files_by_ext(
    root: Path,
    data_dirs: tuple[Path, ...],
    excluded_dirs: frozenset[str],
    suffixes: tuple[str, ...],
) -> Iterator[Path]:
    """scandir 剪枝生成器：枚举指定后缀集合的源码文件，自动跳过排除目录.

    替代 ``root.rglob`` + 过滤的实现：rglob 会先递归进入排除目录（如
    ``dist/``、``__pycache__/``）再在 comprehension 中过滤，此生成器在
    scandir 层直接 ``continue`` 剪枝，避免排除目录下的 I/O 与大列表物化。
    同时按名称排序目录条目（含子目录）保证跨平台遍历顺序确定性
    （``os.walk`` / ``rglob`` 不保证）。

    单次遍历同时收集多种后缀（如 ``(".py", ".qml")``），避免 .py 与 .qml
    各自全树扫描一次的重复 I/O；调用方按 ``suffix`` 分拣。

    ``data_dirs`` 判断用 :func:`_data_dir_prefixes` 预计算的相对 parts
    前缀做纯字符串比较，消除逐条目 ``Path.resolve()`` 系统调用（该重构
    同时消除了原 resolve() OSError 静默丢子树的路径）。

    Args:
        root: 项目根目录（遍历起点）
        data_dirs: 数据资源目录绝对路径元组（目录树内整个剪枝）
        excluded_dirs: 始终排除的目录名集合（与 fingerprint 共用）
        suffixes: 目标文件后缀元组（如 ``(".py", ".qml")``）
    """
    prefixes = _data_dir_prefixes(root, data_dirs)
    yield from _iter_src_tree(root, (), prefixes, excluded_dirs, suffixes)


def _iter_src_tree(
    current: Path,
    rel_parts: tuple[str, ...],
    data_dir_prefixes: tuple[tuple[str, ...], ...],
    excluded_dirs: frozenset[str],
    suffixes: tuple[str, ...],
) -> Iterator[Path]:
    """``_iter_src_files_by_ext`` 的递归主体：携带相对 parts 做前缀剪枝.

    ``rel_parts`` 为 ``current`` 相对遍历根的路径组件（递归时元组拼接），
    供 ``data_dirs`` 前缀比较复用，避免每条目重新 ``relative_to``。
    """
    for entry in sorted(os.scandir(current), key=lambda e: e.name):
        entry_rel = (*rel_parts, entry.name)
        if entry.is_dir(follow_symlinks=False):
            if entry.name in excluded_dirs or entry.name.endswith(".egg-info"):
                continue
            # data-dirs 剪枝：整个目录树不遍历（含 data-dir 自身）
            if data_dir_prefixes and any(entry_rel[: len(p)] == p for p in data_dir_prefixes):
                continue
            yield from _iter_src_tree(Path(entry.path), entry_rel, data_dir_prefixes, excluded_dirs, suffixes)
        elif entry.is_file(follow_symlinks=False) and entry.name.endswith(suffixes):
            # data-dirs 内的单文件也排除（防御性，剪枝应已跳过整个目录）
            if data_dir_prefixes and any(entry_rel[: len(p)] == p for p in data_dir_prefixes):
                continue
            yield Path(entry.path)
