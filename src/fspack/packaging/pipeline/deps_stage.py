"""依赖阶段：AST 依赖分析 + wheel 下载解压 + 依赖指纹缓存.

职责单一化：仅处理依赖相关的阶段，不涉及源码编译、loader 生成等其他流程。

- :func:`_analyze_dependencies`：AST 分析（指纹缓存命中则跳过）
- :func:`_download_dependencies`：tkinter 补充 + wheel 下载 + 暂存目录精简解压
- :func:`_dep_cache_path` / :func:`_dep_cache_load` / :func:`_dep_cache_save`：
  分析结果缓存（JSON 文件），重复构建加速 ~478ms
- :func:`_dependency_build_key` / :func:`_installed_fingerprint` /
  :func:`_save_installed_dependencies` / :func:`_site_packages_has_deps`：
  依赖复用判定——构建身份 + 产物指纹双重校验
- :func:`unpack_wheels`：调用 slim_unpack（顶层包装便于测试 patch）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fspack.config import (
    DEFAULT_SLIM_RULES,
    DependencyReport,
    SlimRules,
    cache_root,
)
from fspack.fsutil import atomic_write_text
from fspack.packaging.builtin import TkinterBundler as _DefaultTkinterBundler
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
    "_dependency_build_key",
    "_download_dependencies",
    "_installed_fingerprint",
    "_save_installed_dependencies",
    "_site_packages_has_deps",
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
        from fspack.analyzer.fingerprint import clear_fingerprint_cache
        from fspack.config import expand_extras

        # 每次构建入口失效构建级指纹缓存（cached_source_fingerprint）：
        # 上一轮构建的 pyc 剥离等步骤修改过 dist/src 树，防止跨构建读到脏指纹
        clear_fingerprint_cache()

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

    复用判定：:func:`_site_packages_has_deps` 同时校验构建身份键与 site-packages
    产物指纹，两者都与上次成功构建一致才跳过下载解压。

    重解包走暂存目录：先解压到 ``dist/.fspack-deps-*/site-packages``，成功后整体
    ``replace`` 替换正式目录。两个目的——

    - 依赖变少或 ``slim_rules``/子模块收紧时，旧文件不会残留（原地解压只会新增
      与覆盖，从不删除）
    - 解压或替换失败时旧 site-packages 原样回滚，不会留下半成品

    tkinter 装于 runtime 目录而非 site-packages，故整体替换不影响它。
    替换前删除 ``.pyc_stamp``：dist-info 目录名未变时 pyc 键不变，但它记录的
    字节码可能属于已被替换的旧依赖版本。
    """
    target = ctx.cfg.target
    # 补充内置库：embed python 缺失 tkinter（纯 Python 包 + _tkinter.pyd + Tcl/Tk 脚本），
    # 若 AST 检测到 tkinter 使用则从 python-build-standalone Windows 构建提取并补充到 runtime。
    # Linux standalone 已含全部 stdlib，无需补充。
    # tkinter 使用检测须跨平台：Linux/macOS 的 trim 阶段（_slim_runtime）依赖
    # has_tkinter 保留 Tcl/Tk 动态库（libtcl9.0.so/libtk9.0.so，_tkinter 模块
    # 运行时 dlopen 依赖），仅按 Windows 判定会让 Linux tkinter 项目误删库而崩溃。
    has_tkinter = "tkinter" in report.ast_stdlib
    TkinterBundler_dispatch = _S("TkinterBundler", _DefaultTkinterBundler)
    if TkinterBundler_dispatch.is_needed(report.ast_stdlib, target):  # type: ignore[attr-defined]
        builtin_cache = cache_root()
        with ctx.tracker.stage("补充内置库") as st:
            TkinterBundler_dispatch.ensure(ctx.runtime_dir, ctx.info.py_version, builtin_cache, stage=st)  # type: ignore[attr-defined]
            st.set_detail("tkinter")

    # 下载用包名：优先 declared（pyproject.toml 声明的 PyPI 包名，权威），
    # declared 为空时回退到 ast_third_party（AST 扫描的导入名，best effort）。
    # 原因：导入名 ≠ PyPI 包名时（如 orderedset → ordered-set），用导入名 pip download 会失败。
    # declared 非空时以声明为准，未声明的依赖通过 report.missing 日志提示用户补充。
    packages_to_download: tuple[str, ...] = report.declared if report.declared else report.ast_third_party

    build_key = _dependency_build_key(ctx, report)
    if _site_packages_has_deps(site_packages, packages_to_download, build_key=build_key):
        with ctx.tracker.stage("下载依赖") as st:
            st.skip(len(packages_to_download))
            st.set_detail("已存在跳过（构建参数与产物校验通过）")
        return has_tkinter

    wheels: list[Path] = []
    if packages_to_download:
        download_wheels_dispatch = _S("download_wheels", _default_download_wheels)
        with ctx.tracker.stage("下载依赖") as st:
            wheels = download_wheels_dispatch(
                packages_to_download,
                ctx.info.py_version,
                ctx.cfg.mirror.pypi_index,
                fspack_wheel_cache_dir(),
                platform_tags=wheel_platform_tags(target),
                stage=st,
                extra_index_urls=ctx.info.extra_index_urls,
                find_links=ctx.info.find_links,
                require_hashes=ctx.opts.require_hashes,
            )
    else:
        _logger.info("无第三方依赖，跳过 wheel 下载")

    # 暂存目录与旧目录备份都在同一个临时目录内：整体替换成功后临时目录自动清理，
    # 失败时备份仍在（回滚用），且两种情况都不会在 dist 留下中间目录。
    with tempfile.TemporaryDirectory(prefix=".fspack-deps-", dir=site_packages.parent) as work:
        staged = Path(work) / "site-packages"
        staged.mkdir()
        if packages_to_download:
            with ctx.tracker.stage("解压 wheel(精简)") as st:
                _S("unpack_wheels", unpack_wheels)(
                    wheels,
                    staged,
                    report.ast_submodules,
                    ctx.opts.keep_modules,
                    slim_rules=ctx.info.slim_rules,
                    stage=st,
                )
        # dist-info 名称相同时 pyc 键不会变化，替换依赖前必须使旧编译 stamp 失效。
        pyc_stamp = ctx.cfg.dist_dir / ".pyc_stamp"
        if pyc_stamp.is_file():
            pyc_stamp.unlink()
        previous = Path(work) / "previous"
        if site_packages.exists():
            site_packages.replace(previous)
        try:
            staged.replace(site_packages)
        except OSError:
            # 回滚：恢复旧 site-packages。回滚本身失败时保留备份目录并告警
            # （临时目录退出会带走备份，故先移出 dist），仍抛原始异常。
            try:
                previous.replace(site_packages)
            except OSError as rollback_error:  # pragma: no cover - 双重故障极罕见
                _logger.error("依赖回滚失败，旧 site-packages 保留在 %s: %s", previous, rollback_error)
            raise
    return has_tkinter


def _dep_cache_path(dist_dir: Path) -> Path:
    """依赖分析缓存文件路径：``dist/.dep_cache.json``."""
    return dist_dir / ".dep_cache.json"


def _dep_cache_load(dist_dir: Path, fingerprint: str, declared: tuple[str, ...]) -> DependencyReport | None:
    """加载依赖分析缓存，指纹或声明依赖不匹配时返回 ``None``.

    缓存 JSON 结构损坏（字段缺失/类型异常，如旧版本缓存或外部改写）时
    返回 ``None`` 回退重新分析，不抛异常阻断构建。
    """
    cache = _dep_cache_path(dist_dir)
    if not cache.is_file():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if data["fingerprint"] != fingerprint or tuple(data["declared"]) != declared:
            return None
        r = data["report"]
        return DependencyReport(
            declared=tuple(r["declared"]),
            ast_third_party=tuple(r["ast_third_party"]),
            ast_stdlib=tuple(r["ast_stdlib"]),
            ast_local=tuple(r["ast_local"]),
            ast_submodules={k: frozenset(v) for k, v in r["ast_submodules"].items()},
            # 旧缓存无 ast_errors 字段时回填空 tuple（兼容）
            ast_errors=tuple(r.get("ast_errors", ())),
        )
    except (KeyError, TypeError, AttributeError):
        # 结构损坏（字段缺失/类型不符）：视为缓存未命中，回退重新分析
        return None


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
            # AST 解析失败的诊断信息一并持久化，缓存命中时不丢失
            "ast_errors": list(report.ast_errors),
        },
    }
    atomic_write_text(cache, json.dumps(payload, ensure_ascii=False))


def _dependency_build_key(ctx: BuildContext, report: DependencyReport) -> str:
    """依赖复用身份键：影响 site-packages 最终内容的全部构建参数.

    覆盖下载包列表（含版本 specifier）、目标 Python/平台、主镜像与私有源、
    子模块使用与 keep_modules、slim include/exclude 规则，以及会改写依赖文件的
    下游转换开关（pyc 编译/剥离、no_site、Nuitka、二进制依赖分析、Win7 补丁、
    哈希校验）。任一项变化即视为身份不同，必须重新下载解压。

    ``analyze_deps`` 会按源码 import 图删除无引用二进制，故仅在该开关开启时把
    源码指纹纳入键（代价是一次目录遍历）。
    """
    from fspack import __version__
    from fspack.analyzer import source_fingerprint

    opts = ctx.opts
    payload = {
        "schema": 1,
        "fspack": __version__,
        "packages": sorted(report.declared or report.ast_third_party),
        "python": ctx.info.py_version,
        "platform": ctx.cfg.target.value,
        "index": ctx.cfg.mirror.pypi_index,
        "extra_indexes": ctx.info.extra_index_urls,
        "find_links": ctx.info.find_links,
        "submodules": {pkg: sorted(subs) for pkg, subs in report.ast_submodules.items()},
        "keep_modules": sorted(opts.keep_modules or ()),
        "include": ctx.info.slim_rules.include,
        "exclude": ctx.info.slim_rules.exclude,
        "transforms": [
            opts.no_pyc,
            opts.pyc_strip,
            opts.pyc_optimize,
            opts.no_site,
            opts.nuitka,
            opts.nuitka_packages,
            opts.compiler,
            opts.analyze_deps,
            opts.no_win7_scan,
            opts.no_win7_dll,
            opts.require_hashes,
        ],
        "source": source_fingerprint(ctx.cfg.project_dir, ctx.info.data_dirs) if opts.analyze_deps else "",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _installed_fingerprint(site_packages: Path) -> str | None:
    """site-packages 内容指纹（相对路径 + size + mtime_ns），含子目录.

    用 ``os.scandir`` 复用目录枚举缓存的 stat，避免逐文件独立系统调用。
    含符号链接或枚举遇 ``OSError``（权限/并发删除）时返回 ``None``——
    调用方据此判定"无法验证"并重新解包，绝不在不确定时复用。
    """
    if not site_packages.is_dir():
        return None
    digest = hashlib.sha256()
    pending = [site_packages]
    try:
        while pending:
            with os.scandir(pending.pop()) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name)
            for entry in ordered:
                if entry.is_symlink():
                    return None
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    rel = Path(entry.path).relative_to(site_packages).as_posix()
                    digest.update(json.dumps([rel, stat.st_size, stat.st_mtime_ns]).encode("utf-8"))
    except OSError:
        return None
    return digest.hexdigest()


def _save_installed_dependencies(ctx: BuildContext, site_packages: Path, report: DependencyReport) -> None:
    """在构建末尾记录 site-packages 的最终状态，供下次构建验证复用.

    pyc 剥离、Nuitka 替换、``no_site`` 删除 ``.pth``、二进制依赖分析与 Win7 补丁
    都会合法改写/删除 wheel 解出的文件，因此必须在这些步骤完成之后采集指纹——
    记录的是"上次成功构建真正留下的产物"，而非解压瞬间的原始内容。

    指纹为 ``None``（符号链接等）时照常写入：下次校验必然不匹配，退回重新解包。
    """
    cache = _dep_cache_path(ctx.cfg.dist_dir)
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        payload["installed"] = {
            "key": _dependency_build_key(ctx, report),
            "packages": sorted(report.declared or report.ast_third_party),
            "fingerprint": _installed_fingerprint(site_packages),
        }
        atomic_write_text(cache, json.dumps(payload, ensure_ascii=False))
    except (OSError, ValueError):
        _logger.warning("无法保存依赖产物状态，下次构建将重新解包")


def _site_packages_has_deps(
    site_packages: Path,
    packages: Sequence[str],
    *,
    build_key: str = "",
) -> bool:
    """site-packages 可否直接复用：构建身份与产物指纹同时匹配.

    历史实现只按 ``*.dist-info`` 目录名判断"依赖已安装"，两处假阳性：版本
    specifier 变化（``numpy>=1.0`` → ``numpy>=2.0``）与产物被下游步骤/人工删除
    均检测不到，会静默产出旧依赖。现改为双重校验——

    1. ``build_key``（:func:`_dependency_build_key`）与上次成功构建一致，
       且记录的包名列表与本次完全相同；
    2. site-packages 实际内容指纹与记录的指纹逐字节一致。

    任一条件不满足、缺少 ``build_key``、上次构建留有 ``.build_failed`` 标记或
    缓存结构异常，一律返回 ``False`` 走重新下载解压。
    """
    if not build_key or (site_packages.parent / ".build_failed").exists():
        return False
    try:
        payload = json.loads(_dep_cache_path(site_packages.parent).read_text(encoding="utf-8"))
        state = payload["installed"]
        if state["key"] != build_key or state["packages"] != sorted(packages):
            return False
        fingerprint = _installed_fingerprint(site_packages)
        return fingerprint is not None and state["fingerprint"] == fingerprint
    except (OSError, ValueError, KeyError, TypeError):
        return False


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
