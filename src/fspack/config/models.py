"""配置数据结构与镜像源定义.

本模块从 :mod:`fspack.config` 抽离，含所有 dataclass/enum 与镜像源常量，
以及被 :class:`SlimRules` 使用的工具函数。``config.py`` 通过 re-export
保持公开 API 不变。

依赖 :mod:`fspack.config.versions` 提供默认 Python 版本（:data:`DEFAULT_PY_VERSION`
等），:mod:`fspack.config.parsing` 提供 :func:`parse_project`（在
:meth:`ProjectInfo.from_dir` 中延迟导入打破循环依赖）。
"""

from __future__ import annotations

import enum
import fnmatch
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fspack.exceptions import ProjectError
from fspack.platform import Platform

__all__ = [
    "DEFAULT_MIRROR",
    "DEFAULT_SLIM_RULES",
    "MIRRORS",
    "AppType",
    "BuildConfig",
    "BuildDefaults",
    "BuildOptions",
    "DependencyReport",
    "EntryPoint",
    "MirrorConfig",
    "ProjectInfo",
    "SlimRules",
    "build_options_from_defaults",
    "get_mirror",
]

_logger = logging.getLogger(__name__)


class AppType(enum.Enum):
    """应用类型：CLI 控制台、GUI 窗口或 WEB 服务.

    ``WEB`` 用于前后端分离 Web 应用（Flask/FastAPI 等），与 GUI 一样关闭
    控制台窗口（Windows loader 加 ``-mwindows``），且 wrapper 注入静态文件
    serve 与自动开浏览器逻辑。
    """

    CLI = "cli"
    GUI = "gui"
    WEB = "web"


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

DEFAULT_MIRROR = "aliyun"


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
        # 延迟导入打破 config ↔ parsing 循环依赖
        from fspack.config.parsing import infer_app_type

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
    no_slim_runtime: bool | None = None
    ccache: bool | None = None
    nuitka_packages: tuple[str, ...] = ()
    no_size_report: bool | None = None
    analyze_deps: bool | None = None
    # 默认启用的 optional-dependencies 分组名（来自 [tool.fspack] extras），
    # CLI --extra 指定时完全覆盖此值（集合语义，非合并）
    extras: tuple[str, ...] = ()
    # 延迟导入的顶层模块名（来自 [tool.fspack] lazy-imports）：
    # wrapper 注入 _LazyImportFinder meta path finder，首次属性访问时才加载
    lazy_imports: tuple[str, ...] = ()
    # 依赖下载强制哈希校验：透传 pip download --require-hashes，
    # 仅在线模式生效，缓存命中时跳过（缓存目录 wheel 已首次校验）
    require_hashes: bool | None = None
    # 关闭构建结束后的 SBOM 生成：默认输出 SPDX 2.3 兼容 JSON
    # 到 dist/release/<name>-<version>-sbom.json
    no_sbom: bool | None = None
    # Windows 代码签名证书路径：未指定时跳过 signtool 签名。
    # 配置层仅作为 CLI --sign-exe-certificate 的回退默认值
    sign_exe_certificate: str | None = None
    # Windows 代码签名证书密码：与 sign_exe_certificate 配套
    sign_exe_password: str | None = None
    # Linux .deb GPG 签名密钥 ID：未指定时跳过 gpg 签名。
    # 配置层仅作为 CLI --sign-deb-key 的回退默认值
    sign_deb_key: str | None = None
    # WEB 应用启动后自动打开浏览器（webbrowser.open）。WEB 类型默认启用，
    # 配置层 true 对非 WEB 类型也可启用（如 GUI 内嵌 WebView 场景）
    open_browser: bool | None = None


@dataclass(frozen=True)
class SlimRules:
    """wheel 精简用户自定义规则（glob 模式）.

    ``include`` 强制保留被 spec 剥离的文件（覆盖 STRIP_EXTS/闭包外剥离），
    ``exclude`` 强制剥离被 spec 保留的文件（覆盖闭包内/shared 保留）。
    优先级：``include`` > ``exclude`` > spec 自动分类。

    glob 模式用 :func:`fnmatch.fnmatchcase` 匹配 wheel 内 POSIX 相对路径，
    ``*`` 匹配任意字符含 ``/``（如 ``PySide6/translations/*`` 匹配子目录文件）。
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, fspack_cfg: dict[str, Any]) -> SlimRules:
        """从 ``[tool.fspack]`` 解析 ``slim-include``/``slim-exclude``."""
        include = _parse_string_list_cfg(fspack_cfg.get("slim-include"), "slim-include", reject_empty=True)
        exclude = _parse_string_list_cfg(fspack_cfg.get("slim-exclude"), "slim-exclude", reject_empty=True)
        return cls(include=include, exclude=exclude)

    @property
    def has_rules(self) -> bool:
        """是否配置了任何用户规则."""
        return bool(self.include or self.exclude)

    def matches_include(self, path: str) -> bool:
        """检查路径是否匹配任一 include 规则."""
        return _match_any_glob(path, self.include)

    def matches_exclude(self, path: str) -> bool:
        """检查路径是否匹配任一 exclude 规则."""
        return _match_any_glob(path, self.exclude)


# SlimRules 默认空规则单例（frozen dataclass 不可变，安全共享）
DEFAULT_SLIM_RULES = SlimRules()


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
    # 数据资源目录（相对项目目录的 POSIX 路径）：原样保留目录树，
    # copy_source 对其跳过元数据/文档排除（pyproject.toml/*.md/uv.lock 等），
    # _strip_py_sources 跳过其下 .py 剥离。用于含子项目作为资源的场景
    # （如 fspack 自身的 src/fspack/assets/templates/ 含完整项目模板）。
    data_dirs: tuple[str, ...] = ()
    # 前端构建产物目录（相对项目目录的 POSIX 路径，如 "dist"）：与 data_dirs
    # 同等保护——copy_source 跳过元数据排除、_strip_py_sources 跳过 .py 剥离。
    # 此外 entry wrapper 在打包时把这些目录解析为 dist 下绝对路径，注入
    # Flask static_folder / FastAPI StaticFiles serve。仅 AppType.WEB 项目使用。
    web_static_dirs: tuple[str, ...] = ()
    build_defaults: BuildDefaults = field(default_factory=BuildDefaults)
    extra_index_urls: tuple[str, ...] = ()
    find_links: tuple[str, ...] = ()
    # wheel 精简用户规则：include 强制保留（覆盖 spec 剥离），
    # exclude 强制剥离（覆盖 spec 保留）。glob 模式匹配 wheel 内 POSIX 相对路径。
    slim_rules: SlimRules = field(default_factory=SlimRules)
    # [project.optional-dependencies] 全部分组：extra_name → 依赖声明元组
    # （含版本约束）。打包时由 --extra / [tool.fspack] extras 选择启用分组，
    # 经 _expand_extras 合并到下载依赖集合
    optional_dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_dir(cls, project_dir: Path, py_version: str | None = None) -> ProjectInfo:
        """从项目目录解析 pyproject.toml 并构造实例（委托 :func:`parse_project`）."""
        # 延迟导入打破 config.models ↔ config.parsing 循环依赖
        from fspack.config.parsing import parse_project

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

    @property
    def default_entry(self) -> EntryPoint:
        """默认入口：GUI 优先于 WEB 优先于 CLI，同类型按入口名字母排序。

        未显式指定入口时（如 ``fsp r`` 不带 ``--entry``、``fsp doctor --test``
        运行验证），按 GUI 优先、WEB 次之、CLI 最后，同类型按名字母序选第一个，
        保证选择稳定可预期。单入口项目返回唯一入口。

        排序键 ``(0 if GUI else 1 if WEB else 2, name)``：GUI 排前、WEB 次之、
        CLI 最后，同类型内按 ``name`` 升序。``AppType`` 枚举定义序 CLI/GUI/WEB
        不能直接用枚举值排序，须显式让 GUI > WEB > CLI。
        """
        entries = self.all_entries
        return min(
            entries,
            key=lambda ep: (0 if ep.app_type is AppType.GUI else 1 if ep.app_type is AppType.WEB else 2, ep.name),
        )


@dataclass(frozen=True)
class DependencyReport:
    """依赖分析结果.

    ``ast_errors`` 记录 AST 解析失败的文件与错误信息（iter-138 引入），
    格式为 ``"<相对路径>: <错误信息>"``，供上层向用户提示哪些文件被跳过，
    避免静默丢失依赖分析失败的诊断信息。
    """

    declared: tuple[str, ...]
    ast_third_party: tuple[str, ...]
    ast_stdlib: tuple[str, ...]
    ast_local: tuple[str, ...]
    ast_submodules: dict[str, frozenset[str]] = field(default_factory=dict)
    ast_errors: tuple[str, ...] = ()

    @classmethod
    def from_src(
        cls,
        src_dir: Path,
        project_name: str,
        declared: tuple[str, ...],
        data_dirs: tuple[str, ...] = (),
    ) -> DependencyReport:
        """扫描源码目录构造依赖分析报告。

        ``data_dirs`` 为 ``[tool.fspack] data-dirs`` 配置的数据资源目录树（相对
        ``src_dir`` 的 POSIX 路径），其下 ``.py`` 是模板/前端产物等数据资源，
        不应被 AST 扫描误判为项目依赖。

        惰性导入 :func:`fspack.analyzer.analyze_dependencies` 打破 config ↔ analyzer 循环依赖。
        """
        from fspack.analyzer import analyze_dependencies

        return analyze_dependencies(src_dir, project_name, declared, data_dirs)

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
    - ``no_slim_runtime``：关闭 standalone runtime 精简（默认 strip libpython 调试符号 +
      删 python3.X 二进制 + 删 include/share + 非 tkinter 项目剥离 Tcl/Tk，省 ~100MB）
    - ``no_pyc``：关闭字节码预编译
    - ``pyc_strip``：剥离非 ``__init__.py`` 的 ``.py`` 源码
    - ``pyc_optimize``：字节码优化级别 0/1/2（``compileall -o``）
    - ``no_site``：禁用 ``site.py`` 加载（``_pth`` 省略 ``import site``）
    - ``nuitka``：启用 Nuitka 编译模式（用户源码编译为 ``.pyd``）
    - ``ccache``：Nuitka 编译启用 ccache 缓存（首次下载到本地，后续复用，加速重复编译）
    """

    keep_modules: set[str] | None = None
    icon: Path | None = None
    no_stdlib_trim: bool = False
    no_slim_runtime: bool = False
    no_pyc: bool = False
    pyc_strip: bool = False
    # pyc_optimize 默认 2（与 cli.py --pyc-optimize argparse default 一致）
    pyc_optimize: int = 2
    no_site: bool = False
    nuitka: bool = False
    ccache: bool = False
    nuitka_packages: tuple[str, ...] = ()
    # 关闭构建结束后的体积报告（默认输出，--no-size-report 关闭）
    no_size_report: bool = False
    # 启用二进制依赖分析：解析 .dll/.so/.dylib 依赖树，剥离无引用文件
    # （默认关闭，耗时）
    analyze_deps: bool = False
    # 启用的 [project.optional-dependencies] 分组名集合，
    # 来自 CLI --extra（覆盖配置默认）或 [tool.fspack] extras（配置默认）
    extras: frozenset[str] = frozenset()
    # 延迟导入的顶层模块名元组：wrapper 注入 _LazyImportFinder，
    # 首次属性访问时才加载模块，降低启动时间
    lazy_imports: tuple[str, ...] = ()
    # 依赖下载强制哈希校验：透传 pip download --require-hashes，
    # 仅在线模式生效，缓存命中时跳过（缓存目录 wheel 已首次校验）
    require_hashes: bool = False
    # 关闭构建结束后的 SBOM 生成：默认输出 SPDX 2.3 兼容 JSON
    no_sbom: bool = False
    # Windows 代码签名证书路径：非 None 时调用 signtool 签名 exe 与安装包
    sign_exe_certificate: Path | None = None
    # Windows 代码签名证书密码：与 sign_exe_certificate 配套
    sign_exe_password: str | None = None
    # Linux .deb GPG 签名密钥 ID：非 None 时调用 gpg --detach-sign 签名 .deb
    sign_deb_key: str | None = None
    # WEB 应用启动后自动打开浏览器：WEB 类型在 stages 层默认启用
    # （app_type is WEB），配置/CLI 可显式覆盖（如 GUI 内嵌 WebView 也启用）
    open_browser: bool = False


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
        no_slim_runtime=defaults.no_slim_runtime if defaults.no_slim_runtime is not None else base.no_slim_runtime,
        no_pyc=defaults.no_pyc if defaults.no_pyc is not None else base.no_pyc,
        pyc_strip=defaults.pyc_strip if defaults.pyc_strip is not None else base.pyc_strip,
        pyc_optimize=defaults.pyc_optimize if defaults.pyc_optimize is not None else base.pyc_optimize,
        no_site=defaults.no_site if defaults.no_site is not None else base.no_site,
        nuitka=defaults.nuitka if defaults.nuitka is not None else base.nuitka,
        ccache=defaults.ccache if defaults.ccache is not None else base.ccache,
        nuitka_packages=defaults.nuitka_packages,
        no_size_report=defaults.no_size_report if defaults.no_size_report is not None else base.no_size_report,
        analyze_deps=defaults.analyze_deps if defaults.analyze_deps is not None else base.analyze_deps,
        extras=frozenset(defaults.extras),
        lazy_imports=defaults.lazy_imports,
        require_hashes=defaults.require_hashes if defaults.require_hashes is not None else base.require_hashes,
        no_sbom=defaults.no_sbom if defaults.no_sbom is not None else base.no_sbom,
        sign_exe_certificate=(
            Path(defaults.sign_exe_certificate) if defaults.sign_exe_certificate else base.sign_exe_certificate
        ),
        sign_exe_password=defaults.sign_exe_password
        if defaults.sign_exe_password is not None
        else base.sign_exe_password,
        sign_deb_key=defaults.sign_deb_key if defaults.sign_deb_key is not None else base.sign_deb_key,
        open_browser=defaults.open_browser if defaults.open_browser is not None else base.open_browser,
    )


def _parse_string_list_cfg(
    value: object,
    cfg_key: str,
    *,
    reject_empty: bool = False,
) -> tuple[str, ...]:
    """解析 ``[tool.fspack]`` 字符串列表配置为去空白元组.

    通用解析器，覆盖 ``exclude``/``extra-index-urls``/``find-links``/
    ``slim-include``/``slim-exclude`` 等场景。

    Args:
        value: 配置原始值（``list`` 或 ``None``）
        cfg_key: 配置键名（用于错误消息，如 ``"slim-include"``）
        reject_empty: ``True`` 时空字符串元素报错（``exclude``/``slim-*``），
            ``False`` 时静默过滤（``extra-index-urls``/``find-links``）

    Returns:
        去空白后的字符串元组；``value`` 为 ``None`` 时返回空元组

    Raises:
        ProjectError: ``value`` 非 list、元素非字符串、或空元素且 ``reject_empty=True``
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProjectError(f"[tool.fspack] {cfg_key} 必须是字符串列表，得到 {type(value).__name__}")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ProjectError(f"[tool.fspack] {cfg_key} 元素必须是字符串，得到 {item!r}")
        stripped = item.strip()
        if not stripped:
            if reject_empty:
                raise ProjectError(f"[tool.fspack] {cfg_key} 元素必须是非空字符串，得到 {item!r}")
            continue
        result.append(stripped)
    return tuple(result)


def _match_any_glob(path: str, patterns: tuple[str, ...]) -> bool:
    """检查 path 是否匹配任一 glob 模式（fnmatch 大小写敏感，``*`` 含 ``/``）."""
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)
