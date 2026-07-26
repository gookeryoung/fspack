"""fspack 配置数据结构与 pyproject.toml 解析.

本模块整合了项目元信息的数据结构定义（``ProjectInfo``/``EntryPoint``/
``AppType`` 等）与从 ``pyproject.toml`` 解析填充这些结构的逻辑
（``parse_project``/``detect_entry``/``resolve_py_version``）。
两者合并消除原 ``config`` ↔ ``project`` 模块间的循环依赖。

公共 API：

- 数据结构：``AppType``/``MirrorConfig``/``EntryPoint``/``ProjectInfo``/
  ``DependencyReport``/``BuildConfig``
- 镜像源：``MIRRORS``/``DEFAULT_MIRROR``/``get_mirror``
- 项目解析：``parse_project``/``detect_entry``/``infer_app_type``/
  ``resolve_py_version``/``DEFAULT_PY_VERSION``/``DEFAULT_LINUX_PY_VERSION``/
  ``KNOWN_EMBED_VERSIONS``/``KNOWN_STANDALONE_VERSIONS``/``known_versions``
"""

from __future__ import annotations

import ast
import codecs
import enum
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fspack.exceptions import ProjectError
from fspack.platform import Platform

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]
    except ImportError as e:  # pragma: no cover
        raise ProjectError("解析 pyproject.toml 需要 tomli（Python<3.11），请安装 tomli") from e

__all__ = [
    "DEFAULT_LINUX_PY_VERSION",
    "DEFAULT_MIRROR",
    "DEFAULT_NUITKA_VERSION",
    "DEFAULT_PY_VERSION",
    "KNOWN_EMBED_VERSIONS",
    "KNOWN_STANDALONE_VERSIONS",
    "MIRRORS",
    "NUITKA_VERSIONS",
    "AppType",
    "BuildConfig",
    "BuildDefaults",
    "BuildOptions",
    "DependencyReport",
    "EntryPoint",
    "MirrorConfig",
    "ProjectInfo",
    "build_options_from_defaults",
    "detect_entry",
    "get_mirror",
    "infer_app_type",
    "known_versions",
    "nuitka_version_for",
    "parse_project",
    "resolve_py_version",
]

_logger = logging.getLogger(__name__)


class AppType(enum.Enum):
    """应用类型：CLI 控制台或 GUI 窗口."""

    CLI = "cli"
    GUI = "gui"


@dataclass(frozen=True)
class MirrorConfig:
    """国内镜像源配置."""

    name: str
    python_base: str
    pypi_index: str

    def embed_url(self, version: str) -> str:
        """返回指定版本的 embed python zip 下载地址."""
        return f"{self.python_base}/{version}/python-{version}-embed-amd64.zip"


# 预定义国内镜像源实例
MIRRORS: dict[str, MirrorConfig] = {
    "huawei": MirrorConfig(
        name="华为云",
        python_base="https://mirrors.huaweicloud.com/python",
        pypi_index="https://mirrors.huaweicloud.com/pypi/simple/",
    ),
    "aliyun": MirrorConfig(
        name="阿里云",
        python_base="https://npmmirror.com/mirrors/python",
        pypi_index="https://mirrors.aliyun.com/pypi/simple/",
    ),
    "tsinghua": MirrorConfig(
        name="清华",
        python_base="https://mirrors.tuna.tsinghua.edu.cn/python",
        pypi_index="https://pypi.tuna.tsinghua.edu.cn/simple/",
    ),
}

DEFAULT_MIRROR = "tsinghua"


def get_mirror(name: str | None = None) -> MirrorConfig:
    """按名称获取镜像配置，name 为 None 时返回默认镜像."""
    key = name or DEFAULT_MIRROR
    if key not in MIRRORS:
        raise KeyError(f"未知镜像源: {key}，可选: {', '.join(MIRRORS)}")
    return MIRRORS[key]


@dataclass(frozen=True)
class EntryPoint:
    """单个打包入口：用于多入口项目生成多个可执行文件."""

    name: str
    module: str
    file: Path
    app_type: AppType

    @classmethod
    def from_script(cls, name: str, script_path: Path) -> EntryPoint:
        """从入口名与脚本路径构造实例。

        ``module`` 取脚本文件名 stem；``app_type`` 调用 :func:`infer_app_type`
        仅按脚本自身 import 推断（多入口项目共享 declared，不能据项目级
        依赖判断单个入口类型）。
        """
        return cls(
            name=name,
            module=script_path.stem,
            file=script_path,
            app_type=infer_app_type(script_path, ()),
        )

    def entry_rel(self, src_dir: Path) -> str:
        """入口脚本相对源码目录的 POSIX 路径（用于写入 .entry 文件）."""
        return self.file.relative_to(src_dir).as_posix()


@dataclass(frozen=True)
class BuildDefaults:
    """``[tool.fspack]`` 构建默认值，CLI 标志覆盖这些值.

    所有字段为 ``None`` 表示配置未指定，使用 :class:`BuildOptions` 默认值。
    非 ``None`` 时作为 CLI 未显式指定该标志时的回退默认值。

    合并策略（在 :func:`fspack.cli.main` 中执行）：

    - 布尔开关（``nuitka``/``pyc_strip``/``no_site`` 等）：``cli or config``，
      即 CLI 显式启用或配置启用 → 启用
    - 整数开关（``pyc_optimize``）：``cli if cli is not None else config if config is not None else 2``，
      CLI 显式指定 > 配置指定 > 默认值 2
    """

    nuitka: bool | None = None
    pyc_strip: bool | None = None
    pyc_optimize: int | None = None
    no_site: bool | None = None
    no_pyc: bool | None = None
    no_stdlib_trim: bool | None = None


@dataclass(frozen=True)
class ProjectInfo:
    """解析后的项目元信息."""

    name: str
    version: str
    src_dir: Path
    entry_module: str
    entry_file: Path
    app_type: AppType
    dependencies: tuple[str, ...]
    py_version: str
    requires_python: str | None = None
    entries: tuple[EntryPoint, ...] = ()
    icon: Path | None = None
    exclude_dirs: tuple[str, ...] = ()
    build_defaults: BuildDefaults = field(default_factory=BuildDefaults)

    @classmethod
    def from_dir(cls, project_dir: Path, py_version: str | None = None) -> ProjectInfo:
        """从项目目录解析 pyproject.toml 并构造实例（委托 :func:`parse_project`）."""
        return parse_project(project_dir, py_version)

    @property
    def exe_name(self) -> str:
        """生成的可执行文件名（单入口模式）."""
        return f"{self.name}.exe"

    @property
    def py_xy(self) -> str:
        """形如 python311 的版本前缀."""
        major, minor = self.py_version.split(".")[:2]
        return f"python{major}{minor}"

    @property
    def all_entries(self) -> tuple[EntryPoint, ...]:
        """所有入口：多入口模式返回 entries，单入口模式构造单一入口."""
        if self.entries:
            return self.entries
        return (EntryPoint(name=self.name, module=self.entry_module, file=self.entry_file, app_type=self.app_type),)


@dataclass(frozen=True)
class DependencyReport:
    """依赖分析结果."""

    declared: tuple[str, ...]
    ast_third_party: tuple[str, ...]
    ast_stdlib: tuple[str, ...]
    ast_local: tuple[str, ...]
    ast_submodules: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_src(cls, src_dir: Path, project_name: str, declared: tuple[str, ...]) -> DependencyReport:
        """扫描源码目录构造依赖分析报告。

        惰性导入 :func:`fspack.analyzer.analyze_dependencies` 打破 config ↔ analyzer 循环依赖。
        """
        from fspack.analyzer import analyze_dependencies

        return analyze_dependencies(src_dir, project_name, declared)

    @property
    def missing(self) -> tuple[str, ...]:
        """AST 发现但未在 pyproject 声明的第三方依赖."""
        declared_top = {
            re.split(r"[<>=!~;\[]", d, maxsplit=1)[0].strip().replace("-", "_").lower() for d in self.declared
        }
        return tuple(sorted(m for m in self.ast_third_party if m.lower() not in declared_top))


@dataclass(frozen=True)
class BuildConfig:
    """单次构建的运行参数."""

    project_dir: Path
    dist_dir: Path
    embed_cache_dir: Path
    mirror: MirrorConfig
    target: Platform = Platform.WINDOWS


@dataclass(frozen=True)
class BuildOptions:
    """构建行为开关（不影响产物路径与运行时环境）。

    与 :class:`BuildConfig` 区别：``BuildConfig`` 封装路径与镜像配置（必需），
    ``BuildOptions`` 封装构建行为开关（可选，默认值对应原 ``build()`` 行为）。
    将原 ``build()`` 的 8 个开关参数聚合为一个 dataclass，便于扩展与透传。

    字段：
    - ``keep_modules``：显式保留的子模块集合（如 ``{"PySide2.QtGui"}``）
    - ``icon``：exe 图标路径，覆盖项目配置与自动搜索
    - ``no_stdlib_trim``：关闭标准库精简（默认剥离 Linux standalone 无用模块）
    - ``no_pyc``：关闭字节码预编译
    - ``pyc_strip``：剥离非 ``__init__.py`` 的 ``.py`` 源码
    - ``pyc_optimize``：字节码优化级别 0/1/2（``compileall -o``）
    - ``no_site``：禁用 ``site.py`` 加载（``_pth`` 省略 ``import site``）
    - ``nuitka``：启用 Nuitka 编译模式（用户源码编译为 ``.pyd``）
    """

    keep_modules: set[str] | None = None
    icon: Path | None = None
    no_stdlib_trim: bool = False
    no_pyc: bool = False
    pyc_strip: bool = False
    # pyc_optimize 默认 2（与 cli.py --pyc-optimize argparse default 一致，iter-35 决策）
    pyc_optimize: int = 2
    no_site: bool = False
    nuitka: bool = False


def build_options_from_defaults(defaults: BuildDefaults) -> BuildOptions:
    """从 ``[tool.fspack]`` 构建默认值构造 :class:`BuildOptions`.

    ``BuildDefaults`` 中 ``None`` 的字段使用 :class:`BuildOptions` 默认值，
    非 ``None`` 的字段覆盖默认值。用于无 CLI 标志的场景（如 ``fsp p`` 内部
    调用 :func:`fspack.builder.build` 时透传项目配置）。

    与 :func:`fspack.cli.main` 的合并策略不同：本函数不处理 CLI 标志覆盖，
    仅将配置层默认值转为运行层 :class:`BuildOptions`。CLI 合并逻辑由
    ``cli.py`` 直接内联实现（``any([cli, config])``）。
    """
    base = BuildOptions()
    return replace(
        base,
        no_stdlib_trim=defaults.no_stdlib_trim if defaults.no_stdlib_trim is not None else base.no_stdlib_trim,
        no_pyc=defaults.no_pyc if defaults.no_pyc is not None else base.no_pyc,
        pyc_strip=defaults.pyc_strip if defaults.pyc_strip is not None else base.pyc_strip,
        pyc_optimize=defaults.pyc_optimize if defaults.pyc_optimize is not None else base.pyc_optimize,
        no_site=defaults.no_site if defaults.no_site is not None else base.no_site,
        nuitka=defaults.nuitka if defaults.nuitka is not None else base.nuitka,
    )


# ---- pyproject.toml 解析与项目入口识别 ----

# Windows embed python 版本映射：major.minor → 完整版本号
# python.org 在 minor 维护期内发布二进制 embed zip；进入 security-only 阶段后仅发
# 源码包，不再提供 embed zip。下表为各 minor 最后一个含二进制 installer 的版本：
#   3.8 → 3.8.10（EOL）、3.9 → 3.9.13、3.10 → 3.10.11、3.11 → 3.11.9、
#   3.12 → 3.12.10；3.13/3.14 仍处 bugfix 阶段，取最新发布版本。
KNOWN_EMBED_VERSIONS: dict[str, str] = {
    "3.8": "3.8.10",
    "3.9": "3.9.13",
    "3.10": "3.10.11",
    "3.11": "3.11.9",
    "3.12": "3.12.10",
    "3.13": "3.13.14",
    "3.14": "3.14.6",
}

# Linux python-build-standalone 版本映射：major.minor → 完整版本号
# astral-sh 每个 release tag 持续构建每个 minor 的最新补丁版本（含 security-only
# 阶段的源码-only 版本），版本号须与 STANDALONE_RELEASE_TAG（runtime.py）实际提供
# 的一致，否则下载 404。3.8/3.9 已 EOL，astral-sh 不再发布。
KNOWN_STANDALONE_VERSIONS: dict[str, str] = {
    "3.10": "3.10.20",
    "3.11": "3.11.15",
    "3.12": "3.12.13",
    "3.13": "3.13.14",
    "3.14": "3.14.6",
}

# 默认 Python 版本：从对应平台的版本表派生，确保 EMBED 与 STANDALONE 各自使用最新版。
# 更新版本表时默认值自动跟随，避免硬编码常量与版本表不同步。
DEFAULT_PY_VERSION = KNOWN_EMBED_VERSIONS["3.11"]
DEFAULT_LINUX_PY_VERSION = KNOWN_STANDALONE_VERSIONS["3.11"]


# Nuitka 版本按目标 Python major.minor 锁定。
# - 3.8/3.9：nuitka 2.5.1（2.x 末尾稳定版，4.x 已不再维护 Python 3.8 EOL）
# - 3.10+：nuitka 4.1.3（当前最新稳定版，覆盖 3.10/3.11/3.12/3.13/3.14）
# 键用 major.minor 与 KNOWN_*_VERSIONS 风格一致，避免每个补丁版本重复
# （3.11.9 与 3.11.15 共用 4.1.3）。
NUITKA_VERSIONS: dict[str, str] = {
    "3.8": "2.5.1",
    "3.9": "2.5.1",
    "3.10": "4.1.3",
    "3.11": "4.1.3",
    "3.12": "4.1.3",
    "3.13": "4.1.3",
    "3.14": "4.1.3",
}

# 默认 Nuitka 版本：py_version 不在 NUITKA_VERSIONS 时回退（如未来 3.15）。
DEFAULT_NUITKA_VERSION = "4.1.3"


def nuitka_version_for(py_version: str) -> str:
    """按目标 Python 版本返回锁定的 Nuitka 版本.

    Args:
        py_version: 完整 Python 版本号（如 ``3.11.9``）。

    Returns:
        对应的 Nuitka 版本号（如 ``4.1.3``）；未知 Python 版本回退
        :data:`DEFAULT_NUITKA_VERSION`。
    """
    major, minor = py_version.split(".")[:2]
    return NUITKA_VERSIONS.get(f"{major}.{minor}", DEFAULT_NUITKA_VERSION)


def known_versions(target: Platform) -> dict[str, str]:
    """按目标平台返回已知 Python 版本映射.

    Windows 用 :data:`KNOWN_EMBED_VERSIONS`（python.org embed zip 可用版本），
    Linux 用 :data:`KNOWN_STANDALONE_VERSIONS`（python-build-standalone release 可用版本）。
    两侧最新补丁版本可能不同：如 3.11 Windows 最新 embed 为 3.11.9，Linux standalone 为 3.11.15。
    """
    if target is Platform.LINUX:
        return KNOWN_STANDALONE_VERSIONS
    return KNOWN_EMBED_VERSIONS


def _ver_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


# PEP 440 版本规范符正则
_SPEC_RE = re.compile(r"(>=|<=|==|!=|~=|>|<)\s*(\d+(?:\.\d+)*)")

_GUI_HINTS = frozenset({"tkinter", "PySide2", "PySide6", "PyQt5", "PyQt6", "matplotlib", "wx", "win32gui", "pygame"})


def parse_project(project_dir: Path, py_version: str | None = None) -> ProjectInfo:
    """解析 pyproject.toml 并识别入口，返回项目元信息。

    支持多入口声明 ``[tool.fspack.entries]``：键为入口名（用作 exe 名），
    值为入口脚本相对项目目录的路径（POSIX 风格）。声明多入口时，
    ``ProjectInfo.entries`` 非空，``entry_module``/``entry_file``/``app_type``
    取首个入口（保持向后兼容）。
    """
    project_dir = Path(project_dir).resolve()
    pp = project_dir / "pyproject.toml"
    if not pp.is_file():
        raise ProjectError(f"未找到 pyproject.toml: {pp}")
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ProjectError(f"pyproject.toml 语法错误: {e}") from e

    proj = data.get("project", {})
    if not isinstance(proj, dict):
        raise ProjectError("pyproject.toml [project] 节格式异常")
    name = str(proj.get("name") or project_dir.name)
    version = str(proj.get("version", "0.0.0"))
    deps = tuple(str(d) for d in proj.get("dependencies", []))
    requires_python = str(proj.get("requires-python") or "") or None

    tool: dict[str, Any] = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
    fspack_cfg: dict[str, Any] = tool.get("fspack", {}) if isinstance(tool.get("fspack"), dict) else {}
    entries_tbl: dict[str, Any] = fspack_cfg.get("entries", {}) if isinstance(fspack_cfg.get("entries"), dict) else {}
    icon_rel = fspack_cfg.get("icon")
    icon_path = _resolve_icon(project_dir, icon_rel)

    # [tool.fspack] exclude：额外排除目录/文件模式（合并到 copy_source 内置 _EXCLUDE）
    exclude_dirs = _parse_exclude_dirs(fspack_cfg.get("exclude"))
    # [tool.fspack] 构建默认值：CLI 标志覆盖
    build_defaults = _parse_build_defaults(fspack_cfg)

    if entries_tbl:
        entries = _parse_entries(project_dir, entries_tbl)
        first = entries[0]
        return ProjectInfo(
            name=name,
            version=version,
            src_dir=project_dir,
            entry_module=first.module,
            entry_file=first.file,
            app_type=first.app_type,
            dependencies=deps,
            py_version=py_version or DEFAULT_PY_VERSION,
            requires_python=requires_python,
            entries=entries,
            icon=icon_path,
            exclude_dirs=exclude_dirs,
            build_defaults=build_defaults,
        )

    entry_module, entry_file, app_type = detect_entry(project_dir, name, deps)
    return ProjectInfo(
        name=name,
        version=version,
        src_dir=project_dir,
        entry_module=entry_module,
        entry_file=entry_file,
        app_type=app_type,
        dependencies=deps,
        py_version=py_version or DEFAULT_PY_VERSION,
        requires_python=requires_python,
        icon=icon_path,
        exclude_dirs=exclude_dirs,
        build_defaults=build_defaults,
    )


def _parse_entries(
    project_dir: Path,
    entries_tbl: dict[str, Any],
) -> tuple[EntryPoint, ...]:
    """解析 ``[tool.fspack.entries]`` 表为 EntryPoint 元组。

    键为入口名（用作 exe 名，须为合法标识符风格），值为入口脚本相对
    项目目录的路径。脚本路径不存在或为空时报错。Python 字典保持插入序，
    首个入口作为主入口（保持向后兼容）。

    多入口模式下每个入口的 ``app_type`` 按脚本自身 import 推断，不看项目级
    declared（不同入口可能是不同类型，如 cli/gui/web 混合）。
    """
    if not entries_tbl:
        raise ProjectError("[tool.fspack.entries] 为空，请删除该表或至少声明一个入口")
    entries: list[EntryPoint] = []
    for entry_name, script_rel in entries_tbl.items():
        if not isinstance(entry_name, str) or not entry_name:
            raise ProjectError(f"[tool.fspack.entries] 入口名无效: {entry_name!r}")
        if not isinstance(script_rel, str) or not script_rel.strip():
            raise ProjectError(f"[tool.fspack.entries] {entry_name} 的脚本路径为空")
        script_path = (project_dir / script_rel).resolve()
        if not script_path.is_file():
            raise ProjectError(f"[tool.fspack.entries] {entry_name} 的脚本不存在: {script_rel}")
        entries.append(EntryPoint.from_script(entry_name, script_path))
    return tuple(entries)


def _parse_exclude_dirs(value: object) -> tuple[str, ...]:
    """解析 ``[tool.fspack] exclude`` 配置为排除模式元组.

    接受字符串列表（如 ``["examples", "docs"]``），每个元素为 glob 模式，
    合并到 :func:`fspack.builder.copy_source` 内置 ``_EXCLUDE`` 中。
    非列表或元素非字符串时报错。
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProjectError(f"[tool.fspack] exclude 必须是字符串列表，得到 {type(value).__name__}")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ProjectError(f"[tool.fspack] exclude 元素必须是非空字符串，得到 {item!r}")
        result.append(item.strip())
    return tuple(result)


# [tool.fspack] 构建默认值键名与 BuildDefaults 字段的映射
_BUILD_DEFAULT_KEYS: dict[str, str] = {
    "nuitka": "nuitka",
    "pyc_strip": "pyc_strip",
    "pyc_optimize": "pyc_optimize",
    "no_site": "no_site",
    "no_pyc": "no_pyc",
    "no_stdlib_trim": "no_stdlib_trim",
}


def _parse_build_defaults(fspack_cfg: dict[str, Any]) -> BuildDefaults:
    """从 ``[tool.fspack]`` 解析构建默认值.

    识别 ``nuitka``/``pyc_strip``/``pyc_optimize``/``no_site``/``no_pyc``/
    ``no_stdlib_trim`` 键，其余键忽略（如 ``icon``/``entries``/``exclude``）。
    类型不匹配时报错，避免静默忽略错误配置。
    """
    kwargs: dict[str, bool | int | None] = {}
    for cfg_key, field_name in _BUILD_DEFAULT_KEYS.items():
        raw = fspack_cfg.get(cfg_key)
        if raw is None:
            continue
        if field_name == "pyc_optimize":
            if not isinstance(raw, int) or raw not in (0, 1, 2):
                raise ProjectError(f"[tool.fspack] {cfg_key} 必须是 0/1/2，得到 {raw!r}")
            kwargs[field_name] = raw
        else:
            if not isinstance(raw, bool):
                raise ProjectError(f"[tool.fspack] {cfg_key} 必须是布尔值，得到 {raw!r}")
            kwargs[field_name] = raw
    return BuildDefaults(**kwargs)  # type: ignore[arg-type]


def _resolve_icon(project_dir: Path, icon_rel: object) -> Path | None:
    """解析 ``[tool.fspack] icon`` 配置为绝对路径。

    ``icon_rel`` 为相对项目目录的路径字符串（POSIX 或原生均可）。为空时返回
    ``None``（由 builder 回退到默认 icon）。路径不存在时报错，避免构建时
    才发现 windres 找不到文件。
    """
    if not icon_rel:
        return None
    if not isinstance(icon_rel, str) or not icon_rel.strip():
        raise ProjectError(f"[tool.fspack] icon 配置无效: {icon_rel!r}")
    icon_path = (project_dir / icon_rel.strip()).resolve()
    if not icon_path.is_file():
        raise ProjectError(f"[tool.fspack] icon 文件不存在: {icon_rel}")
    return icon_path


def _read_python_version(path: Path) -> str:
    """读取 ``.python-version`` 文件内容，自动识别 BOM 编码。

    ``.python-version`` 可能由不同编辑器保存为 UTF-8（含/不含 BOM）或 UTF-16，
    通过字节序标记自动选择解码方式，避免 ``UnicodeDecodeError``。

    Args:
        path: ``.python-version`` 文件路径。

    Returns:
        去除首尾空白后的版本字符串。
    """
    data = path.read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig").strip()
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16").strip()
    return data.decode("utf-8").strip()


def _normalize_py_version(version: str, versions: dict[str, str]) -> str | None:
    """将版本号规范化为完整版本（``major.minor.micro``）。

    短版本号（``major.minor``，如 ``"3.13"``）查 ``versions`` 映射得到完整版本号
    （如 ``"3.13.14"``）；完整版本号（>=3 段）原样返回；未知短版本号（无映射）
    告警并返回 ``None``，避免拼出错误下载 URL。

    Args:
        version: 用户输入的版本号，可能为短版本（``"3.13"``）或完整版本（``"3.13.14"``）。
        versions: 平台对应的已知版本映射（embed 或 standalone）。

    Returns:
        完整版本号字符串，或 ``None``（未知短版本号）。
    """
    if version in versions:
        return versions[version]
    if len(version.split(".")) >= 3:
        return version
    _logger.warning("版本号 %s 不在已知版本映射中", version)
    return None


def resolve_py_version(
    project_dir: Path,
    explicit: str | None,
    requires_python: str | None,
    default: str = DEFAULT_PY_VERSION,
    target: Platform = Platform.WINDOWS,
) -> str:
    """解析最终使用的 Python 版本。

    优先级：
    1. ``explicit``（``--py-version`` CLI 标志）—— 不满足 ``requires-python`` 时告警但仍使用
    2. ``.python-version`` 文件 —— 不满足 ``requires-python`` 时告警并回退到自动选择
    3. ``requires-python`` 约束 —— 自动选择最高兼容已知版本
    4. ``default``

    ``explicit`` 与 ``.python-version`` 均支持短版本号（如 ``"3.13"``），通过
    :func:`known_versions` 按目标平台选取映射（embed 或 standalone），映射为完整版本号
    （如 ``"3.13.14"``），避免拼出不存在的下载 URL。

    Args:
        project_dir: 项目目录，用于读取 ``.python-version``。
        explicit: ``--py-version`` CLI 显式指定的版本号。
        requires_python: ``pyproject.toml`` 的 ``requires-python`` 约束。
        default: 无任何线索时的默认版本。
        target: 目标平台，决定短版本号映射查 embed 还是 standalone 表。
    """
    versions = known_versions(target)
    if explicit:
        full = _normalize_py_version(explicit, versions)
        resolved = full if full is not None else explicit
        if requires_python and not _satisfies(resolved, requires_python):
            _logger.warning("Python %s 不满足 requires-python: %s", resolved, requires_python)
        return resolved

    pv_file = project_dir / ".python-version"
    if pv_file.is_file():
        pv = _read_python_version(pv_file)
        full = _normalize_py_version(pv, versions)
        if full is not None:
            if requires_python and not _satisfies(full, requires_python):
                _logger.warning(
                    ".python-version %s 不满足 requires-python: %s，自动选择兼容版本", full, requires_python
                )
            else:
                return full

    if requires_python:
        # 按目标平台选取候选版本：Windows 用 embed，Linux 用 standalone
        candidates = sorted(versions.values(), key=_ver_key, reverse=True)
        for ver in candidates:
            if _satisfies(ver, requires_python):
                return ver
        raise ProjectError(f"requires-python: {requires_python}，无已知兼容 python 版本")

    return default


def _satisfies(version: str, specifiers: str) -> bool:
    """检查版本是否满足 PEP 440 ``requires-python`` 规范符."""
    ver_parts = tuple(int(x) for x in version.split("."))
    for op, spec_ver in _SPEC_RE.findall(specifiers):
        spec_parts = tuple(int(x) for x in spec_ver.split("."))
        length = max(len(ver_parts), len(spec_parts))
        ver = ver_parts + (0,) * (length - len(ver_parts))
        spec = spec_parts + (0,) * (length - len(spec_parts))
        if op == ">=":
            ok = ver >= spec
        elif op == "<=":
            ok = ver <= spec
        elif op == ">":
            ok = ver > spec
        elif op == "<":
            ok = ver < spec
        elif op == "==":
            ok = ver == spec
        elif op == "!=":
            ok = ver != spec
        else:
            continue
        if not ok:
            return False
    return True


def detect_entry(
    src_dir: Path,
    name: str,
    deps: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, Path, AppType]:
    """识别入口模块，返回 (module, file, app_type)。

    优先匹配 <name>.py 与 <name>/__main__.py，再兜底扫描顶层 .py。
    入口判定：含 def main() 或 if __name__ == "__main__" 块。
    """
    declared = tuple(deps or ())
    candidates: list[tuple[str, Path]] = []
    direct = src_dir / f"{name}.py"
    if direct.is_file():
        candidates.append((name, direct))
    pkg_main = src_dir / name / "__main__.py"
    if pkg_main.is_file():
        candidates.append((name, pkg_main))
    for py in sorted(src_dir.glob("*.py")):
        candidates.append((py.stem, py))

    seen: set[str] = set()
    for mod, path in candidates:
        if mod not in seen and path.is_file():
            seen.add(mod)
            if _has_entry(path):
                return mod, path, infer_app_type(path, declared)
    raise ProjectError(f"未识别到入口（需 def main() 或 if __name__=='__main__'）: {src_dir}")


def _has_entry(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return True
        if isinstance(node, ast.If) and _is_main_check(node.test):
            return True
    return False


def _is_main_check(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "__name__"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value == "__main__"
    )


def infer_app_type(path: Path, declared: tuple[str, ...]) -> AppType:
    """根据 import 与声明依赖推断 CLI/GUI 类型.

    惰性导入 :func:`fspack.analyzer.collect_imports` 打破 config ↔ analyzer 循环依赖。
    """
    from fspack.analyzer import collect_imports

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for top in collect_imports(tree):
        if top in _GUI_HINTS:
            return AppType.GUI
    for dep in declared:
        top = re.split(r"[<>=!~;\[]", dep, maxsplit=1)[0].strip().replace("-", "_")
        if top in _GUI_HINTS:
            return AppType.GUI
    return AppType.CLI
