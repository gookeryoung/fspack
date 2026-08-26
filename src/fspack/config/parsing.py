"""pyproject.toml 解析编排与 ``[tool.fspack]`` 配置项解析.

本模块从 :mod:`fspack.config` 抽离，原职责中的入口识别与类型推断已进一步
拆分（本模块 re-export 保持路径兼容）：

- :mod:`fspack.config.entries`：入口脚本识别（``[project.scripts]`` 解析、
  dotted module → 脚本路径、兜底扫描 ``detect_entry``）
- :mod:`fspack.config.app_type`：AppType 推断（``infer_app_type`` 与判定表）

本模块保留：:func:`parse_project` 解析编排、解析缓存（:func:`clear_project_cache`）、
``[tool.fspack]`` 各配置项解析（``_parse_build_defaults``/``_resolve_icon``/
``_parse_exclude_dirs`` 等）与 extras 展开（:func:`expand_extras`）。

依赖 :mod:`fspack.config.models` 提供 dataclass 与 :func:`_parse_string_list_cfg`，
:mod:`fspack.config.versions` 提供默认 Python 版本。

**入口来源与优先级**（识别细节见 :mod:`fspack.config.entries`）：

1. ``[project.scripts]``（PEP 621 标准入口点）
2. ``detect_entry``：无任何入口声明时按文件名兜底扫描

``[tool.fspack.entries]`` 已移除支持：pyproject.toml 中声明该表会报错，
提示迁移到 ``[project.scripts]``。

**解析缓存**：:func:`parse_project` 按 ``(project_dir, py_version, pyproject_mtime_ns)``
缓存解析结果（:func:`_parse_project_cached`），同一项目目录在 pyproject.toml
未修改时复用缓存，避免 ``fsp b``/``fsp p`` 流程内多次调用（cli → pipeline →
installer）重复读取与 AST 扫描。缓存键含 ``mtime_ns``，pyproject.toml 修改后
自动失效；:func:`clear_project_cache` 提供显式清空入口（测试隔离/强制重解析）。
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from fspack._compat import tomllib
from fspack.config.app_type import (  # noqa: F401
    _GUI_HINTS,
    _WEB_HINTS,
    infer_app_type,
)
from fspack.config.entries import (  # noqa: F401
    _has_entry,
    _is_main_check,
    _parse_project_scripts,
    _resolve_module_script,
    detect_entry,
)
from fspack.config.models import (
    BuildDefaults,
    ProjectInfo,
    SlimRules,
    _parse_string_list_cfg,
)
from fspack.config.versions import DEFAULT_PY_VERSION
from fspack.exceptions import ProjectError

__all__ = [
    "clear_project_cache",
    "detect_entry",
    "expand_extras",
    "infer_app_type",
    "parse_project",
]

_logger = logging.getLogger(__name__)

# 缓存上限：64 个不同 (project_dir, py_version, mtime) 组合，覆盖多数项目场景；
# LRU 淘汰最久未用，避免长期运行内存膨胀。
_PROJECT_CACHE_MAXSIZE = 64


# [tool.fspack] 构建默认值键名与 BuildDefaults 字段的映射
_BUILD_DEFAULT_KEYS: dict[str, str] = {
    "nuitka": "nuitka",
    "pyc_strip": "pyc_strip",
    "pyc_optimize": "pyc_optimize",
    "no_site": "no_site",
    "no_pyc": "no_pyc",
    "no_stdlib_trim": "no_stdlib_trim",
    "no_slim_runtime": "no_slim_runtime",
    "no_stdlib_zip": "no_stdlib_zip",
    "splash": "splash",
    "ccache": "ccache",
    "no_size_report": "no_size_report",
    "analyze_deps": "analyze_deps",
    "require_hashes": "require_hashes",
    "no_sbom": "no_sbom",
    "no_manifest": "no_manifest",
    "no_win7_scan": "no_win7_scan",
    "no_win7_dll": "no_win7_dll",
    "open_browser": "open_browser",
}


def parse_project(project_dir: Path, py_version: str | None = None) -> ProjectInfo:
    """解析 pyproject.toml 并识别入口，返回项目元信息.

    入口来源与优先级（详见模块 docstring）：

    1. ``[project.scripts]``（PEP 621）：``name = "module:function"``，
       自动识别 flat/src layout 解析 dotted module 为脚本路径。
    2. ``detect_entry``：无任何入口声明时按文件名兜底扫描。

    ``[tool.fspack.entries]`` 已移除支持：声明该表时报 ``ProjectError``，
    提示迁移到 ``[project.scripts]``。

    声明多入口时，``ProjectInfo.entries`` 非空，``entry_module``/
    ``entry_file``/``app_type`` 取首个入口（保持向后兼容）。

    解析结果按 ``(project_dir, py_version, pyproject.toml mtime)`` 缓存（最多 64
    个条目），同一项目在 pyproject.toml 未修改时复用缓存，避免 ``fsp b``/
    ``fsp p`` 流程内多次调用重复读取与 AST 扫描。pyproject.toml 修改后 mtime
    变化，下次调用自动获取新值。
    """
    project_dir = Path(project_dir).resolve()
    pp = project_dir / "pyproject.toml"
    if not pp.is_file():
        raise ProjectError(f"未找到 pyproject.toml: {pp}")
    # 用 mtime_ns 作为缓存键：分辨率纳秒级，覆盖秒级与亚秒级修改；
    # 文件被 touch 但内容未改也会失效，但这是可接受的过度失效（缓存重建成本低）
    mtime_ns = pp.stat().st_mtime_ns
    return _parse_project_cached(project_dir, py_version, mtime_ns)


@lru_cache(maxsize=_PROJECT_CACHE_MAXSIZE)
def _parse_project_cached(
    project_dir: Path,
    py_version: str | None,
    pyproject_mtime_ns: int,  # noqa: ARG001 — 仅作缓存键，函数内不读取（避免重复 stat）
) -> ProjectInfo:
    """缓存版项目解析：实际读取 pyproject.toml + AST 识别入口.

    缓存键含 ``pyproject_mtime_ns``，文件修改后 mtime 变化触发新解析。
    ``project_dir`` 已在 :func:`parse_project` 中 resolve，此处不再重复。

    ``pyproject_mtime_ns`` 仅作缓存键，函数内不读取该参数（避免重复 stat）。
    """
    pp = project_dir / "pyproject.toml"
    try:
        raw_text = pp.read_text(encoding="utf-8")
    except OSError as e:
        raise ProjectError(f"pyproject.toml 读取失败: {e}") from e
    except UnicodeDecodeError as e:
        # pyproject.toml 非 UTF-8 编码（如 GBK/UTF-16 或含非法字节）：包装为
        # ProjectError 输出清晰的中文错误，而非抛出原始 UnicodeDecodeError 栈。
        raise ProjectError(f"pyproject.toml 编码错误（需 UTF-8）: {e}") from e
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        raise ProjectError(f"pyproject.toml 语法错误: {e}") from e

    proj = data.get("project", {})
    if not isinstance(proj, dict):
        raise ProjectError("pyproject.toml [project] 节格式异常")
    name = str(proj.get("name") or project_dir.name)
    version = str(proj.get("version", "0.0.0"))
    # dependencies 须为字符串列表：标量字符串会被逐字符迭代成 ("r","i","c","h")，
    # 非 list 时报错（与 _parse_optional_dependencies 的校验风格一致）
    raw_deps = proj.get("dependencies", [])
    if not isinstance(raw_deps, list):
        raise ProjectError(f"[project] dependencies 必须是字符串列表，得到 {type(raw_deps).__name__}")
    deps = tuple(str(d) for d in raw_deps)
    requires_python = str(proj.get("requires-python") or "") or None
    # [project].description 与 [project].authors[0].name：用于 Windows loader exe
    # 的 VS_VERSIONINFO 资源段（FileDescription/CompanyName），降低杀软启发式可疑度。
    # authors 取 PEP 621 列表首项的 name（dict 形式）或首项字符串（裸字符串形式）。
    description = str(proj.get("description") or "")
    author = _parse_author(proj.get("authors"))
    # [project.optional-dependencies] 全部分组：extra_name → 依赖声明元组
    optional_deps = _parse_optional_dependencies(proj.get("optional-dependencies"))

    tool: dict[str, Any] = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
    fspack_cfg: dict[str, Any] = tool.get("fspack", {}) if isinstance(tool.get("fspack"), dict) else {}
    # [tool.fspack.entries] 已移除支持：声明该表时报错提示迁移，
    # 避免用户按旧文档声明后静默失效（不再是"不再误导"的静默忽略）
    if "entries" in fspack_cfg:
        raise ProjectError(
            "[tool.fspack.entries] 已移除支持，请改用 [project.scripts] 声明入口："
            '如 cli = "cli:main"（模块名:函数名，模块按 flat/src layout 解析）'
        )
    # [project.scripts] PEP 621 标准入口点：name = "module:function"
    scripts_tbl: dict[str, Any] = proj.get("scripts", {}) if isinstance(proj.get("scripts"), dict) else {}
    icon_rel = fspack_cfg.get("icon")
    icon_path = _resolve_icon(project_dir, icon_rel)

    # [tool.fspack] exclude：额外排除目录/文件模式（合并到 copy_source 内置 _EXCLUDE）
    exclude_dirs = _parse_exclude_dirs(fspack_cfg.get("exclude"))
    # [tool.fspack] data-dirs：原样保留的数据资源目录树（相对项目目录的 POSIX 路径），
    # copy_source 对其跳过元数据/文档排除，_strip_py_sources 跳过其下 .py 剥离。
    data_dirs = _parse_data_dirs(fspack_cfg.get("data-dirs"))
    # [tool.fspack] web-static-dirs：前端构建产物目录（相对项目目录的 POSIX 路径，
    # 如 "dist"），与 data-dirs 同等保护，且 wrapper 在打包时解析为 dist 内绝对
    # 路径注入 Flask static_folder / FastAPI StaticFiles serve。
    web_static_dirs = _parse_web_static_dirs(fspack_cfg.get("web-static-dirs"))
    # [tool.fspack] 构建默认值：CLI 标志覆盖
    build_defaults = _parse_build_defaults(fspack_cfg)
    # [tool.fspack] 私有包源：extra-index-urls / find-links 透传给 pip/uv
    extra_index_urls = _parse_string_list_cfg(fspack_cfg.get("extra-index-urls"), "extra-index-urls")
    find_links = _parse_string_list_cfg(fspack_cfg.get("find-links"), "find-links")
    # [tool.fspack] wheel 精简用户规则
    slim_rules = SlimRules.from_config(fspack_cfg)

    # [project.scripts]（PEP 621）：dotted module → 脚本路径，
    # Python 字典保持插入序，首个入口作为主入口。
    if scripts_tbl:
        merged = _parse_project_scripts(project_dir, scripts_tbl)
        if merged:
            first = merged[0]
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
                description=description,
                author=author,
                entries=merged,
                icon=icon_path,
                exclude_dirs=exclude_dirs,
                data_dirs=data_dirs,
                web_static_dirs=web_static_dirs,
                build_defaults=build_defaults,
                extra_index_urls=extra_index_urls,
                find_links=find_links,
                slim_rules=slim_rules,
                optional_dependencies=optional_deps,
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
        description=description,
        author=author,
        icon=icon_path,
        exclude_dirs=exclude_dirs,
        data_dirs=data_dirs,
        web_static_dirs=web_static_dirs,
        build_defaults=build_defaults,
        extra_index_urls=extra_index_urls,
        find_links=find_links,
        slim_rules=slim_rules,
        optional_dependencies=optional_deps,
    )


def clear_project_cache() -> None:
    """清空 :func:`parse_project` 的双层解析缓存.

    ProjectInfo 解析有两层缓存，均在此清空：

    - 内层：本模块 :func:`_parse_project_cached`（parse_project 直连调用方）
    - 外层：:func:`fspack.config.models._project_info_from_dir_cached`
      （``ProjectInfo.from_dir`` 按 mtime 键控），仅清内层会导致 ``from_dir``
      命中外层旧值，"清缓存后重解析"失效

    用于：

    - 测试隔离：测试间避免缓存污染（不同测试用不同 tmp_path 通常天然隔离，
      但同测试内修改 pyproject.toml 后强制重解析需显式清空）
    - 强制重解析：外部进程修改 pyproject.toml 后 mtime 未变（极少见）时
      手动清空缓存
    - 内存管理：长期运行进程主动释放缓存条目
    """
    _parse_project_cached.cache_clear()
    # 延迟导入避免 models ↔ parsing 模块级循环（models.from_dir 已延迟导入本模块）
    from fspack.config.models import _clear_project_info_cache

    _clear_project_info_cache()


def _parse_author(authors: object) -> str:
    """从 PEP 621 ``[project].authors`` 提取首位作者名，用于 VS_VERSIONINFO 资源段.

    支持两种声明形式：

    - dict 形式（规范）：``authors = [{ name = "张三", email = "..." }]``，取 ``name`` 字段
    - 裸字符串形式：``authors = ["张三"]``，取首项字符串

    非列表、空列表、首项无 ``name`` 字段时返回空串（资源段对应字段留空，
    不影响 exe 编译，仅资源信息不完整）。
    """
    if not isinstance(authors, list) or not authors:
        return ""
    first = authors[0]
    if isinstance(first, dict):
        return str(first.get("name") or "")
    if isinstance(first, str):
        return first
    return ""


def _parse_exclude_dirs(value: object) -> tuple[str, ...]:
    """解析 ``[tool.fspack] exclude`` 配置为排除模式元组（空元素报错）."""
    return _parse_string_list_cfg(value, "exclude", reject_empty=True)


def _parse_data_dirs(value: object) -> tuple[str, ...]:
    """解析 ``[tool.fspack] data-dirs`` 配置为目录路径元组（空元素报错）。

    路径为相对项目目录的 POSIX 风格字符串（如 ``src/fspack/assets/templates``），
    运行时由 :func:`copy_source`/``_strip_py_sources`` 解析为绝对路径并据此跳过
    元数据/文档排除与 ``.py`` 剥离。空列表表示无数据资源目录（默认行为不变）。
    """
    return _parse_string_list_cfg(value, "data-dirs", reject_empty=True)


def _parse_web_static_dirs(value: object) -> tuple[str, ...]:
    """解析 ``[tool.fspack] web-static-dirs`` 配置为目录路径元组（空元素报错）。

    路径为相对项目目录的 POSIX 风格字符串（如 ``dist``），运行时由
    :func:`copy_source`/``_strip_py_sources`` 与 ``data-dirs`` 同等跳过元数据/文档
    排除与 ``.py`` 剥离，并由 :class:`EntryWrapper` 在打包时解析为 dist 内绝对
    路径注入 Flask ``static_folder`` / FastAPI ``StaticFiles`` serve。空列表
    表示无前端构建产物（仅 ``AppType.WEB`` 项目使用）。
    """
    return _parse_string_list_cfg(value, "web-static-dirs", reject_empty=True)


def _parse_optional_dependencies(value: object) -> dict[str, tuple[str, ...]]:
    """解析 ``[project.optional-dependencies]`` 为 ``{extra_name: deps}`` 字典.

    PEP 621 规定该字段为表，键为分组名（非空字符串），值为依赖声明字符串列表
    （含版本约束与环境标记）。fspack 仅做结构与类型校验，不解析依赖本身——
    自引用 ``"my-pkg[extra1,extra2]"`` 由 :func:`_expand_extras` 在合并阶段展开。

    Args:
        value: ``proj.get("optional-dependencies")`` 原始值（dict 或 None）

    Returns:
        ``{extra_name: (dep1, dep2, ...)}`` 字典；``value`` 为 None 时返回空字典

    Raises:
        ProjectError: ``value`` 非 dict、分组名非空字符串、依赖列表非字符串列表
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProjectError("[project.optional-dependencies] 必须是表")
    result: dict[str, tuple[str, ...]] = {}
    for extra_name, dep_list in value.items():
        if not isinstance(extra_name, str) or not extra_name.strip():
            raise ProjectError(f"[project.optional-dependencies] 分组名无效: {extra_name!r}")
        if not isinstance(dep_list, list) or not all(isinstance(x, str) for x in dep_list):
            raise ProjectError(
                f"[project.optional-dependencies] {extra_name} 必须是字符串列表，得到 {type(dep_list).__name__}"
            )
        result[extra_name] = tuple(dep_list)
    return result


def _parse_build_defaults(fspack_cfg: dict[str, Any]) -> BuildDefaults:  # noqa: PLR0912
    """从 ``[tool.fspack]`` 解析构建默认值.

    识别 ``nuitka``/``pyc_strip``/``pyc_optimize``/``no_site``/``no_pyc``/
    ``no_stdlib_trim``/``no_slim_runtime``/``ccache``/``no_size_report``/``analyze_deps``/
    ``nuitka_packages``/``extras``/``lazy_imports`` 键，其余键忽略（如 ``icon``/``entries``/``exclude``）。
    类型不匹配时报错，避免静默忽略错误配置。
    """
    kwargs: dict[str, bool | int | str | None | tuple[str, ...]] = {}
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
    # nuitka_packages 为字符串列表，单独解析
    raw_pkgs = fspack_cfg.get("nuitka_packages")
    if raw_pkgs is not None:
        if not isinstance(raw_pkgs, list) or not all(isinstance(x, str) for x in raw_pkgs):
            raise ProjectError(f"[tool.fspack] nuitka_packages 必须是字符串列表，得到 {raw_pkgs!r}")
        kwargs["nuitka_packages"] = tuple(raw_pkgs)
    # slim-stdlib 为枚举字符串：Windows embed stdlib zip 重写档位
    raw_slim = fspack_cfg.get("slim-stdlib")
    if raw_slim is not None:
        if not isinstance(raw_slim, str) or raw_slim.strip() not in ("default", "aggressive"):
            raise ProjectError(f"[tool.fspack] slim-stdlib 必须是 default 或 aggressive，得到 {raw_slim!r}")
        kwargs["slim_stdlib"] = raw_slim.strip()
    # compiler 为枚举字符串：Windows Nuitka 编译器选择（auto/msvc/mingw）
    raw_compiler = fspack_cfg.get("compiler")
    if raw_compiler is not None:
        if not isinstance(raw_compiler, str) or raw_compiler.strip() not in ("auto", "msvc", "mingw"):
            raise ProjectError(f"[tool.fspack] compiler 必须是 auto、msvc 或 mingw，得到 {raw_compiler!r}")
        kwargs["compiler"] = raw_compiler.strip()
    # extras 为字符串列表：默认启用的 optional-dependencies 分组名
    kwargs["extras"] = _parse_string_list_cfg(fspack_cfg.get("extras"), "extras", reject_empty=True)
    # lazy_imports 为字符串列表：延迟导入的顶层模块名
    kwargs["lazy_imports"] = _parse_string_list_cfg(fspack_cfg.get("lazy_imports"), "lazy_imports", reject_empty=True)
    # 安全加固：签名证书/密码/密钥 ID 为字符串配置（非布尔开关）
    sign_exe_cert = fspack_cfg.get("sign-exe-certificate")
    if sign_exe_cert is not None:
        if not isinstance(sign_exe_cert, str) or not sign_exe_cert.strip():
            raise ProjectError(f"[tool.fspack] sign-exe-certificate 必须是非空字符串，得到 {sign_exe_cert!r}")
        kwargs["sign_exe_certificate"] = sign_exe_cert.strip()
    sign_exe_pwd = fspack_cfg.get("sign-exe-password")
    if sign_exe_pwd is not None:
        if not isinstance(sign_exe_pwd, str):
            raise ProjectError(f"[tool.fspack] sign-exe-password 必须是字符串，得到 {sign_exe_pwd!r}")
        kwargs["sign_exe_password"] = sign_exe_pwd
    sign_deb_key = fspack_cfg.get("sign-deb-key")
    if sign_deb_key is not None:
        if not isinstance(sign_deb_key, str) or not sign_deb_key.strip():
            raise ProjectError(f"[tool.fspack] sign-deb-key 必须是非空字符串，得到 {sign_deb_key!r}")
        kwargs["sign_deb_key"] = sign_deb_key.strip()
    return BuildDefaults(**cast(Any, kwargs))


def expand_extras(
    base_deps: tuple[str, ...],
    optional_deps: dict[str, tuple[str, ...]],
    enabled_extras: frozenset[str],
    project_name: str,
) -> tuple[str, ...]:
    """合并 ``base_deps`` 与启用的 extras 依赖，展开自引用 ``"my-pkg[extra]"``.

    处理三类依赖声明（PEP 631 + pip 行为）：

    1. **自引用** ``"my-pkg[extra1,extra2]"``：当依赖名（归一化后）等于项目名时，
       递归展开 ``optional_deps[extra1]`` + ``optional_deps[extra2]``。
    2. **第三方 extras** ``"other-pkg[extra]"``：原样保留，由 pip 解析。
    3. **普通依赖** ``"rich>=13"``：原样保留。

    enabled_extras 中的分组直接展开其依赖列表，等价 ``pip install pkg[extra]`` 语义。
    循环保护：已展开的 extra 名记入 ``visited``，重复出现跳过（PEP 631 未规定循环检测，
    pip 自身也不检测，但 fspack 用 visited 避免无限递归）。

    Args:
        base_deps: ``[project] dependencies`` 依赖声明元组
        optional_deps: ``[project.optional-dependencies]`` 全部分组
        enabled_extras: 启用的分组名集合（来自 CLI --extra 或配置默认）
        project_name: 项目名（用于识别自引用，按 ``lower().replace("-", "_")`` 归一化）

    Returns:
        合并去重后的依赖元组（保留首次出现顺序）：base_deps + enabled extras 展开 +
        自引用展开；未知 enabled_extras 抛错

    Raises:
        ProjectError: ``enabled_extras`` 含 ``optional_deps`` 中不存在的分组名
    """
    unknown = enabled_extras - set(optional_deps)
    if unknown:
        raise ProjectError(f"未知的 extras 分组: {sorted(unknown)}，可选: {sorted(optional_deps)}")

    proj_norm = project_name.lower().replace("-", "_")
    merged: list[str] = []
    seen: set[str] = set()

    def _add_dep(dep: str) -> None:
        """添加依赖到 merged（按原始字符串去重，保留首次出现顺序）."""
        if dep not in seen:
            seen.add(dep)
            merged.append(dep)

    def _expand_dep_list(deps: tuple[str, ...], visited: frozenset[str]) -> None:
        """递归展开依赖列表，处理自引用 ``"my-pkg[extra1,extra2]"``.

        ``visited`` 记录已展开的 extra 名，循环引用时跳过避免无限递归。
        """
        # 依赖规范正则：name[extras]后跟可选版本约束与环境标记
        # 例："rich>=13; python_version<'3.11'" → name="rich", extras=None
        #      "my-pkg[gui,web]>=1.0" → name="my-pkg", extras="gui,web"
        #      "pandas[performance]" → name="pandas", extras="performance"
        dep_re = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[([^\]]+)\])?")
        for dep in deps:
            m = dep_re.match(dep)
            if not m:
                _add_dep(dep)
                continue
            pkg_name = m.group(1)
            extras_str = m.group(2)
            if extras_str is None:
                _add_dep(dep)
                continue
            extra_list = [e.strip() for e in extras_str.split(",") if e.strip()]
            pkg_norm = pkg_name.lower().replace("-", "_")
            if pkg_norm == proj_norm:
                # 自引用：递归展开 optional_deps[extra] 的依赖
                for extra in extra_list:
                    if extra in visited:
                        continue
                    sub_deps = optional_deps.get(extra)
                    if sub_deps is None:
                        # 自引用了不存在的 extra：跳过（pip 行为，构建期报错更友好）
                        continue
                    _expand_dep_list(sub_deps, visited | {extra})
            else:
                # 第三方 extras：原样保留
                _add_dep(dep)

    # 先展开 base_deps（处理其中的自引用）
    _expand_dep_list(base_deps, frozenset())
    # 再展开 enabled extras 的依赖
    for extra in enabled_extras:
        # enabled_extras 已校验过存在性
        _expand_dep_list(optional_deps[extra], frozenset({extra}))
    return tuple(merged)


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
    return icon_path
