"""编译与产物构建阶段：用户源码编译 + entry loader 生成 + 二进制依赖分析 + 图标解析.

- :func:`_compile_user_sources`：Nuitka 编译（可选）+ 字节码预编译 + pyc_strip 源码剥离
- :func:`_build_entry_loaders` / :func:`_build_one_loader` / :func:`_loader_exe_path`：
  多入口 C loader 并行编译（ThreadPoolExecutor，上限 _MAX_LOADER_WORKERS）
- :func:`_analyze_binary_dependencies`：PE/ELF/Mach-O 依赖图 BFS 剥离无引用二进制
- :func:`_resolve_project_icon`：4 层优先级（CLI > 配置 > favicon > 默认）icon 解析
"""

from __future__ import annotations

import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor as _DefaultThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from fspack.config import AppType, EntryPoint, nuitka_cache_dir
from fspack.packaging.entry import EntryWrapper
from fspack.packaging.icon import ensure_ico, find_favicon
from fspack.packaging.loader import (
    LoaderVersionInfo,
    generate_loader_source,
)
from fspack.packaging.loader import (
    compile_loader as _default_compile_loader,
)
from fspack.packaging.pyc import _precompile_pyc
from fspack.platform import Platform
from fspack.platform import detect_platform as _default_detect_platform

from .context import _DEFAULT_ICON, _MAX_LOADER_WORKERS, BuildContext

if TYPE_CHECKING:
    from fspack.progress import StageRecorder

__all__ = [
    "_analyze_binary_dependencies",
    "_build_entry_loaders",
    "_compile_user_sources",
    "_resolve_project_icon",
]

_logger = logging.getLogger(__name__)

# 延迟持有 stages 模块引用，避免顶层循环 import
_stages_mod_holder: list[Any] = [None]


def _S(fn_name: str, fallback_fn: Callable[..., Any]) -> Callable[..., Any]:
    """运行时从 :mod:`stages` 模块动态取 ``fn_name``，fallback 到默认实现.

    兼容测试 patch ``fspack.packaging.pipeline.stages.compile_loader``。
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


def _compile_user_sources(ctx: BuildContext, src_dst: Path) -> None:
    """编译用户源码：Nuitka 编译（可选）+ 字节码预编译.

    Nuitka 编译模式：用 runtime python -c "sys.path.insert(0, <nuitka_cache>); ..." 调用
    nuitka --module 将 dist/src 下用户源码编译为 .pyd。
    用户源码以 .pyd 形式本机执行，速度提升 30-50%（参考 RimSort Nuitka 打包方案）。
    仅编译用户源码（src/），第三方依赖（site-packages/）保持 wheel 解压 + .pyc。
    交叉构建跳过（Nuitka 无法生成目标平台 .pyd）。
    win7 重编译版 runtime 跳过（``runtime_dir/.win7_runtime`` 标记存在，py>=3.12
    Windows 默认启用）：官方工具链编译的 .pyd 与重编译版 python3XX.dll ABI 不兼容
    （加载即访问违例），编译必败回退 .pyc，见 :func:`is_win7_runtime`。
    nuitka 装到本地缓存 ~/.fspack/cache/nuitka/<py_version>/，不污染 dist/runtime；
    编译时用 -c 注入 sys.path 绕过 _pth 对 PYTHONPATH 的限制。
    stamp 命中跳过整个阶段（含 ensure_env 与 compile_src）。
    入口文件跳过编译与剥离：入口包装器用 ``runpy.run_module``/``run_path`` 调用
    用户代码，需 ``.py`` 存在才能被 ``find_spec`` 定位（``.pyd`` 无字节码无法被
    ``runpy`` 执行，``__pycache__`` 下的 ``.pyc`` 不在 ``FileFinder`` 搜索范围）。

    ``[tool.fspack] data-dirs`` 配置的数据资源目录树（``ctx.info.data_dirs``）
    与 ``web-static-dirs`` 配置的前端构建产物目录（``ctx.info.web_static_dirs``）
    解析为 dist/src 下绝对路径后传给 Nuitka 编译与 ``_precompile_pyc``，
    其下 ``.py`` 既不被 Nuitka 编译也不被剥离：这些目录视为完整资源原样保留
    （如 fspack 的 ``assets/templates/`` 含项目模板源码，逐一 Nuitka 编译既拖慢
    构建也无运行收益，且下游 ``fsp doctor --test`` 复制后需 ``.py`` 存在才能构建；
    前端 ``dist/`` 内含 JS 工具脚本）。
    """
    target = ctx.cfg.target
    detect_platform_dispatch = _S("detect_platform", _default_detect_platform)
    build_host_platform = detect_platform_dispatch()
    # 入口文件相对 src 的 POSIX 路径集合：Nuitka 编译与 pyc_strip 剥离均跳过这些文件
    entry_rels = frozenset(ep.entry_rel(ctx.info.src_dir) for ep in ctx.info.all_entries)
    # data_dirs/web_static_dirs 配置为相对项目目录的 POSIX 路径，解析为 dist/src
    # 下的绝对路径：project_dir/<rel> → dist/src/<rel>（src_dst 即 dist/src，镜像
    # 项目根）。仅解析存在的目录，避免传不存在的路径（无副作用但增加判断开销）。
    # Nuitka 编译收集与 pyc 剥离共用，两类资源目录树同等保护。
    resolved_data_dirs = tuple(src_dst / Path(rel) for rel in ctx.info.data_dirs if (src_dst / Path(rel)).is_dir())
    resolved_web_static_dirs = tuple(
        src_dst / Path(rel) for rel in ctx.info.web_static_dirs if (src_dst / Path(rel)).is_dir()
    )
    if ctx.opts.nuitka and target is build_host_platform:
        with ctx.tracker.stage("Nuitka 编译") as st:
            from fspack.packaging.win7.dll import is_win7_runtime

            if is_win7_runtime(ctx.runtime_dir):
                # win7 重编译版 runtime（adang1345/PythonVista 组件整套替换）与官方
                # embed 构建工具链不同：官方工具链（构建机/standalone python）编译的
                # .pyd 在重编译版 python3XX.dll 进程内加载即访问违例（0xC0000005，
                # Win10/11 同样崩溃，与官方 _ctypes.pyd 混搭崩溃同源，实测 3.13 MSVC
                # 产物在替换后 runtime 100% 复现）。编译是必然失败的无效功（verify
                # 会全部判损坏回退 .pyc），前置跳过并提示用户可选 --no-win7-dll。
                _logger.warning(
                    "Nuitka 编译跳过: runtime 已替换为 win7 重编译版组件，"
                    "官方工具链编译的 .pyd 与其 ABI 不兼容（加载即访问违例），回退到 .pyc 模式；"
                    "如需 Nuitka 本机编译请加 --no-win7-dll（产物将不支持 Win7）"
                )
                st.set_detail("win7 重编译版 runtime，跳过（回退到 .pyc 模式）")
            else:
                from fspack.packaging.nuitka import NuitkaCompiler

                nuitka_cache_root = nuitka_cache_dir()
                NuitkaCompiler.compile_with_stamp(
                    src_dst,
                    ctx.cfg.dist_dir,
                    ctx.runtime_dir,
                    ctx.info.py_version,
                    target,
                    ctx.cfg.mirror,
                    nuitka_cache_root,
                    stage=st,
                    entry_rels=entry_rels,
                    ccache=ctx.opts.ccache,
                    nuitka_packages=ctx.opts.nuitka_packages,
                    data_dirs=(*resolved_data_dirs, *resolved_web_static_dirs),
                )

    # 预编译字节码：用 runtime 自身 python 编译 src + site-packages 为 .pyc，加速首次启动。
    # pyc_strip=True 时额外剥离非 __init__.py 源码（源码保护，保留包标识避免命名空间包问题）。
    # 交叉构建时（构建机平台 ≠ 目标平台）runtime python 无法执行，跳过预编译。
    # Nuitka 模式下 src 已编译为 .pyd，compileall 会跳过（找不到 .py 不生成 .pyc），
    # site-packages 仍按 pyc_optimize 编译，故本步保留不跳过。
    # data_dirs/web_static_dirs 已在上方解析（与 Nuitka 编译共用），其下 .py 不剥离。
    if not ctx.opts.no_pyc and target is build_host_platform:
        with ctx.tracker.stage("预编译字节码") as st:
            _precompile_pyc(
                ctx.cfg.dist_dir,
                ctx.runtime_dir,
                ctx.info.py_version,
                target,
                strip_py=ctx.opts.pyc_strip,
                stage=st,
                optimize=ctx.opts.pyc_optimize,
                entry_rels=entry_rels,
                data_dirs=resolved_data_dirs,
                web_static_dirs=resolved_web_static_dirs,
            )


def _build_entry_loaders(ctx: BuildContext, resolved_icon: Path | None, has_tkinter: bool) -> list[Path]:
    """为每个入口生成 C loader 与入口包装器，返回生成的 exe 路径列表.

    用 ``tempfile.TemporaryDirectory`` 作为 loader 编译工作目录，编译完成后自动清理，
    避免 ``dist/build/`` 残留 ``loader.c``/``icon.rc``/``icon.ico``/``icon.o`` 中间文件
    被打包进发行包。loader 缓存命中路径不创建工作目录，无副作用。

    **并行编译**（iter-133）：多入口场景用 :class:`ThreadPoolExecutor` 并行编译
    每个 entry loader（mingw/gcc/clang 子进程释放 GIL，线程足够并行）。
    ``max_workers = min(cpu_count, :data:`_MAX_LOADER_WORKERS`)`` 平衡并行收益与
    Windows 资源限制。共享 ``TemporaryDirectory``，每个入口分配独立子目录
    （``<tmp>/<entry_name>``）避免 ``loader.c``/``icon.rc``/``icon.o`` 文件冲突。

    **线程安全**：``exes``/``st.processed()`` 仅在主线程（``future.result()`` 迭代）
    聚合，无共享可变状态竞争。``compile_loader`` 内部 ``stage.hit_cache()``/
    ``stage.set_detail()`` 在 worker 线程调用，``StageRecorder._hits += 1`` 在 GIL 下
    最坏丢失一次计数（benign race，不影响正确性）。

    **异常传播**：worker 内 ``compile_loader`` 抛异常（如 ``LoaderError``）时
    ``future.result()`` 重抛，``with ThreadPoolExecutor`` 的 ``__exit__`` 调
    ``shutdown(wait=True)`` 等待在途任务后传播。
    """
    target = ctx.cfg.target
    exes: list[Path] = []
    with ctx.tracker.stage("生成 C loader") as st:
        # splash 启动画面：--splash 构建选项（默认关闭），仅 Windows 生效，
        # 画布标题用应用名（嵌入源码参与 loader 缓存键）
        source = generate_loader_source(
            ctx.info.py_xy,
            target,
            splash=ctx.opts.splash,
            splash_title=ctx.info.name,
        )
        entries = ctx.info.all_entries
        # 单入口无需并行（线程池开销无收益）
        if len(entries) <= 1:
            with tempfile.TemporaryDirectory(prefix="fspack_loader_") as tmp:
                _build_one_loader(ctx, entries[0], source, Path(tmp), resolved_icon, has_tkinter, st)
                exes.append(_loader_exe_path(ctx, entries[0], target))
            st.processed(len(exes))
            return exes

        cpu = os.cpu_count() or 1
        max_workers = min(cpu, _MAX_LOADER_WORKERS)
        _logger.info("并行编译 %d 个 entry loader（max_workers=%d）", len(entries), max_workers)
        ThreadPoolExecutor_dispatch: Any = _S("ThreadPoolExecutor", _DefaultThreadPoolExecutor)
        # 临时工作目录：编译完成（或异常）后自动清理，不污染 dist/
        # 共享 TemporaryDirectory，每入口独立子目录避免 loader.c/icon.rc/icon.o 冲突
        with tempfile.TemporaryDirectory(prefix="fspack_loader_") as tmp:
            build_dir = Path(tmp)

            def _build_one(ep: EntryPoint) -> Path:
                """单入口编译 worker：生成包装器 + .entry + 编译 loader，返回 exe 路径."""
                work_subdir = build_dir / ep.name
                work_subdir.mkdir(parents=True, exist_ok=True)
                _build_one_loader(ctx, ep, source, work_subdir, resolved_icon, has_tkinter, st)
                return _loader_exe_path(ctx, ep, target)

            with ThreadPoolExecutor_dispatch(max_workers=max_workers) as pool:
                futures = [pool.submit(_build_one, ep) for ep in entries]
                # 按 submit 顺序取 result，保持 exes 顺序与 entries 一致。
                # future.result() 重抛 worker 异常（如 LoaderError）：首个异常
                # 不立即抛出，先等待其余 future 完成并逐个记录其异常（多入口
                # 并行编译时常见多个入口同时失败，静默丢弃会丢失诊断信息），
                # 最终重抛首个异常由 with 块 __exit__ 的 shutdown 传播
                first_exc: BaseException | None = None
                for future in futures:
                    try:
                        exes.append(future.result())
                    except Exception as exc:
                        if first_exc is None:
                            first_exc = exc
                        else:
                            _logger.warning("其余 entry loader 编译异常: %s", exc)
                if first_exc is not None:
                    raise first_exc
        st.processed(len(exes))
    return exes


def _build_one_loader(  # noqa: PLR0913
    ctx: BuildContext,
    ep: EntryPoint,
    source: str,
    work_dir: Path,
    resolved_icon: Path | None,
    has_tkinter: bool,
    stage: StageRecorder,
) -> None:
    """为单个入口生成包装器、``.entry`` 文件并编译 loader.

    抽取自 :func:`_build_entry_loaders` 供串行与并行路径复用。``work_dir`` 由调用方
    分配（并行模式下为 ``<tmp>/<entry_name>`` 子目录，避免多入口文件冲突）。
    """
    compile_loader_dispatch = _S("compile_loader", _default_compile_loader)
    entry_rel = ep.entry_rel(ctx.info.src_dir)
    result = EntryWrapper.dotted_module_name(ctx.info.src_dir, ep.file)
    module_dotted = result[0] if result is not None else None
    pkg_root_rel = result[1] if result is not None else "."
    # 生成入口包装器：处理 sys.path、Qt 插件路径与包上下文（相对导入），
    # WEB 类型额外注入静态文件 serve 与自动开浏览器（open_browser 默认启用）。
    # open_browser = opts.open_browser（CLI/配置显式启用）或 WEB 类型自动启用；
    # 非 WEB 类型 opts.open_browser=True 时也启用（如 GUI 内嵌 WebView 场景）。
    wrapper_name = f"_entry_{ep.name}.py"
    wrapper_path = ctx.cfg.dist_dir / wrapper_name
    open_browser = ctx.opts.open_browser or ep.app_type is AppType.WEB
    # web_static_dirs 是相对项目目录的路径（如 "frontend"），copy_source 复制到
    # dist/src/ 下，wrapper 运行时以 _DIST_DIR 为基准解析，需加 "src/" 前缀
    # 使其指向 dist/src/frontend（前端构建产物的实际位置）。
    web_static_dirs = tuple(f"src/{d}" for d in ctx.info.web_static_dirs)
    wrapper_path.write_text(
        EntryWrapper.generate_wrapper_source(
            ep.name,
            module_dotted,
            entry_rel,
            pkg_root_rel,
            has_tkinter=has_tkinter,
            lazy_imports=ctx.opts.lazy_imports,
            web_static_dirs=web_static_dirs,
            open_browser=open_browser,
        ),
        encoding="utf-8",
    )
    # .entry 指向 wrapper（loader 读 .entry 路径运行）
    if ctx.info.entries:
        # 多入口模式：每个入口写 <name>.entry
        (ctx.cfg.dist_dir / f"{ep.name}.entry").write_text(wrapper_name, encoding="utf-8")
    else:
        # 单入口模式：写 .entry（向后兼容）
        (ctx.cfg.dist_dir / ".entry").write_text(wrapper_name, encoding="utf-8")
    exe = _loader_exe_path(ctx, ep, ctx.cfg.target)
    # Windows 目标构造版本信息元数据，嵌入 exe 资源段（VS_VERSIONINFO + manifest），
    # 降低 Defender 等杀软对 mingw 小型 exe 的启发式误报。Linux/macOS 无 PE 资源段，
    # 传 None 保持 loader 缓存按 (source, app_type, platform) 跨项目共享。
    version_info = (
        LoaderVersionInfo(
            name=ctx.info.name,
            version=ctx.info.version,
            description=ctx.info.description,
            author=ctx.info.author,
            exe_filename=f"{ep.name}.exe",
        )
        if ctx.cfg.target is Platform.WINDOWS
        else None
    )
    compile_loader_dispatch(
        source,
        exe,
        ep.app_type,
        work_dir,
        ctx.cfg.target,
        icon=resolved_icon,
        version_info=version_info,
        stage=stage,
    )


def _loader_exe_path(ctx: BuildContext, ep: EntryPoint, target: Platform) -> Path:
    """返回入口对应的 loader exe 路径（Windows 加 ``.exe`` 后缀）."""
    exe_name = f"{ep.name}.exe" if target is Platform.WINDOWS else ep.name
    return ctx.cfg.dist_dir / exe_name


def _analyze_binary_dependencies(ctx: BuildContext) -> int:
    """执行二进制依赖分析，剥离 dist 内无引用的 .dll/.so/.dylib.

    仅当 ``ctx.opts.analyze_deps=True`` 时调用。流程：

    1. :func:`analyze_binary_dependencies` 扫描 dist 下所有二进制，构建依赖图
    2. :func:`find_unused_binaries` 从入口 BFS，返回不可达二进制列表
    3. :func:`strip_unused_binaries` 删除未引用文件，累加节省字节数到 stage

    工具缺失（objdump/otool 未安装）时静默跳过，不阻断构建。
    节省字节数通过 :meth:`StageRecorder.add_saved_bytes` 写入 tracker，
    在 ``BuildTracker.summary()`` 中体现为"依赖分析"阶段"节省"列。
    """
    from fspack.packaging.dep_analyzer import (
        analyze_binary_dependencies,
        find_unused_binaries,
        strip_unused_binaries,
    )

    with ctx.tracker.stage("依赖分析") as st:
        graph = analyze_binary_dependencies(ctx.cfg.dist_dir, ctx.cfg.target, runtime_dir=ctx.runtime_dir)
        if not graph.binaries:
            st.set_detail("无二进制或工具缺失，跳过")
            return 0

        unused = find_unused_binaries(graph)
        if not unused:
            st.set_detail(f"扫描 {len(graph.binaries)} 个二进制，全部可达")
            return 0

        saved = strip_unused_binaries(unused)
        st.add_saved_bytes(saved)
        st.set_detail(f"剥离 {len(unused)} 个无引用二进制")
        _logger.info("依赖分析：剥离 %d 个无引用二进制，节省 %d bytes", len(unused), saved)
        return saved


def _resolve_project_icon(
    cli_icon: Path | None,
    project_icon: Path | None,
    project_dir: Path,
    work_dir: Path,
    target: Platform,
) -> Path | None:
    """按优先级解析最终 icon 路径，非 .ico 格式自动转换。

    优先级：``cli_icon`` > ``project_icon`` > 自动搜索 ``favicon.*`` > 默认 ``app.ico``。

    - Linux 目标：始终返回 ``None``（ELF 无图标资源概念）
    - 非 ``.ico`` 格式（``.png``/``.jpg`` 等）：调用 :func:`ensure_ico` 转换，
      转换失败（如 Pillow 未安装）回退到默认 ``app.ico``
    - 默认 ``app.ico`` 是 fspack 自带资源，必定存在，无需转换

    ``work_dir`` 为图片转换的临时目录（通常是 ``dist/build``）。
    """
    if target is Platform.LINUX:
        return None

    # 选定候选 icon：CLI > 项目配置 > favicon 自动搜索
    candidate = cli_icon
    if candidate is None:
        candidate = project_icon
    if candidate is None:
        candidate = find_favicon(project_dir)
        if candidate is not None:
            _logger.info("使用 favicon 作为 icon: %s", candidate)

    # 无任何候选 → 默认 icon（.ico，无需转换）
    if candidate is None:
        return _DEFAULT_ICON

    # 转换为 .ico（.ico 原样返回，其他格式用 Pillow 转换，失败回退默认）
    resolved = ensure_ico(candidate, work_dir)
    if resolved is not None:
        return resolved
    _logger.warning("icon 转换失败，回退到默认 icon: %s", _DEFAULT_ICON)
    return _DEFAULT_ICON
