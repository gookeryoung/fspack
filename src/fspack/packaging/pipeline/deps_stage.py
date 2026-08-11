"""依赖阶段：AST 依赖分析 + wheel 下载解压 + 依赖指纹缓存.

职责单一化：仅处理依赖相关的阶段，不涉及源码编译、loader 生成等其他流程。

- :func:`_analyze_dependencies`：AST 分析（指纹缓存命中则跳过）
- :func:`_download_dependencies`：tkinter 补充 + wheel 下载 + 精简解压
- :func:`_dep_cache_path` / :func:`_dep_cache_load` / :func:`_dep_cache_save`：
  指纹缓存（JSON 文件），重复构建加速 ~478ms
- :func:`_site_packages_has_deps` / :func:`_strip_version_specifier`：辅助判断
- :func:`unpack_wheels`：调用 slim_unpack（顶层包装便于测试 patch）
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from fspack._util.fsutil import atomic_write_text
from fspack.config import (
    DEFAULT_SLIM_RULES,
    DependencyReport,
    SlimRules,
    cache_root,
)
from fspack.packaging.builtin import TkinterBundler as _DefaultTkinterBundler
from fspack.packaging.site_packages import normalize_pkg_name as _normalize_pkg_name
from fspack.packaging.wheels import download_wheels as _default_download_wheels
from fspack.platform import wheel_platform_tags

from .context import BuildContext, fspack_wheel_cache_dir

if TYPE_CHECKING:
    from fspack.progress import StageRecorder

__all__ = [
    "_analyze_dependencies",
    "_dep_cache_load",
    "_dep_cache_path",
    "_dep_cache_save",
    "_download_dependencies",
    "_site_packages_has_deps",
    "_strip_version_specifier",
    "unpack_wheels",
]

_logger = logging.getLogger(__name__)

# 延迟持有 stages 模块引用，避免顶层循环 import
_stages_mod_holder: list[Any] = [None]


def _S(fn_name: str, fallback_fn: Callable[..., Any]) -> Callable[..., Any]:
    """运行时从 :mod:`stages` 模块动态取 ``fn_name``，fallback 到默认实现.

    兼容测试 patch ``fspack.packaging.pipeline.stages.download_wheels`` /
    ``stages.unpack_wheels``。
    """
    mod = _stages_mod_holder[0]
    if mod is None:
        try:
            from fspack.packaging.pipeline import stages as _stages_mod

            mod = _stages_mod
            _stages_mod_holder[0] = mod
        except ImportError:
            return fallback_fn
    return getattr(mod, fn_name, fallback_fn)


def _analyze_dependencies(ctx: BuildContext, *, save_cache: bool = True) -> DependencyReport:
    """分析依赖（源码指纹缓存命中则跳过 AST 扫描）.

    ``save_cache=False`` 时跳过缓存写入（用于 ``--dry-run`` 模式，避免创建
    ``dist/.dep_cache.json`` 触发 dist 目录创建）。

    extras 依赖合并：``ctx.opts.extras`` 指定的 ``[project.optional-dependencies]``
    分组经 :func:`fspack.config.expand_extras` 展开后与 ``ctx.info.dependencies``
    合并，作为 ``declared`` 传入依赖分析。自引用 ``"my-pkg[extra]"`` 递归展开，
    第三方 ``"pkg[extra]"`` 原样保留交给 pip。缓存键含 declared，extras 变化时
    缓存自动失效。
    """
    project_dir = ctx.cfg.project_dir
    with ctx.tracker.stage("分析依赖") as st:
        # 源码指纹缓存：源码未变时跳过 AST 分析，重复构建加速 ~478ms
        from fspack.analyzer import source_fingerprint
        from fspack.config import expand_extras

        # 合并 base deps 与 enabled extras（展开自引用）
        expanded_deps = expand_extras(
            ctx.info.dependencies,
            ctx.info.optional_dependencies,
            ctx.opts.extras,
            ctx.info.name,
        )
        fingerprint = source_fingerprint(project_dir, ctx.info.data_dirs)
        report = _dep_cache_load(ctx.cfg.dist_dir, fingerprint, expanded_deps)
        if report is not None:
            st.hit_cache()
            ast_count = len(report.ast_third_party)
            st.set_detail(f"缓存命中，AST {ast_count} 个第三方")
        else:
            report = DependencyReport.from_src(project_dir, ctx.info.name, expanded_deps, ctx.info.data_dirs)
            if save_cache:
                _dep_cache_save(ctx.cfg.dist_dir, fingerprint, report)
            if report.missing:
                _logger.info("AST 发现未声明依赖: %s", ", ".join(report.missing))
            ast_count = len(report.ast_third_party)
            st.processed(ast_count)
            st.set_detail(f"AST {ast_count} 个第三方")
    return report


def _download_dependencies(ctx: BuildContext, site_packages: Path, report: DependencyReport) -> bool:
    """下载并解压第三方依赖 wheel 到 site-packages，返回是否补充了 tkinter.

    补充内置库 tkinter（embed python 缺失，AST 检测到使用时从 python-build-standalone 提取）。
    下载用包名优先 declared（PyPI 包名权威），declared 为空时回退 ast_third_party。
    """
    target = ctx.cfg.target
    # 补充内置库：embed python 缺失 tkinter（纯 Python 包 + _tkinter.pyd + Tcl/Tk 脚本），
    # 若 AST 检测到 tkinter 使用则从 python-build-standalone Windows 构建提取并补充到 runtime。
    # Linux standalone 已含全部 stdlib，无需补充。
    has_tkinter = False
    TkinterBundler_dispatch = _S("TkinterBundler", _DefaultTkinterBundler)
    if TkinterBundler_dispatch.is_needed(report.ast_stdlib, target):  # type: ignore[attr-defined]
        builtin_cache = cache_root()
        with ctx.tracker.stage("补充内置库") as st:
            TkinterBundler_dispatch.ensure(ctx.runtime_dir, ctx.info.py_version, builtin_cache, stage=st)  # type: ignore[attr-defined]
            has_tkinter = True
            st.set_detail("tkinter")

    # 下载用包名：优先 declared（pyproject.toml 声明的 PyPI 包名，权威），
    # declared 为空时回退到 ast_third_party（AST 扫描的导入名，best effort）。
    # 原因：导入名 ≠ PyPI 包名时（如 orderedset → ordered-set），用导入名 pip download 会失败。
    # declared 非空时以声明为准，未声明的依赖通过 report.missing 日志提示用户补充。
    packages_to_download: tuple[str, ...] = report.declared if report.declared else report.ast_third_party

    if packages_to_download:
        if _site_packages_has_deps(site_packages, packages_to_download):
            with ctx.tracker.stage("下载依赖") as st:
                _logger.info("site-packages 已有依赖，跳过下载解压")
                st.skip(len(packages_to_download))
                st.set_detail("已存在跳过")
        else:
            wheel_cache = fspack_wheel_cache_dir()
            download_wheels_dispatch = _S("download_wheels", _default_download_wheels)
            unpack_wheels_dispatch = _S("unpack_wheels", unpack_wheels)
            with ctx.tracker.stage("下载依赖") as st:
                wheels = download_wheels_dispatch(
                    packages_to_download,
                    ctx.info.py_version,
                    ctx.cfg.mirror.pypi_index,
                    wheel_cache,
                    platform_tags=wheel_platform_tags(target),
                    stage=st,
                    extra_index_urls=ctx.info.extra_index_urls,
                    find_links=ctx.info.find_links,
                )
            with ctx.tracker.stage("解压 wheel(精简)") as st:
                unpack_wheels_dispatch(
                    wheels,
                    site_packages,
                    report.ast_submodules,
                    ctx.opts.keep_modules,
                    slim_rules=ctx.info.slim_rules,
                    stage=st,
                )
    else:
        _logger.info("无第三方依赖，跳过 wheel 下载")
    return has_tkinter


def _dep_cache_path(dist_dir: Path) -> Path:
    """依赖分析缓存文件路径：``dist/.dep_cache.json``."""
    return dist_dir / ".dep_cache.json"


def _dep_cache_load(dist_dir: Path, fingerprint: str, declared: tuple[str, ...]) -> DependencyReport | None:
    """加载依赖分析缓存，指纹或声明依赖不匹配时返回 ``None``."""
    cache = _dep_cache_path(dist_dir)
    if not cache.is_file():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("fingerprint") != fingerprint or tuple(data.get("declared", [])) != declared:
        return None
    r = data["report"]
    return DependencyReport(
        declared=tuple(r["declared"]),
        ast_third_party=tuple(r["ast_third_party"]),
        ast_stdlib=tuple(r["ast_stdlib"]),
        ast_local=tuple(r["ast_local"]),
        ast_submodules={k: frozenset(v) for k, v in r["ast_submodules"].items()},
    )


def _dep_cache_save(dist_dir: Path, fingerprint: str, report: DependencyReport) -> None:
    """保存依赖分析缓存."""
    cache = _dep_cache_path(dist_dir)
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": fingerprint,
        "declared": list(report.declared),
        "report": {
            "declared": list(report.declared),
            "ast_third_party": list(report.ast_third_party),
            "ast_stdlib": list(report.ast_stdlib),
            "ast_local": list(report.ast_local),
            "ast_submodules": {k: sorted(v) for k, v in report.ast_submodules.items()},
        },
    }
    atomic_write_text(cache, json.dumps(payload, ensure_ascii=False))


def _site_packages_has_deps(site_packages: Path, packages: Sequence[str]) -> bool:
    """检查 site-packages 是否已安装全部声明依赖.

    逐个检查 ``packages`` 中的包是否有对应的 ``*.dist-info`` 目录。
    仅当全部声明依赖均已安装时返回 True，可跳过下载+解压阶段
    （需 ``fspack c`` 清理后才会重新解压）。

    不能仅检查 ``any(*.dist-info)``：python-build-standalone 预装 pip
    （含 ``pip-*.dist-info``），embed python 也会预装 pip，导致无用户依赖时
    误判为已安装。必须按声明的包名逐一匹配。
    """
    if not site_packages.is_dir():
        return False
    # 收集 site-packages 中所有已安装包的规范化名（PEP 503）
    installed: set[str] = set()
    for d in site_packages.glob("*.dist-info"):
        if not d.is_dir():
            continue
        # dist-info 目录名格式: <name>-<version>.dist-info
        stem = d.name[: -len(".dist-info")]
        # 从右侧分离 version（最后一个 - 之后的部分）
        parts = stem.rsplit("-", 1)
        pkg_name = parts[0] if len(parts) == 2 else stem
        installed.add(_normalize_pkg_name(pkg_name))

    return all(_normalize_pkg_name(_strip_version_specifier(pkg)) in installed for pkg in packages)


def _strip_version_specifier(pkg: str) -> str:
    """从依赖字符串中剥离版本 specifier，返回纯包名.

    ``pygame>=2.5.0`` → ``pygame``；``requests`` → ``requests``。
    """
    return re.split(r"[<>=!~;\[]", pkg, maxsplit=1)[0].strip()


def unpack_wheels(  # noqa: PLR0913
    wheels: Sequence[Path],
    site_packages_dir: Path,
    submodule_usage: dict[str, frozenset[str]] | None = None,
    keep_modules: set[str] | None = None,
    *,
    slim_rules: SlimRules = DEFAULT_SLIM_RULES,
    stage: StageRecorder | None = None,
) -> int:
    """将给定 wheel 列表解包到 site-packages 目录，返回解包数量.

    当提供 ``submodule_usage`` 时按子模块分析选择性解压（精简打包），
    否则全量解压。``slim_rules`` 透传给 ``slim_unpack``，作为用户自定义
    glob 规则覆盖 spec 自动分类。
    """
    from fspack.slim import slim_unpack

    return slim_unpack(
        wheels,
        site_packages_dir,
        submodule_usage,
        keep_modules,
        slim_rules=slim_rules,
        stage=stage,
    )
