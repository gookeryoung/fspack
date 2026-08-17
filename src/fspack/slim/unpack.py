"""精简打包解压实现：按需解压 wheel，剥离非必要文件。

从 :mod:`fspack.slim.base` 拆分而来，封装 wheel 解压与精简逻辑。依赖
:mod:`fspack.slim.spec` 的 ``SlimSpec`` 注册表做条目分类与子模块归一化。

核心流程：

1. ``slim_unpack`` 入口：合并 AST 收集的子模块使用信息与用户显式指定模块，
   应用各 spec 的依赖闭包扩展，并行或串行解压 wheel
2. ``_unpack_wheel_dispatch`` 分发：文件名可解析走精简解压，否则走全量解压
3. ``_slim_extract`` 按需解压：用户规则（include/exclude）优先级最高，
   其次 spec 自动分类，``exclude`` 类与未保留子模块文件跳过
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import Sequence

from fspack.config import DEFAULT_SLIM_RULES, SlimRules
from fspack.exceptions import DependencyError
from fspack.progress import StageRecorder, iter_with_progress, parallel_map_with_progress
from fspack.slim.spec import WheelInfo, get_spec, normalize_name

__all__ = ["slim_unpack"]

# 共享 logger 名：保持与原 fspack.slim.base 一致，测试 caplog 按 logger 名过滤
_logger = logging.getLogger("fspack.slim.base")


def _detect_top_pkg(zf: zipfile.ZipFile, whl_pkg: str) -> str | None:
    """从已打开的 ZipFile 中找出与 whl_pkg 关联的顶层目录名。

    优先返回 ``normalize_name(top) == whl_pkg`` 的目录（wheel 文件名与顶层目录一致
    的常规场景）。无匹配时回退到第一个能匹配某个**非兜底** spec 的顶层目录——
    用于支持拆分 wheel：PySide6 6.6+ 将包拆为 ``pyside6``/``pyside6_essentials``
    /``pyside6_addons`` 三个 wheel，后两者的文件名归一化包名分别为
    ``pyside6-essentials``/``pyside6-addons``，但 wheel 内顶层目录均为 ``PySide6``
    （归一化为 ``pyside6``）。回退匹配使 ``QtSlimSpec`` 识别这些拆分 wheel，
    共享 ``PySide6`` 的 keep_subs 与精简规则。

    无任何匹配时返回 None（调用方走全量解压）。
    """
    fallback: str | None = None
    # top 目录去重：同一 wheel 的全部条目共享顶层目录名，对每个 top 只判定一次，
    # 避免逐条目重复 normalize_name（正则替换）与 get_spec（注册表遍历）
    seen_tops: set[str] = set()
    for name in zf.namelist():
        top = name.split("/")[0]
        if top.endswith(".dist-info") or top in seen_tops:
            continue
        seen_tops.add(top)
        if normalize_name(top) == whl_pkg:
            return top
        # 回退：记录第一个能匹配非兜底 spec 的顶层目录
        # （避免误匹配 numpy.libs 等辅助目录，这些目录应走 DefaultSlimSpec 兜底）
        if fallback is None:
            spec = get_spec(normalize_name(top))
            if not spec.is_fallback:
                fallback = top
    return fallback


def _full_unpack(whl: Path, dest: Path) -> None:
    """全量解压单个 wheel 到目标目录."""
    try:
        with zipfile.ZipFile(whl) as zf:
            try:
                zf.extractall(dest)
            except FileExistsError:  # pragma: no cover - 并发竞争边缘场景
                # 并行解压目录创建竞争：逐文件安全提取
                for info in zf.infolist():
                    _safe_extract(zf, info, dest)
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e


def _safe_extract(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> None:
    """线程安全提取单个条目：捕获并行解压目录创建竞争（``FileExistsError``）。

    ``zipfile._extract_member`` 的 check-then-create 非原子：多线程并行解压
    共享目录树的 wheel（如 PySide6 拆分 wheel 均含 ``PySide6/`` 顶层目录）时，
    两线程同时检测到目录不存在并同时 ``makedirs``，第二个触发 ``FileExistsError``。
    重试一次即可——目录已被另一线程创建，``os.path.exists`` 返回 True 跳过 ``makedirs``。
    """
    try:
        zf.extract(info, dest)
    except FileExistsError:  # pragma: no cover - 并发竞争边缘场景
        zf.extract(info, dest)


def _slim_extract(
    zf: zipfile.ZipFile,
    dest: Path,
    top_pkg: str,
    keep_subs: set[str],
    slim_rules: SlimRules = DEFAULT_SLIM_RULES,
) -> int:
    """从已打开的 ZipFile 按需解压，剥离 ``exclude`` 类文件与未保留子模块文件。

    - 始终剥离 ``exclude`` 类条目（如 examples/docs/tests 子目录、Qt 开发工具 exe）
    - ``keep_subs`` 非空时按子模块选择性保留（``submodule`` 类仅保留 ``keep_subs`` 中）
    - ``keep_subs`` 为空时 ``submodule`` 类视作 ``shared`` 保留（等价于全量解压，
      但仍应用剥离规则）——用于源码仅 ``import <top_pkg>`` 顶层导入、AST 未
      收集到子模块使用信息的场景

    ``slim_rules``（用户自定义 glob 规则）优先级高于 spec 自动分类：

    - ``include`` 匹配的条目始终保留（覆盖 spec 的 ``exclude`` 判定）
    - ``exclude`` 匹配的条目始终剥离（覆盖 spec 的 ``shared``/``submodule`` 判定）
    - 二者均不匹配时走 spec 自动分类

    解压完成后输出精简统计日志（剥离文件数、节省字节数），便于用户评估精简效果。
    返回剥离文件累计字节数，供调用方（:func:`slim_unpack`）回填到
    :class:`StageRecorder` 的"节省"列，在汇总表中直观体现精简价值。
    """
    spec = get_spec(normalize_name(top_pkg))
    skipped_files = 0
    skipped_bytes = 0
    total_bytes = 0
    for info in zf.infolist():
        total_bytes += info.file_size
        # 用户规则优先级最高：include > exclude > spec 自动分类
        if slim_rules.matches_include(info.filename):
            _safe_extract(zf, info, dest)
            continue
        if slim_rules.matches_exclude(info.filename):
            if not info.is_dir():
                skipped_files += 1
                skipped_bytes += info.file_size
            continue
        category, sub = spec.classify_entry(info.filename, top_pkg, keep_subs)
        if category == "exclude":
            # 剥离文件与剥离目录的目录条目均跳过，避免遗留空目录
            if not info.is_dir():
                skipped_files += 1
                skipped_bytes += info.file_size
            continue
        if info.is_dir():
            _safe_extract(zf, info, dest)
            continue
        # keep_subs 为空时不应用子模块选择性剥离（全量保留 .pyd 等）
        if category == "submodule" and keep_subs and sub not in keep_subs:
            skipped_files += 1
            skipped_bytes += info.file_size
            continue
        _safe_extract(zf, info, dest)
    if skipped_files:
        assert zf.filename is not None  # 从文件路径打开的 ZipFile 必有 filename
        saved_mb = skipped_bytes / 1024 / 1024
        total_mb = total_bytes / 1024 / 1024
        pct = (skipped_bytes / total_bytes * 100) if total_bytes else 0
        _logger.info(
            "精简 %s: 剥离 %d 个文件，节省 %.1fMB / %.1fMB (%.0f%%)",
            Path(zf.filename).name,
            skipped_files,
            saved_mb,
            total_mb,
            pct,
        )
    return skipped_bytes


def _unpack_one_wheel(
    whl: Path,
    dest: Path,
    whl_pkg: str,
    merged: dict[str, set[str]],
    slim_rules: SlimRules = DEFAULT_SLIM_RULES,
) -> int:
    """解压单个可解析文件名的 wheel：检测 top_pkg 后选择全量或精简解压。

    单次 ``zipfile.ZipFile`` 打开同时完成 top_pkg 检测与解压，避免重复打开。
    坏 zip 抛 :class:`DependencyError`。

    ``keep_subs`` 通过 ``normalize_name(top_pkg)`` 从 ``merged`` 查找（而非用
    ``whl_pkg``），使拆分 wheel（如 ``pyside6_essentials``）能共享主包
    ``PySide6`` 的保留集合——详见 :func:`_detect_top_pkg` 回退匹配逻辑。

    ``slim_rules`` 透传给 :func:`_slim_extract`，优先级高于 spec 自动分类。

    返回精简剥离的字节数（全量解压分支返回 0），供 :func:`slim_unpack` 累加到
    :class:`StageRecorder` 的"节省"列。
    """
    try:
        with zipfile.ZipFile(whl) as zf:
            top_pkg = _detect_top_pkg(zf, whl_pkg)
            if top_pkg is None:
                # wheel 顶层目录与归一化包名不匹配 → 兜底全量解压
                # （_detect_top_pkg 回退匹配已处理拆分 wheel 场景，此分支仅在
                # wheel 结构异常时触发，用户规则无法应用）
                zf.extractall(dest)
                return 0
            # 用 top_pkg 的归一化名查找 keep_subs，支持拆分 wheel 共享主包保留集合
            keep_subs = merged.get(normalize_name(top_pkg), set())
            if keep_subs:
                _logger.info("精简解压 %s: 保留子模块 %s", whl.name, ", ".join(sorted(keep_subs)))
            else:
                # keep_subs 为空：仅应用剥离规则（examples/docs/tests 等），子模块文件全保留
                _logger.info("解压 %s（应用剥离规则）", whl.name)
            return _slim_extract(zf, dest, top_pkg, keep_subs, slim_rules)
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e


# 并行解压阈值：低于此数走串行（避免线程池启动开销）
# I/O 密集 zipfile 解压，GIL 在 I/O 等待时释放，2+ wheel 即有并行收益
_PARALLEL_WHEEL_THRESHOLD = 2


def _unpack_wheel_dispatch(
    whl: Path,
    dest: Path,
    merged: dict[str, set[str]],
    slim_rules: SlimRules,
) -> int:
    """分发单个 wheel 解压：文件名可解析走精简，否则走全量。

    返回精简节省字节数（全量解压返回 0）。模块级函数确保可在线程池中执行
    （线程安全：``dest`` 各 wheel 写入不同文件无冲突，``merged``/``slim_rules``
    只读，``zipfile`` 各 wheel 独立打开）。
    """
    info = WheelInfo.from_filename(whl.name)
    if info is None:
        _full_unpack(whl, dest)
        return 0
    whl_pkg = normalize_name(info.name)
    return _unpack_one_wheel(whl, dest, whl_pkg, merged, slim_rules)


def slim_unpack(  # noqa: PLR0913
    wheels: Sequence[Path],
    site_packages_dir: Path,
    submodule_usage: dict[str, frozenset[str]] | None = None,
    keep_modules: set[str] | None = None,
    *,
    slim_rules: SlimRules = DEFAULT_SLIM_RULES,
    stage: StageRecorder | None = None,
) -> int:
    """按子模块 import 分析选择性解压给定 wheel 列表（白名单制）。

    - 合并 ``submodule_usage``（AST 收集）与 ``keep_modules``（用户显式指定）
      构建每个包的保留集合；按包对应的 ``SlimSpec`` 归一化子模块名
      （Qt 库为 ``QtCore`` → ``Core``）
    - 各 ``SlimSpec`` 的 :meth:`SlimSpec.expand_closure` 自动扩展依赖闭包
      （如 Qt 库 ``import QtWidgets`` 自动加入 ``Gui``/``Core``），闭包内的
      子模块对应的 ``.pyd`` 与 ``Qt5/6*.dll`` 均保留
    - 有保留集合的 wheel 按需解压，无保留集合的 wheel 全量解压
      （向后兼容：纯顶层 import 或无子模块分析时）
    - 返回解包 wheel 数量

    ``slim_rules``：用户自定义 glob 规则（:class:`fspack.config.SlimRules`），
    优先级高于 spec 自动分类。``include`` 强制保留（覆盖 spec 剥离），
    ``exclude`` 强制剥离（覆盖 spec 保留）。匹配 wheel 内 POSIX 相对路径。

    ``stage`` 用于通过进度显示函数回写处理项数与节省字节数到 BuildTracker。

    2+ wheel 启用 :func:`parallel_map_with_progress` 并行解压（I/O 密集 zipfile
    解压，GIL 在 I/O 等待时释放）。PySide6 拆分 wheel（3 wheel）等场景显著提速。
    """
    site_packages_dir.mkdir(parents=True, exist_ok=True)

    merged: dict[str, set[str]] = {}
    if submodule_usage:
        for pkg, subs in submodule_usage.items():
            pkg_norm = normalize_name(pkg)
            spec = get_spec(pkg_norm)
            merged[pkg_norm] = {spec.normalize_submodule(s) for s in subs}
    if keep_modules:
        for spec_str in keep_modules:
            if "." not in spec_str:
                continue
            pkg, sub = spec_str.split(".", 1)
            pkg_norm = normalize_name(pkg)
            spec = get_spec(pkg_norm)
            norm_sub = spec.normalize_submodule(sub)
            merged.setdefault(pkg_norm, set()).add(norm_sub)

    # 应用各 spec 的依赖闭包扩展（如 Qt 模块依赖映射）
    for pkg, subs in merged.items():
        spec = get_spec(pkg)
        subs.update(spec.expand_closure(subs))

    sorted_wheels = sorted(wheels)
    count = 0
    total_saved = 0

    if len(sorted_wheels) >= _PARALLEL_WHEEL_THRESHOLD:
        # 多 wheel 并行解压（I/O 密集 zipfile 解压，GIL 在 I/O 等待时释放）
        def _unpack_one(whl: Path) -> int:
            """线程池 worker：分发单个 wheel 解压."""
            return _unpack_wheel_dispatch(whl, site_packages_dir, merged, slim_rules)

        saved_list = parallel_map_with_progress(sorted_wheels, _unpack_one, "解压 wheel", stage=stage)
        total_saved = sum(saved_list)
        count = len(sorted_wheels)
    else:
        # 单/零 wheel 串行（避免线程池启动开销）
        for whl in iter_with_progress(sorted_wheels, "解压 wheel", stage=stage):
            total_saved += _unpack_wheel_dispatch(whl, site_packages_dir, merged, slim_rules)
            count += 1

    if stage is not None and count:
        stage.set_detail(f"{count} wheels 解压")
        if total_saved:
            stage.add_saved_bytes(total_saved)
    return count
