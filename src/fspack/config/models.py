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
import functools
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

# 常见「PyPI 分发名 ≠ 导入名」静态映射表（归一化 PyPI 名 → 导入名元组，均小写）。
# AST 扫描得到的是导入名（如 ``yaml``），pyproject 声明的是 PyPI 分发名（如
# ``PyYAML``），单纯归一化（``-``→``_``、小写）无法匹配，需显式映射消除
# ``missing`` 误报。仅收录高频且无歧义的映射：多个分发共享的顶层导入名
# （如 protobuf 的 ``google``）不收录，避免误吞真实缺失。
_PYPI_IMPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "attrs": ("attr",),
    "beautifulsoup4": ("bs4",),
    "djangorestframework": ("rest_framework",),
    "gitpython": ("git",),
    "grpcio": ("grpc",),
    "opencv_python": ("cv2",),
    "opencv_python_headless": ("cv2",),
    "opencv_contrib_python": ("cv2",),
    "opencv_contrib_python_headless": ("cv2",),
    "pdfminer_six": ("pdfminer",),
    "psycopg2_binary": ("psycopg2",),
    "pycryptodome": ("crypto",),
    "pycryptodomex": ("cryptodome",),
    "pyjwt": ("jwt",),
    "pymupdf": ("fitz",),
    "pyserial": ("serial",),
    "python_dateutil": ("dateutil",),
    "python_dotenv": ("dotenv",),
    "python_json_logger": ("pythonjsonlogger",),
    "python_multipart": ("multipart",),
    "pywin32": ("win32api", "win32con", "win32gui", "win32com", "pythoncom", "pywintypes", "win32event", "win32file"),
    "pyyaml": ("yaml",),
    "ruamel_yaml": ("ruamel",),
    "scikit_learn": ("sklearn",),
    "scikit_image": ("skimage",),
    "setuptools": ("pkg_resources",),
    "websocket_client": ("websocket",),
    "pillow": ("pil",),
    "paho_mqtt": ("paho",),
}


@functools.lru_cache(maxsize=128)
def _installed_top_level_imports(dist_name: str) -> tuple[str, ...]:
    """读取当前环境已安装分发的 ``top_level.txt`` 推导入名（未安装/无文件返回空元组）.

    运行时兜底：静态映射表 :data:`_PYPI_IMPORT_ALIASES` 未覆盖的分发
    （如 ``pywin32`` 的 ``win32api`` 等多个导入名），若恰好安装在当前
    构建环境则从其 ``top_level.txt`` 读取顶层导入名。未安装时返回空元组，
    不影响 ``missing`` 判定（仅少一层覆盖）。
    """
    import importlib.metadata as _im

    try:
        dist = _im.distribution(dist_name)
    except (_im.PackageNotFoundError, OSError, ValueError):
        # 未安装 / 元数据损坏 / 非法名：静默降级为无兜底信息
        return ()
    text = dist.read_text("top_level.txt") or ""
    return tuple(line.strip().lower() for line in text.splitlines() if line.strip())


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
        # 延迟导入打破 config ↔ app_type 循环依赖（app_type 顶层导入本模块）
        from fspack.config.app_type import infer_app_type

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
    # 关闭构建结束后的 manifest 生成：默认输出产物清单 JSON
    # 到 dist/release/<name>-<version>-manifest.json
    no_manifest: bool | None = None
    # 关闭构建结束后的 Win7 兼容扫描：默认输出文本报告
    # 到 dist/release/win7-compat-report.txt（仅 Windows 目标）
    no_win7_scan: bool | None = None
    # 关闭 Win7 兼容 DLL 注入：跳过 shim 注入（3.9-3.11）与组件整体替换（3.12+，
    # 需从 GitHub 下载重编译版 embed zip）。产物仅面向 Win8+/Win10+ 时启用，
    # 避免网络受限环境下因 GitHub 下载失败阻断构建
    no_win7_dll: bool | None = None
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


@functools.lru_cache(maxsize=64)
def _project_info_from_dir_cached(
    resolved_dir_str: str,
    py_version: str | None,
    mtime_ns: int,  # noqa: ARG001 - 作为 lru_cache 失效键，函数体内无需访问
) -> ProjectInfo:
    """按 (项目目录, py_version, pyproject.toml mtime) 缓存 ProjectInfo.

    lru_cache 不缓存异常（parse_project 抛错时不会污染缓存），同一目录
    pyproject.toml 未变动时命中缓存，避免二次解析 TOML / AST 扫描。
    maxsize=64 应对 doctor/bench 模式下多模板项目批量构建场景。
    """
    from fspack.config.parsing import parse_project

    return parse_project(Path(resolved_dir_str), py_version)


def _clear_project_info_cache() -> None:
    """清空 ProjectInfo 缓存（测试/调试/变更 pyproject 后手动失效）."""
    _project_info_from_dir_cached.cache_clear()


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
    # 项目描述与作者：取自 [project].description 与 [project].authors[0].name，
    # 用于 Windows loader exe 的 VS_VERSIONINFO 资源段（CompanyName/FileDescription），
    # 资源段完整的 exe 可显著降低 Defender 等杀软启发式可疑度。未声明时为空串。
    description: str = ""
    author: str = ""
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
        """从项目目录解析 pyproject.toml 并构造实例（按 mtime 缓存）.

        读取 ``pyproject.toml`` 的 ``st_mtime_ns`` 作为失效键：同一目录
        文件未变动时直接命中 lru_cache，避免重复 TOML 解析 + AST 扫描。
        文件不存在时 ``mtime_ns=0``，交给 :func:`parse_project` 抛
        :class:`ProjectError`（lru_cache 不缓存异常）。
        """
        resolved = Path(project_dir).resolve()
        try:
            mtime_ns = (resolved / "pyproject.toml").stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        return _project_info_from_dir_cached(str(resolved), py_version, mtime_ns)

    @property
    def exe_name(self) -> str:
        """生成的可执行文件名（多入口模式取默认入口名，与构建侧命名一致）.

        构建侧（compile_stage._loader_exe_path）按入口名命名 exe，多入口项目
        项目名与 exe 名不同；此处取默认入口名保证安装器校验/快捷方式引用与
        构建产物一致。单入口模式 ``all_entries`` 回退构造入口名为项目名，
        行为不变（``<name>.exe``）。
        """
        return f"{self.default_entry.name}.exe"

    @property
    def py_xy(self) -> str:
        """形如 ``python311`` / ``python313t`` 的版本前缀.

        free-threaded build（``py_version`` 末尾 ``t`` 后缀）返回
        ``python313t``，与 ``python313t.dll`` 文件名一致；loader 用本字段
        拼接 ``runtime\\\\{py_xy}.dll`` 路径，必须与 runtime 实际 DLL 名一致。
        """
        # 内联 t 后缀剥离：避免 models → versions 引入新依赖（versions 已被
        # parsing 间接依赖 models，反向依赖会形成循环）
        is_t = self.py_version.endswith("t")
        base = self.py_version[:-1] if is_t else self.py_version
        major, minor = base.split(".")[:2]
        return f"python{major}{minor}{'t' if is_t else ''}"

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
        """AST 发现但未在 pyproject 声明的第三方依赖.

        导入名与 PyPI 分发名不一致（如 ``import yaml`` 对应声明 ``PyYAML``）
        时，依次经名字归一化、静态映射表
        :data:`_PYPI_IMPORT_ALIASES`、当前环境 ``top_level.txt`` 运行时
        兜底三层匹配，命中任一层即视为已声明，不产生误报。

        运行时兜底惰性求值：``importlib.metadata`` 首次导入约 220ms（dry-run
        全链路实测占 ~58%），仅在归一化 + 静态表匹配后仍存在未命中导入、且
        声明中含静态表未覆盖的分发时才触发——常见场景（声明与导入名一致或
        静态表覆盖）零开销。
        """
        declared_top: set[str] = set()
        unresolved_raws: list[str] = []
        for d in self.declared:
            raw = re.split(r"[<>=!~;\[]", d, maxsplit=1)[0].strip()
            if not raw:
                continue
            # PEP 503 语义：分发名中 ``-``/``.``/``_`` 等价（ruamel.yaml 与
            # ruamel_yaml 同一分发），统一归一化为 ``_`` 再查静态表
            norm = raw.replace("-", "_").replace(".", "_").lower()
            declared_top.add(norm)
            aliases = _PYPI_IMPORT_ALIASES.get(norm)
            if aliases is not None:
                declared_top.update(aliases)
            else:
                unresolved_raws.append(raw)
        candidates = [m for m in self.ast_third_party if m.lower() not in declared_top]
        if candidates and unresolved_raws:
            # 仍有未匹配导入且存在静态表未覆盖的声明：当前环境 top_level.txt 兜底
            # （未安装返回空）。兜底只会从 missing 中移除名字，不影响已匹配结果。
            for raw in unresolved_raws:
                declared_top.update(_installed_top_level_imports(raw))
            candidates = [m for m in candidates if m.lower() not in declared_top]
        return tuple(sorted(candidates))


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
    - ``pyc_optimize``：字节码优化级别 0/1/2（解释器 ``-O``/``-OO`` 标志）
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
    # 关闭构建结束后的 manifest 生成：默认输出产物清单 JSON
    no_manifest: bool = False
    # 关闭构建结束后的 Win7 兼容扫描与报告（仅 Windows 目标，loader exe
    # 硬门禁不受此开关影响）
    no_win7_scan: bool = False
    # 关闭 Win7 兼容 DLL 注入（shim 注入 3.9-3.11 + 组件整体替换 3.12+）：
    # 产物仅面向 Win8+/Win10+ 时启用，避免 GitHub 下载失败阻断构建
    no_win7_dll: bool = False
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
        no_manifest=defaults.no_manifest if defaults.no_manifest is not None else base.no_manifest,
        no_win7_scan=defaults.no_win7_scan if defaults.no_win7_scan is not None else base.no_win7_scan,
        no_win7_dll=defaults.no_win7_dll if defaults.no_win7_dll is not None else base.no_win7_dll,
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
