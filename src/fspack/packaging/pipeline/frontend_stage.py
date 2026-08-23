"""前端构建阶段：``fsp b`` 自动识别 web 结构并构建前端产物.

背景：模板运行时（如 webview_app ``server.py``）在产物缺失时惰性执行
``npm install && npm run build``——开发机 ``fsp r`` 首跑可用，但 ``fsp b``
若不先构建前端，打出的应用会在终端用户机器上尝试安装前端依赖（无 node
环境，必然失败）。本阶段在复制源码前识别前端项目并就地构建，保证
``web-static-dirs`` 配置的前端产物随包分发。

识别规则（两条路径，命中任一）：

1. **显式配置**：``[tool.fspack] web-static-dirs`` 各目录自下而上找最近的
   ``package.json``（如 ``frontend/deploy`` → ``frontend``）。配置项应指向
   构建产物目录；若误指含源码的前端根目录，目录恒非空等效于跳过（安全退化）。
2. **结构自动识别**（未配置项目，如 webview_app）：剪枝遍历项目目录
   （深度 ≤ 4，排除 node_modules/dist/.venv 等）找含 ``build`` 脚本的
   ``package.json``，产物目录按约定取 ``deploy/`` 或 ``dist/``。

跳过条件：无 ``build`` 脚本（如纯手写 html 的最小模板）、或产物目录已非空
（增量语义，重建请先删除产物目录）。执行顺序：pnpm → yarn → npm
（:func:`shutil.which` 解析，Windows 自动命中 ``.cmd``）；``node_modules``
缺失时先 install 再 build。失败抛 :class:`fspack.exceptions.FspackError`。

**公开导出**（由 ``pipeline.stages`` re-export）：
- :func:`_detect_frontends`：识别前端项目（纯检测，无副作用）
- :func:`_build_frontend`：构建（产物就绪则跳过），返回阶段 detail 文案
- :func:`_frontend_prune_map`：前端项目集转 copy_source 裁剪映射
- :class:`FrontendProject`：单个前端项目（根目录 + 产物目录集合）
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any

from fspack._util.jsoncache import load_json_dict
from fspack.exceptions import FspackError

__all__ = [
    "FrontendProject",
    "_build_frontend",
    "_detect_frontends",
    "_frontend_prune_map",
]

_logger = logging.getLogger(__name__)

# 单条前端命令超时（秒）：pnpm install 冷缓存下载依赖 + vite/vue-tsc 构建
# 在慢网络/慢 CI 可达数分钟；600s 与 Nuitka 编译超时一致，超时终止进程树
_FE_CMD_TIMEOUT_SEC = 600.0

# drain 线程 join 超时（秒）：进程树被杀后管道写端未全部关闭时不无限等待
_FE_DRAIN_JOIN_TIMEOUT = 5.0

# 输出累积上限（4MB）：pnpm/vite 单命令输出远小于此，超限停止累积（继续透传终端）
_FE_ACCUM_LIMIT = 4 * 1024 * 1024

# 包管理器探测顺序：pnpm（模板推荐，workspace 配置就绪）→ yarn → npm
_PM_CANDIDATES: tuple[str, ...] = ("pnpm", "yarn", "npm")

# 结构扫描剪枝目录名：依赖缓存/构建产物/虚拟环境/工具缓存等不进入
# （node_modules 内大量 package.json，必须剪枝避免误识别与遍历开销）
_EXCLUDED_SCAN_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        "site-packages",
        ".tox",
        "htmlcov",
        "release",
        ".uv-cache",
        ".ruff_cache",
        ".pyrefly_cache",
        ".mypy_cache",
        ".pytest_cache",
        ".idea",
        ".vscode",
    }
)

# 结构扫描最大深度：覆盖 frontend/、src/<pkg>/frontend/ 等常见布局，
# 避免深目录（如前端框架内部结构）拖慢扫描
_MAX_SCAN_DEPTH = 4


@dataclass(frozen=True)
class FrontendProject:
    """单个待构建前端项目：根目录（含 package.json）与产物目录集合.

    :param root: 前端项目根目录（含 ``package.json``，构建的工作目录）
    :param output_dirs: 构建产物目录集合（任一非空即视为产物就绪）；
        显式配置来源为 ``web-static-dirs`` 目录，自动识别来源按约定取
        ``deploy/`` 与 ``dist/``
    """

    root: Path
    output_dirs: tuple[Path, ...]


def _load_package_json(path: Path) -> dict[str, Any] | None:
    """读取 ``package.json`` 为 dict，不存在或损坏返回 None（不抛）."""
    return load_json_dict(path, delete_on_corrupt=False, logger=_logger)


def _has_build_script(pkg: dict[str, Any]) -> bool:
    """package.json 是否含可执行的 ``scripts.build`` 定义."""
    scripts = pkg.get("scripts")
    return isinstance(scripts, dict) and isinstance(scripts.get("build"), str)


def _find_configured_frontends(project_dir: Path, web_static_dirs: tuple[str, ...]) -> list[FrontendProject]:
    """按 ``web-static-dirs`` 配置定位前端项目：各自下而上找最近 package.json.

    产物目录缺失（尚未构建）不影响向上查找——路径运算不需要目录存在。
    """
    root = project_dir.resolve()
    result: list[FrontendProject] = []
    for rel in web_static_dirs:
        static_dir = (project_dir / rel).resolve()
        cur = static_dir
        # cur 位于项目目录内（含项目根）才继续；配置越界时循环自然终止
        while cur == root or root in cur.parents:
            pkg_path = cur / "package.json"
            if pkg_path.is_file():
                pkg = _load_package_json(pkg_path)
                if pkg is not None and _has_build_script(pkg):
                    result.append(FrontendProject(cur, (static_dir,)))
                break
            if cur == root:
                break
            cur = cur.parent
    return result


def _find_auto_frontends(project_dir: Path) -> list[FrontendProject]:
    """结构扫描定位前端项目：剪枝遍历找含 build 脚本的 package.json."""
    roots: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_SCAN_DIRS]
        rel = Path(dirpath).relative_to(project_dir)
        if len(rel.parts) >= _MAX_SCAN_DEPTH:
            dirnames[:] = []  # 超深不再下钻
        if "package.json" not in filenames:
            continue
        cur = Path(dirpath)
        pkg = _load_package_json(cur / "package.json")
        if pkg is not None and _has_build_script(pkg):
            roots.append(cur)
    return [FrontendProject(r, (r / "deploy", r / "dist")) for r in roots]


def _detect_frontends(project_dir: Path, web_static_dirs: tuple[str, ...]) -> list[FrontendProject]:
    """识别项目内的前端项目：显式配置优先，结构扫描补充（按根目录去重）."""
    configured = _find_configured_frontends(project_dir, web_static_dirs)
    seen = {fp.root for fp in configured}
    auto = [fp for fp in _find_auto_frontends(project_dir) if fp.root not in seen]
    return [*configured, *auto]


def _is_wsl_windows_mount(exe: str) -> bool:
    """可执行文件是否位于 WSL 的 Windows 盘符挂载路径（``/mnt/<drive>/...``）.

    WSL 默认把 Windows PATH 追加进 Linux PATH，``shutil.which("pnpm")`` 会命中
    ``/mnt/c/.../nodejs/pnpm``（Windows 发行版的 sh 包装脚本）。该脚本 ``exec node``
    依赖 Linux PATH 中的 node——而 Windows node 不在 Linux PATH（``node.exe`` 非
    ``node``），执行必然失败（``exec: node: not found``）。且 Windows 进程的 cwd
    无法落在 Linux 文件系统（/tmp 等），即使经 interop 跑起来也写不进产物目录，
    必须跳过，让后续候选（或明确的「未找到包管理器」报错）接管。
    """
    # 必须用 PurePosixPath：宿主机为 Windows 时 Path 会把根解析成 "\\"，导致挂载检测恒为 False
    parts = PurePosixPath(exe).parts
    return len(parts) >= 3 and parts[0] == "/" and parts[1] == "mnt" and len(parts[2]) == 1 and parts[2].isalpha()


def _resolve_pm() -> tuple[str, str] | None:
    """按 pnpm → yarn → npm 顺序解析包管理器，返回 ``(名称, 可执行文件路径)``.

    :func:`shutil.which` 在 Windows 上按 PATHEXT 命中 ``pnpm.cmd`` 等，
    无需手动拼接后缀。Linux/WSL 下跳过 Windows 盘符挂载路径（见
    :func:`_is_wsl_windows_mount`），未找到时返回 ``None``。
    """
    for name in _PM_CANDIDATES:
        exe = shutil.which(name)
        if exe and not _is_wsl_windows_mount(exe):
            return name, exe
    return None


def _run_cmd(exe: str, args: Sequence[str], cwd: Path) -> None:
    """在前端目录执行包管理器命令，流式透传输出，非零退出/超时抛 FspackError.

    用 ``Popen`` + 守护线程实时透传 stdout/stderr 到终端：vite/vue-tsc 构建
    可达数分钟，静默捕获输出会被误认为卡死（与 Nuitka ``_stream_compile``
    同模式）。输出累积供失败诊断（上限 :data:`_FE_ACCUM_LIMIT`）。

    超时防护：``_FE_CMD_TIMEOUT_SEC``（600s）后终止整棵进程树——Windows 上
    ``pnpm.CMD`` 经 cmd 包装派生 node/vite 孙进程，仅 kill 直接子进程时
    孙进程持有管道写端，drain 线程等不到 EOF 会永久阻塞，须 ``taskkill /T``
    递归终止。

    :param exe: 包管理器可执行文件路径（:func:`_resolve_pm` 的结果）
    :param args: 命令参数（如 ``("install",)``/``("run", "build")``）
    :param cwd: 工作目录（前端项目根）
    :raises FspackError: 命令启动失败、退出码非零、或超时被终止
    """
    cmd = [exe, *args]
    _logger.info("执行: %s（目录 %s）", " ".join(cmd), cwd)
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise FspackError(f"执行 {exe} 失败: {e}") from e

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def _drain(stream: IO[bytes] | None, chunks: list[bytes], out: Any) -> None:
        """消费管道防写端阻塞：os.read 字节块实时写终端并累积（超限停积）."""
        assert stream is not None
        fd = stream.fileno()
        total = 0
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:  # pragma: no cover - fd 被关闭的竞态防御
                break
            if not chunk:
                break
            if total < _FE_ACCUM_LIMIT:
                chunks.append(chunk)
                total += len(chunk)
            out.buffer.write(chunk)
            out.buffer.flush()

    t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks, sys.stdout), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks, sys.stderr), daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        returncode = proc.wait(timeout=_FE_CMD_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        timed_out = True
        _logger.warning("前端命令超时（%ds），终止进程树: %s", int(_FE_CMD_TIMEOUT_SEC), " ".join(cmd[:3]))
        if sys.platform == "win32":
            # Windows 无进程组：taskkill /T 递归终止 pnpm.CMD → node → vite 全链
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            # POSIX：Popen 无独立进程组，仅杀直接子进程；孙进程随管道关闭兜底退出
            proc.kill()
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill 后残留极罕见
            _logger.warning("超时 kill 后子进程 %d 5s 内未退出，放弃等待", proc.pid)
            returncode = -1
    finally:
        t_out.join(timeout=_FE_DRAIN_JOIN_TIMEOUT)
        t_err.join(timeout=_FE_DRAIN_JOIN_TIMEOUT)

    if timed_out or returncode != 0:
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        tail = (stderr or stdout or "").strip()[-800:]
        kind = "前端命令超时" if timed_out else "前端命令失败"
        raise FspackError(f"{kind}（{' '.join(args)}，目录 {cwd}）:\n{tail}")


def _output_ready(fp: FrontendProject) -> bool:
    """前端产物是否就绪：任一产物目录存在且非空."""
    return any(d.is_dir() and next(d.iterdir(), None) is not None for d in fp.output_dirs)


def _build_frontend(frontends: list[FrontendProject]) -> str:
    """构建前端项目集合：产物就绪跳过，缺失则 install（如需）+ build.

    :param frontends: :func:`_detect_frontends` 的识别结果
    :return: 阶段 detail 文案（构建了哪些、跳过了哪些）
    :raises FspackError: 无包管理器且产物缺失、命令失败、或构建后产物仍为空
    """
    todo: list[FrontendProject] = []
    skipped: list[str] = []
    for fp in frontends:
        if _output_ready(fp):
            skipped.append(fp.root.name)
        else:
            todo.append(fp)

    if not todo:
        return f"产物已存在，跳过: {'、'.join(skipped)}"

    pm = _resolve_pm()
    if pm is None:
        raise FspackError("前端产物缺失且未找到 pnpm/yarn/npm，请安装 Node.js 后重试或手动构建前端")
    pm_name, exe = pm

    built: list[str] = []
    for fp in todo:
        _logger.info("构建前端: %s", fp.root)
        if not (fp.root / "node_modules").is_dir():
            _run_cmd(exe, ["install"], fp.root)
        _run_cmd(exe, ["run", "build"], fp.root)
        if not _output_ready(fp):
            raise FspackError(
                f"前端构建完成但产物目录仍为空: {fp.root}"
                "（检查构建输出目录是否为 deploy/dist，或在 [tool.fspack] web-static-dirs 显式配置）"
            )
        built.append(fp.root.name)

    detail = f"{pm_name} 构建: {'、'.join(built)}"
    if skipped:
        detail += f"；产物已存在跳过: {'、'.join(skipped)}"
    return detail


def _frontend_prune_map(frontends: Sequence[FrontendProject]) -> dict[Path, tuple[Path, ...]]:
    """前端项目集转裁剪映射：前端根目录 → 产物目录元组.

    供 :func:`fspack.packaging.sync.copy_source` 的 ``frontend_prune`` 参数
    使用——dist 内前端根目录下只保留产物路径（``deploy/``/``dist/``），
    源码（``src/``/``public/``/``package.json``/构建配置等）不进入发布产物。
    """
    return {fp.root: fp.output_dirs for fp in frontends}
