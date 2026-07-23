"""精简打包核心：抽象基类与按需解压编排。

参考 fspacker ``packers.libspec`` 设计：按包名分发到对应的 ``SlimSpec``
子类。每个子类描述一组包（如 Qt 库、普通库）的精简规则——子模块归一化、
依赖闭包扩展、wheel 条目分类。

新增包精简规则时只需：

1. 继承 ``SlimSpec``，实现 ``match``/``classify_entry``/``normalize_submodule``/
   ``expand_closure``
2. 用 ``@register_spec`` 注册（``DefaultSlimSpec`` 兜底，必须最后注册）

无需修改 ``slim_unpack`` 与 ``classify_entry`` 的分发逻辑。
"""

from __future__ import annotations

import abc
import logging
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from fspack.exceptions import DependencyError
from fspack.progress import StageRecorder, iter_with_progress

if sys.version_info >= (3, 12):  # pragma: no cover
    from typing import override
else:
    from typing_extensions import override  # type: ignore[import-not-found]

__all__ = [
    "SlimSpec",
    "WheelInfo",
    "classify_entry",
    "get_spec",
    "normalize_name",
    "override",
    "parse_wheel_filename",
    "register_spec",
    "slim_unpack",
]

_logger = logging.getLogger(__name__)

# PEP 427 wheel 文件名正则：name-version(-build)?-py-abi-plat.whl
_WHEEL_RE = re.compile(
    r"^(?P<name>.+?)-(?P<ver>.+?)(-(?P<build>\d[^-]*?))?-"
    r"(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WheelInfo:
    """解析后的 wheel 元信息."""

    name: str
    version: str
    python_tags: tuple[str, ...]
    abi_tag: str
    platform_tags: tuple[str, ...]

    @classmethod
    def from_filename(cls, filename: str) -> WheelInfo | None:
        """从 wheel 文件名构造实例，无法解析返回 None."""
        m = _WHEEL_RE.match(filename)
        if m is None:
            return None
        return cls(
            name=m.group("name"),
            version=m.group("ver"),
            python_tags=tuple(m.group("py").split(".")),
            abi_tag=m.group("abi"),
            platform_tags=tuple(m.group("plat").split(".")),
        )


def normalize_name(name: str) -> str:
    """PEP 503 名称归一化：小写，连续的 ``-_.`` 合并为 ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_wheel_filename(filename: str) -> WheelInfo | None:
    """解析 wheel 文件名为 WheelInfo，无法解析返回 None。

    .. deprecated:: 向后兼容包装，内部委托 :meth:`WheelInfo.from_filename`。
    """
    return WheelInfo.from_filename(filename)


class SlimSpec(abc.ABC):
    """精简规则基类：定义如何分类 wheel 条目。

    每个具体子类描述一组包（如 Qt 库、普通库）的精简规则。子类通过
    :meth:`match` 声明匹配的包名集合，:meth:`classify_entry` 实现条目分类逻辑，
    :meth:`expand_closure` 计算子模块依赖闭包，:meth:`normalize_submodule`
    归一化子模块名。
    """

    # 子模块扩展名：仅这些文件按子模块名选择性保留
    SUBMODULE_EXTS: frozenset[str] = frozenset({".pyd", ".pyi", ".so"})

    # 通用剥离子目录：示例代码、文档、测试代码非运行时必需，所有 spec 共享
    COMMON_EXCLUDE_SUBDIRS: frozenset[str] = frozenset(
        {
            "examples",  # 示例代码
            "docs",  # 文档（多数包用复数）
            "doc",  # 文档（少数包用单数）
            "tests",  # 测试代码（多数包用复数）
            "test",  # 测试代码（少数包用单数）
            "testing",  # 测试辅助目录
        }
    )

    @classmethod
    @abc.abstractmethod
    def match(cls, whl_pkg: str) -> bool:
        """是否匹配此精简规则（按 wheel 归一化包名判断）。

        ``whl_pkg`` 为已归一化的包名（小写，``-``/``_``/``.`` 合并）。
        兜底规则的 :meth:`match` 应始终返回 ``True``。
        """

    @classmethod
    @abc.abstractmethod
    def normalize_submodule(cls, sub: str) -> str:
        """归一化子模块名（如 Qt 库统一 ``QtCore``/``Qt5Core`` → ``Core``）。

        无归一化需求的库原样返回 ``sub``。
        """

    @classmethod
    @abc.abstractmethod
    def expand_closure(cls, subs: set[str]) -> set[str]:
        """计算子模块集合的传递依赖闭包。

        返回的集合应包含输入子模块及其所有传递依赖。无依赖映射的子类
        直接返回输入集合的副本（不就地修改 ``subs``）。
        """

    @classmethod
    @abc.abstractmethod
    def classify_entry(cls, entry: str, top_pkg: str, keep_subs: set[str]) -> tuple[str, str | None]:
        """分类 wheel 条目归属。

        返回 ``(类别, 子模块名|None)``，类别为 ``"metadata"``/``"exclude"``/
        ``"shared"``/``"submodule"``：

        - ``metadata``: ``*.dist-info/**`` 元数据，始终保留
        - ``exclude``: 可安全剥离的非必要文件，始终跳过
        - ``shared``: 包级共享文件，始终保留
        - ``submodule``: 子模块专属文件，仅当子模块在 ``keep_subs`` 中时保留
        """

    # ---- 通用分类辅助（供子类复用）----

    @classmethod
    def _classify_top_or_meta(cls, entry: str, top_pkg: str) -> tuple[str, str | None] | None:
        """通用 metadata 与跨包 shared 分类。

        返回 ``None`` 表示不属于这两类，需交由具体规则继续分类。
        """
        parts = entry.split("/")
        if parts[0].endswith(".dist-info"):
            return ("metadata", None)
        if parts[0] != top_pkg:
            return ("shared", None)
        return None

    @classmethod
    def _default_classify(  # noqa: PLR0911, PLR0913
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],  # noqa: ARG003
        extra_excludes: frozenset[str] = frozenset(),
        nested_excludes: frozenset[str] = frozenset(),
        top_ext_always_shared: bool = False,
    ) -> tuple[str, str | None]:
        """默认分类逻辑（供 ``DefaultSlimSpec`` 与简单 spec 复用）。

        - ``*.dist-info/**`` → metadata
        - 嵌套剥离：``nested_excludes`` 中目录名出现在任意层级（含跨包）
          → exclude（用于 ``scipy/<sub>/tests/``、``mpl_toolkits/tests/``）
        - 跨包文件 → shared
        - ``__init__.*``/``_*`` → shared
        - 顶层 ``.pyd``/``.pyi``/``.so`` → submodule（按原文件名 stem）；
          ``top_ext_always_shared=True`` 时改归 shared（用于顶层 C 扩展是
          ``__init__`` 硬依赖的库，如 matplotlib 的 ``ft2font.pyd``）
        - 其他文件 → shared
        - 子目录在 ``COMMON_EXCLUDE_SUBDIRS`` 或 ``extra_excludes`` 中 → exclude
        - 其他子目录 → shared

        ``extra_excludes`` 用于库专属二级剥离目录（如 numpy 的 f2py/distutils、
        lxml 的 includes）；``nested_excludes`` 用于任意层级剥离（含跨包，
        如 scipy 各子模块下的 tests、matplotlib 跨包 mpl_toolkits 下的 tests）；
        ``top_ext_always_shared`` 用于顶层 C 扩展不可选择性剥离的库（matplotlib
        的 ``ft2font`` 是 ``__init__._check_versions()`` 硬依赖，剥离即 ImportError）。
        Qt 等复杂 spec 不用此方法。
        """
        parts = entry.split("/")
        if parts[0].endswith(".dist-info"):
            return ("metadata", None)

        # 嵌套剥离：任意层级（含跨包）匹配则剥离
        # 用于 scipy/<sub>/tests/、mpl_toolkits/<sub>/tests/ 等嵌套测试目录
        if nested_excludes:
            for part in parts[1:]:
                if part in nested_excludes:
                    return ("exclude", None)

        if parts[0] != top_pkg:
            return ("shared", None)

        # 顶层文件（parts == 2）
        if len(parts) == 2:
            filename = parts[1]
            if filename.startswith("__init__.") or filename.startswith("_"):
                return ("shared", None)
            suffix = Path(filename).suffix.lower()
            stem = Path(filename).stem
            if suffix in cls.SUBMODULE_EXTS:
                if top_ext_always_shared:
                    # 顶层 C 扩展是 __init__ 硬依赖，始终保留（如 matplotlib ft2font）
                    return ("shared", None)
                # 非归一化，按原文件名归类
                return ("submodule", stem)
            return ("shared", None)

        # 子目录（len(parts) >= 3）：通用剥离 + 库专属剥离
        if parts[1] in cls.COMMON_EXCLUDE_SUBDIRS or parts[1] in extra_excludes:
            return ("exclude", None)
        return ("shared", None)


# 注册表：按注册顺序匹配，首个命中的 spec 类生效。DefaultSlimSpec 必须最后注册。
_SPECS: list[type[SlimSpec]] = []


def register_spec(spec: type[SlimSpec]) -> type[SlimSpec]:
    """注册精简规则类。装饰器形式，按注册顺序匹配。

    ``DefaultSlimSpec`` 应最后注册（兜底，``match`` 始终返回 ``True``）。
    """
    _SPECS.append(spec)
    return spec


def get_spec(whl_pkg: str) -> type[SlimSpec]:
    """按归一化包名匹配返回对应的精简规则类。

    遍历注册表，首个 :meth:`SlimSpec.match` 命中的 spec 类返回。无匹配时
    返回最后注册的 ``DefaultSlimSpec``（兜底）。
    """
    for spec in _SPECS:
        if spec.match(whl_pkg):
            return spec
    # 兜底：不应到达（DefaultSlimSpec.match 始终 True），仅做防御
    return _SPECS[-1]  # pragma: no cover


def classify_entry(
    entry: str,
    top_pkg: str,
    keep_subs: set[str] | None = None,
) -> tuple[str, str | None]:
    """分类 wheel 条目归属（按 ``top_pkg`` 自动选择精简规则）。

    入口函数：根据归一化包名分发到注册的 ``SlimSpec`` 子类。具体规则见
    各子类的 :meth:`SlimSpec.classify_entry` 文档。
    """
    spec = get_spec(normalize_name(top_pkg))
    return spec.classify_entry(entry, top_pkg, keep_subs or set())


# ---- 解压实现 ----


def _detect_top_pkg(whl: Path, whl_pkg: str) -> str | None:
    """从 wheel 条目中找出与 whl_pkg 归一化名匹配的顶层目录名。

    遍历 wheel 条目，返回第一个 ``normalize_name`` 后等于 ``whl_pkg`` 的目录名。
    无匹配时返回 None（调用方走全量解压）。
    """
    try:
        with zipfile.ZipFile(whl) as zf:
            for name in zf.namelist():
                top = name.split("/")[0]
                if not top.endswith(".dist-info") and normalize_name(top) == whl_pkg:
                    return top
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e
    return None


def _full_unpack(whl: Path, dest: Path) -> None:
    """全量解压单个 wheel 到目标目录."""
    try:
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(dest)
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e


def _slim_extract(whl: Path, dest: Path, top_pkg: str, keep_subs: set[str]) -> None:
    """按需解压 wheel，剥离 ``exclude`` 类文件与未保留子模块文件。

    - 始终剥离 ``exclude`` 类条目（如 examples/docs/tests 子目录、Qt 开发工具 exe）
    - ``keep_subs`` 非空时按子模块选择性保留（``submodule`` 类仅保留 ``keep_subs`` 中）
    - ``keep_subs`` 为空时 ``submodule`` 类视作 ``shared`` 保留（等价于全量解压，
      但仍应用剥离规则）——用于源码仅 ``import <top_pkg>`` 顶层导入、AST 未
      收集到子模块使用信息的场景
    """
    spec = get_spec(normalize_name(top_pkg))
    skipped = 0
    try:
        with zipfile.ZipFile(whl) as zf:
            for info in zf.infolist():
                category, sub = spec.classify_entry(info.filename, top_pkg, keep_subs)
                if category == "exclude":
                    # 剥离文件与剥离目录的目录条目均跳过，避免遗留空目录
                    continue
                if info.is_dir():
                    zf.extract(info, dest)
                    continue
                # keep_subs 为空时不应用子模块选择性剥离（全量保留 .pyd 等）
                if category == "submodule" and keep_subs and sub not in keep_subs:
                    skipped += 1
                    continue
                zf.extract(info, dest)
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e
    if skipped:
        _logger.info("精简 %s: 跳过 %d 个未用子模块文件", whl.name, skipped)


def slim_unpack(
    wheels: Sequence[Path],
    site_packages_dir: Path,
    submodule_usage: dict[str, frozenset[str]] | None = None,
    keep_modules: set[str] | None = None,
    *,
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

    ``stage`` 用于通过 ``iter_with_progress`` 显示解压进度并回写处理项数到 BuildTracker。
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
    for whl in iter_with_progress(sorted_wheels, "解压 wheel", stage=stage):
        info = WheelInfo.from_filename(whl.name)
        if info is None:
            # wheel 文件名无法解析，无法确定 top_pkg → 兜底全量解压
            _full_unpack(whl, site_packages_dir)
            count += 1
            continue
        whl_pkg = normalize_name(info.name)
        keep_subs = merged.get(whl_pkg, set())
        top_pkg = _detect_top_pkg(whl, whl_pkg)
        if top_pkg is None:
            # wheel 顶层目录与归一化包名不匹配 → 兜底全量解压
            _full_unpack(whl, site_packages_dir)
            count += 1
            continue
        if keep_subs:
            _logger.info("精简解压 %s: 保留子模块 %s", whl.name, ", ".join(sorted(keep_subs)))
        else:
            # keep_subs 为空：仅应用剥离规则（examples/docs/tests 等），子模块文件全保留
            _logger.info("解压 %s（应用剥离规则）", whl.name)
        _slim_extract(whl, site_packages_dir, top_pkg, keep_subs)
        count += 1
    if stage is not None and count:
        stage.set_detail(f"{count} wheels 解压")
    return count
