"""pyproject.toml 解析与项目入口识别.

本模块从 :mod:`fspack.config` 抽离，含 :func:`parse_project` 入口解析流程、
``[tool.fspack]`` 配置项解析、入口脚本 AST 识别与 app 类型推断。
``config.py`` 通过 re-export 保持公开 API 不变。

依赖 :mod:`fspack.config.models` 提供 dataclass 与 :func:`_parse_string_list_cfg`，
:mod:`fspack.config.versions` 提供默认 Python 版本，:mod:`fspack.analyzer`
在 :func:`infer_app_type` 中延迟导入打破循环依赖。

**入口来源与优先级**：

1. ``[project.scripts]``（PEP 621 标准入口点）：``name = "module:function"``，
   ``module`` 为 dotted 模块路径（如 ``fspack.cli``），``function`` 被忽略
   （fspack 用 :func:`runpy.run_path`/``run_module`` 运行整个模块）。
   自动识别 flat layout（``<project>/<pkg>/``）与 src layout
   （``<project>/src/<pkg>/``），将 dotted module 解析为脚本文件路径。
2. ``[tool.fspack.entries]``：``name = "script_rel"``，值为脚本相对项目目录
   的路径（POSIX 风格）。优先级高于 ``[project.scripts]``，同名入口以
   ``[tool.fspack.entries]`` 为准覆盖。
3. ``detect_entry``：无任何入口声明时，按 ``<name>.py``/``<name>/__main__.py``
   /顶层 ``*.py`` 兜底扫描，识别含 ``def main()`` 或
   ``if __name__ == "__main__"`` 的脚本。

**解析缓存**：:func:`parse_project` 按 ``(project_dir, py_version, pyproject_mtime_ns)``
缓存解析结果（:func:`_parse_project_cached`），同一项目目录在 pyproject.toml
未修改时复用缓存，避免 ``fsp b``/``fsp p`` 流程内多次调用（cli → pipeline →
installer）重复读取与 AST 扫描。缓存键含 ``mtime_ns``，pyproject.toml 修改后
自动失效；:func:`clear_project_cache` 提供显式清空入口（测试隔离/强制重解析）。
"""

from __future__ import annotations

import ast
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from fspack.config.models import (
    AppType,
    BuildDefaults,
    EntryPoint,
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

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise ProjectError("解析 pyproject.toml 需要 tomli（Python<3.11），请安装 tomli") from e


# [tool.fspack] 构建默认值键名与 BuildDefaults 字段的映射
_BUILD_DEFAULT_KEYS: dict[str, str] = {
    "nuitka": "nuitka",
    "pyc_strip": "pyc_strip",
    "pyc_optimize": "pyc_optimize",
    "no_site": "no_site",
    "no_pyc": "no_pyc",
    "no_stdlib_trim": "no_stdlib_trim",
    "no_slim_runtime": "no_slim_runtime",
    "ccache": "ccache",
    "no_size_report": "no_size_report",
    "analyze_deps": "analyze_deps",
    "require_hashes": "require_hashes",
    "no_sbom": "no_sbom",
}

# GUI 框架导入名集合：用于按入口脚本 import 推断 AppType
_GUI_HINTS = frozenset({"tkinter", "PySide2", "PySide6", "PyQt5", "PyQt6", "matplotlib", "wx", "win32gui", "pygame"})


def parse_project(project_dir: Path, py_version: str | None = None) -> ProjectInfo:
    """解析 pyproject.toml 并识别入口，返回项目元信息.

    入口来源与优先级（详见模块 docstring）：

    1. ``[project.scripts]``（PEP 621）：``name = "module:function"``，
       自动识别 flat/src layout 解析 dotted module 为脚本路径。
    2. ``[tool.fspack.entries]``：``name = "script_rel"``，相对项目目录的路径。
       同名入口覆盖 ``[project.scripts]``。
    3. ``detect_entry``：无任何入口声明时按文件名兜底扫描。

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
    # [project.optional-dependencies] 全部分组：extra_name → 依赖声明元组
    optional_deps = _parse_optional_dependencies(proj.get("optional-dependencies"))

    tool: dict[str, Any] = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
    fspack_cfg: dict[str, Any] = tool.get("fspack", {}) if isinstance(tool.get("fspack"), dict) else {}
    entries_tbl: dict[str, Any] = fspack_cfg.get("entries", {}) if isinstance(fspack_cfg.get("entries"), dict) else {}
    # [project.scripts] PEP 621 标准入口点：name = "module:function"
    scripts_tbl: dict[str, Any] = proj.get("scripts", {}) if isinstance(proj.get("scripts"), dict) else {}
    icon_rel = fspack_cfg.get("icon")
    icon_path = _resolve_icon(project_dir, icon_rel)

    # [tool.fspack] exclude：额外排除目录/文件模式（合并到 copy_source 内置 _EXCLUDE）
    exclude_dirs = _parse_exclude_dirs(fspack_cfg.get("exclude"))
    # [tool.fspack] data-dirs：原样保留的数据资源目录树（相对项目目录的 POSIX 路径），
    # copy_source 对其跳过元数据/文档排除，_strip_py_sources 跳过其下 .py 剥离。
    data_dirs = _parse_data_dirs(fspack_cfg.get("data-dirs"))
    # [tool.fspack] 构建默认值：CLI 标志覆盖
    build_defaults = _parse_build_defaults(fspack_cfg)
    # [tool.fspack] 私有包源：extra-index-urls / find-links 透传给 pip/uv
    extra_index_urls = _parse_string_list_cfg(fspack_cfg.get("extra-index-urls"), "extra-index-urls")
    find_links = _parse_string_list_cfg(fspack_cfg.get("find-links"), "find-links")
    # [tool.fspack] wheel 精简用户规则
    slim_rules = SlimRules.from_config(fspack_cfg)

    # 合并 [project.scripts] 与 [tool.fspack.entries]：
    # 先解析 [project.scripts]（dotted module → 脚本路径），
    # 再用 [tool.fspack.entries] 覆盖同名入口（fspack 优先级更高）。
    # Python 3.7+ dict 保序，按 scripts → fspack entries 顺序去重保留首次出现位置。
    if scripts_tbl or entries_tbl:
        scripts_entries = _parse_project_scripts(project_dir, scripts_tbl) if scripts_tbl else ()
        fspack_entries = _parse_entries(project_dir, entries_tbl) if entries_tbl else ()
        merged = _merge_entries(scripts_entries, fspack_entries)
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
                entries=merged,
                icon=icon_path,
                exclude_dirs=exclude_dirs,
                data_dirs=data_dirs,
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
        icon=icon_path,
        exclude_dirs=exclude_dirs,
        data_dirs=data_dirs,
        build_defaults=build_defaults,
        extra_index_urls=extra_index_urls,
        find_links=find_links,
        slim_rules=slim_rules,
        optional_dependencies=optional_deps,
    )


def clear_project_cache() -> None:
    """清空 :func:`parse_project` 的解析缓存.

    用于：

    - 测试隔离：测试间避免缓存污染（不同测试用不同 tmp_path 通常天然隔离，
      但同测试内修改 pyproject.toml 后强制重解析需显式清空）
    - 强制重解析：外部进程修改 pyproject.toml 后 mtime 未变（极少见）时
      手动清空缓存
    - 内存管理：长期运行进程主动释放缓存条目
    """
    _parse_project_cached.cache_clear()


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


def _parse_project_scripts(
    project_dir: Path,
    scripts_tbl: dict[str, Any],
) -> tuple[EntryPoint, ...]:
    """解析 ``[project.scripts]`` 表（PEP 621）为 EntryPoint 元组.

    PEP 621 入口点格式：``name = "module:function"``，其中：

    - ``name``：可执行文件名（用作 exe 名）。
    - ``module``：dotted 模块路径（如 ``fspack.cli``、``cli``），fspack 将其
      解析为脚本文件路径。``function`` 部分被忽略——fspack 用
      :func:`runpy.run_path`/``run_module`` 运行整个模块而非调用特定函数。
    - ``function``：入口函数名（如 ``main``），仅作元数据保留，运行时不使用。

    项目 layout 自动识别（按优先级尝试，首个命中即用）：

    - **flat layout**：``<project>/<pkg>/...`` 或 ``<project>/<name>.py``。
    - **src layout**：``<project>/src/<pkg>/...`` 或 ``<project>/src/<name>.py``。

    dotted module 到文件路径的映射规则：

    - 多段（``fspack.cli``）：``<pkg>/cli.py``（flat）或 ``src/<pkg>/cli.py``（src）。
    - 单段（``fspack``）：``fspack.py`` 或 ``fspack/__main__.py``
      （flat），``src/fspack.py`` 或 ``src/fspack/__main__.py``（src）。

    键为入口名（须为非空字符串），值须为 ``"module:function"`` 格式字符串。
    缺少 ``:function`` 时视整段为 module（向后兼容纯模块名写法）。
    Python 字典保持插入序，首个入口作为主入口（保持向后兼容）。
    """
    if not scripts_tbl:
        raise ProjectError("[project.scripts] 为空，请删除该表或至少声明一个入口")
    entries: list[EntryPoint] = []
    for entry_name, spec in scripts_tbl.items():
        if not isinstance(entry_name, str) or not entry_name:
            raise ProjectError(f"[project.scripts] 入口名无效: {entry_name!r}")
        if not isinstance(spec, str) or not spec.strip():
            raise ProjectError(f"[project.scripts] {entry_name} 的入口规范为空")
        # PEP 621: "module:function"，function 可省略（纯模块名也接受）
        module_part = spec.split(":", 1)[0].strip()
        if not module_part:
            raise ProjectError(f"[project.scripts] {entry_name} 的模块名无效: {spec!r}")
        script_path = _resolve_module_script(project_dir, module_part)
        if script_path is None:
            raise ProjectError(
                f"[project.scripts] {entry_name} 的模块 {module_part!r} 未找到对应脚本（已尝试 flat 与 src layout）"
            )
        entries.append(EntryPoint.from_script(entry_name, script_path))
    return tuple(entries)


def _resolve_module_script(project_dir: Path, module_dotted: str) -> Path | None:
    """将 dotted 模块名解析为脚本文件绝对路径，自动识别 flat/src layout.

    查找规则（按优先级尝试，首个命中即返回）：

    1. **flat layout**：在 ``project_dir`` 下查找
       - 多段 ``a.b`` → ``<project>/a/b.py``
       - 单段 ``a`` → ``<project>/a.py`` 或 ``<project>/a/__main__.py``
    2. **src layout**：在 ``project_dir/src`` 下重复上述查找

    所有候选路径都不存在时返回 ``None``，由调用方决定报错或回退。

    单段 module 优先 ``a.py``（顶层脚本），再 ``a/__main__.py``（包入口），
    与 :func:`detect_entry` 的优先级一致。
    """
    parts = module_dotted.split(".")
    # 多段 → <...>/a/b.py；单段 → a.py 或 a/__main__.py
    rel_candidates: list[Path] = []
    if len(parts) >= 2:
        rel_candidates.append(Path(*parts).with_suffix(".py"))
    else:
        first = parts[0]
        rel_candidates.append(Path(f"{first}.py"))
        rel_candidates.append(Path(first, "__main__.py"))

    for base in (project_dir, project_dir / "src"):
        for rel in rel_candidates:
            candidate = (base / rel).resolve()
            if candidate.is_file():
                return candidate
    return None


def _merge_entries(
    scripts_entries: tuple[EntryPoint, ...],
    fspack_entries: tuple[EntryPoint, ...],
) -> tuple[EntryPoint, ...]:
    """合并两个入口元组，``fspack_entries`` 覆盖 ``scripts_entries`` 同名入口.

    合并顺序：先 ``scripts_entries``（保持原序），再追加 ``fspack_entries``
    中未在 scripts 出现的新入口。同名入口（按 ``name`` 比较）取 ``fspack_entries``
    的版本（fspack 优先级更高，符合"重复定义以 fspack 为准"语义）。

    返回合并后的 EntryPoint 元组，保留各来源的插入序。
    """
    if not scripts_entries:
        return fspack_entries
    if not fspack_entries:
        return scripts_entries
    fspack_by_name = {ep.name: ep for ep in fspack_entries}
    fspack_only_names = set(fspack_by_name)
    merged: list[EntryPoint] = []
    for ep in scripts_entries:
        if ep.name in fspack_by_name:
            merged.append(fspack_by_name[ep.name])
            fspack_only_names.discard(ep.name)
        else:
            merged.append(ep)
    # 追加 fspack 独有的入口（保持 fspack entries 原序）
    for ep in fspack_entries:
        if ep.name in fspack_only_names:
            merged.append(ep)
    return tuple(merged)


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
