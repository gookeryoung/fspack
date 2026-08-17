"""dist 目录诊断与清理辅助：半成品检测、失败标记读写、dist 清理.

从 :mod:`fspack.packaging.pipeline` 顶层拆分出来。全部为纯函数，不依赖阶段
执行上下文，便于 ``fsp c`` 命令与构建入口复用。

**公开导出**（由 ``pipeline.__init__`` re-export）：
- :func:`clean_dist`：``fsp c`` 命令实现，保留诊断文件
- :func:`_handle_dist_incomplete`：构建前半成品检测（auto-clean 或告警）
- :func:`_save_build_failure` / :func:`_load_build_failure` / :func:`_remove_build_failure`：
  ``dist/.build_failed`` JSON 标记读写
- :func:`_clean_dist_dir`：底层清理（auto-clean 时传 ``keep_diagnostics=False``）
- :data:`_KEEP_NSI` / :data:`_BUILD_FAILED` / :data:`_PYC_STAMP` / :data:`_NUITKA_STAMP`：
  常量名
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fspack._util.fsutil import atomic_write_text, rmtree_longpath
from fspack._util.jsoncache import load_json_dict

if TYPE_CHECKING:
    from fspack.progress import BuildTracker

__all__ = [
    "_BUILD_FAILED",
    "_BUILD_OK",
    "_KEEP_NSI",
    "_NUITKA_STAMP",
    "_PYC_STAMP",
    "_clean_dist_dir",
    "_handle_dist_incomplete",
    "_has_build_stamps",
    "_has_dist_artifacts",
    "_load_build_failure",
    "_remove_build_failure",
    "_remove_build_ok",
    "_restore_moved",
    "_save_build_failure",
    "_save_build_ok",
    "clean_dist",
]

_logger = logging.getLogger(__name__)

# 清理 dist 时保留的 NSIS 脚本文件名（便于改代码后重新打包分发）
_KEEP_NSI = "installer.nsi"

# 构建失败标记文件：构建异常时写入，下次 fsp b 检测到时提示用户
_BUILD_FAILED = ".build_failed"

# 构建成功完成标记文件：构建成功后写入，构建开始时删除（与 .build_failed 的
# 写入/删除点对齐）。no_pyc 与交叉构建场景不产出 .pyc_stamp/.nuitka_compile_stamp，
# 此标记确保半成品检测不误判"恒有残留"
_BUILD_OK = ".build_ok"

# 编译阶段产出的 stamp 文件名：存在即说明上次构建至少完成到编译阶段
_PYC_STAMP = ".pyc_stamp"
_NUITKA_STAMP = ".nuitka_compile_stamp"


def _has_dist_artifacts(dist_dir: Path) -> bool:
    """dist 目录是否含构建产物（子目录或 .exe，排除 NSI/诊断文件）."""
    return any(
        p.name not in (_KEEP_NSI, _BUILD_FAILED) and (p.is_dir() or p.suffix == ".exe") for p in dist_dir.iterdir()
    )


def _has_build_stamps(dist_dir: Path) -> bool:
    """dist 目录是否含构建完成 stamp 文件（说明上次构建至少完成到编译阶段）.

    ``.build_ok`` 为通用完成标记：``no_pyc`` 或交叉构建场景不产出
    ``.pyc_stamp``/``.nuitka_compile_stamp``，此前恒判"残留"导致二次构建误报。
    三者任一存在即视为已完成。
    """
    return any((dist_dir / name).is_file() for name in (_PYC_STAMP, _NUITKA_STAMP, _BUILD_OK))


def _save_build_ok(dist_dir: Path) -> None:
    """构建成功完成后写入 ``dist/.build_ok`` JSON（记录完成时间戳）.

    与 ``.build_failed`` 的写入点（``build()`` 的异常分支）对齐：成功分支写入。
    dist 目录不存在时跳过；写入失败 best-effort（OSError 仅告警不阻断）。
    """
    if not dist_dir.is_dir():
        return
    data = {"timestamp": datetime.now().isoformat(timespec="seconds")}
    try:
        atomic_write_text(dist_dir / _BUILD_OK, json.dumps(data, ensure_ascii=False))
    except OSError as e:
        _logger.warning("写入 .build_ok 失败: %s", e)


def _remove_build_ok(dist_dir: Path) -> None:
    """构建开始时删除旧的 ``.build_ok`` 标记（如存在）.

    与 ``.build_failed`` 的删除点（构建成功后）对齐：``.build_ok`` 在构建
    开始时删除，保证中途中断/失败的构建不残留"成功完成"标记。
    """
    path = dist_dir / _BUILD_OK
    if path.is_file():
        try:
            path.unlink()
        except OSError as e:
            _logger.warning("删除 .build_ok 失败: %s", e)


def _handle_dist_incomplete(dist_dir: Path, auto_clean: bool) -> None:
    """检测 dist 半成品并按 auto_clean 决定自动清理或告警.

    iter-140 引入：替代 iter-128 的 ``_warn_dist_incomplete``，扩展支持
    ``.build_failed`` 标记检测与 ``--auto-clean`` 自动清理。

    检测条件（任一即视为半成品）：

    - dist 含构建产物但缺少编译 stamp 文件（中断/失败的构建残留）
    - dist 含 ``.build_failed`` 标记（上次构建异常退出）

    ``auto_clean=True`` 时调用 :func:`_clean_dist_dir` 清空 dist（不保留诊断文件，
    全新开始）。``auto_clean=False`` 时仅告警，提示用户 ``fsp c`` 或
    ``fsp b --auto-clean``。

    ``.build_failed`` 存在时额外输出失败阶段与错误信息，便于用户定位问题。
    """
    if not dist_dir.is_dir():
        return

    failed_info = _load_build_failure(dist_dir)
    has_artifacts = _has_dist_artifacts(dist_dir)
    has_stamps = _has_build_stamps(dist_dir)

    if failed_info:
        from fspack.console import console

        stage = failed_info.get("stage", "未知")
        error = failed_info.get("error", "")
        timestamp = failed_info.get("timestamp", "")
        console.warn(f"上次构建失败（{timestamp}）：阶段 [{stage}]")
        if error:
            console.rich.print(f"  错误: {error}")

    is_incomplete = (has_artifacts and not has_stamps) or failed_info is not None
    if not is_incomplete:
        return

    if auto_clean:
        _logger.info("auto-clean: 清理 dist 残留: %s", dist_dir)
        _clean_dist_dir(dist_dir, keep_diagnostics=False)
    else:
        _logger.warning(
            "dist 目录含上次构建的残留: %s，建议执行 `fsp c` 清理或 `fsp b --auto-clean` 自动清理后重新构建。",
            dist_dir,
        )


def _save_build_failure(dist_dir: Path, tracker: BuildTracker, exc: BaseException) -> None:
    """构建异常时写入 ``dist/.build_failed`` JSON 记录失败信息.

    iter-140 引入：供下次 ``fsp b`` 检测并提示用户。``exc`` 接受
    ``BaseException``：``KeyboardInterrupt``/``SystemExit`` 等中断类异常同样
    写入标记（Ctrl+C 是半成品 dist 的常见成因）。记录内容：

    - ``stage``：失败时最后完成的阶段名（从 ``tracker.records`` 取末尾）
    - ``error``：异常类型与消息（截断到 500 字符避免文件过大）
    - ``timestamp``：ISO 格式时间戳

    dist 目录不存在时跳过（构建可能在创建 dist 前失败）。写入失败 best-effort
    （OSError 不阻断异常传播）。
    """
    if not dist_dir.is_dir():
        return

    records = tracker.records
    stage = records[-1].name if records else "未知"
    error_msg = f"{type(exc).__name__}: {exc}"
    if len(error_msg) > 500:
        error_msg = error_msg[:497] + "..."

    data = {
        "stage": stage,
        "error": error_msg,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        atomic_write_text(dist_dir / _BUILD_FAILED, json.dumps(data, ensure_ascii=False, indent=2))
    except OSError as e:
        _logger.warning("写入 .build_failed 失败: %s", e)


def _load_build_failure(dist_dir: Path) -> dict[str, str] | None:
    """读取 ``dist/.build_failed`` JSON，返回失败信息 dict.

    文件不存在或解析失败返回 None（不阻断构建流程）。读取 → 解析 → 根 dict
    校验的公共骨架委托 :func:`fspack._util.jsoncache.load_json_dict`
    （``delete_on_corrupt=False``：诊断文件不删除）；值统一转 ``str`` 为本函数外壳。
    """
    path = dist_dir / _BUILD_FAILED
    data = load_json_dict(path, delete_on_corrupt=False, logger=_logger)
    if data is None:
        return None
    return {k: str(v) for k, v in data.items()}


def _remove_build_failure(dist_dir: Path) -> None:
    """构建成功后删除 ``.build_failed`` 标记（如存在）."""
    path = dist_dir / _BUILD_FAILED
    if path.is_file():
        try:
            path.unlink()
        except OSError as e:
            _logger.warning("删除 .build_failed 失败: %s", e)


def _restore_moved(dist_dir: Path, moved: list[tuple[Path, str]]) -> None:
    """把已 move 出的保留文件恢复回 dist，避免 rmtree/move 失败时丢失.

    成功路径下 ``src`` 已 move 回 dist，``exists()`` 为 False 自然跳过；
    恢复失败仅告警（文件仍在临时目录，可人工找回）。

    :param moved: ``(临时目录内路径, 原文件名)`` 列表（见 :func:`_clean_dist_dir`）
    """
    to_restore = [(src, name) for src, name in moved if src.exists()]
    if not to_restore:
        return
    try:
        dist_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _logger.warning("重建 %s 失败: %s", dist_dir, e)
    for src, name in to_restore:
        try:
            shutil.move(str(src), str(dist_dir / name))
        except OSError as e:
            _logger.warning("恢复 %s 失败: %s", name, e)


def _clean_dist_dir(dist_dir: Path, *, keep_diagnostics: bool) -> None:
    """清空 dist 目录，按 keep_diagnostics 决定是否保留诊断文件.

    保留文件先 :func:`shutil.move` 到 dist 同级临时目录（``.fspack_keep_<uuid>``），
    再 rmtree dist、重建后 move 回——规避旧实现"读入内存 → rmtree → 写回"在
    rmtree 与写回之间崩溃导致保留文件丢失的窗口（move 方案下文件任一时刻
    都在磁盘上，崩溃后可恢复）。

    无保留文件时 dist 目录整体移除（不重建空目录）：清理语义上应"干净"，
    且空 dist 配残留 ``.build_ok`` 会让 :func:`_has_build_stamps` 误判 dist 有效。

    :param keep_diagnostics: True 时保留 ``installer.nsi``/``.build_failed``
        （供 ``fsp c`` 使用，用户排查后保留诊断信息；``.build_ok`` 是完成标记
        而非诊断信息，清理后无保留价值，一并删除）；False 时全清（供
        ``--auto-clean`` 使用，全新开始构建）。
    """
    if not dist_dir.is_dir():
        return

    keep_names: list[str] = [_KEEP_NSI]
    if keep_diagnostics:
        keep_names.append(_BUILD_FAILED)

    keep_dir = dist_dir.parent / f".fspack_keep_{uuid.uuid4().hex}"
    # (临时目录内路径, 原文件名)：move 出的保留文件，rmtree 失败时据此恢复
    moved: list[tuple[Path, str]] = []
    try:
        keep_dir.mkdir(parents=True)
        for name in keep_names:
            path = dist_dir / name
            if path.is_file():
                target = keep_dir / name
                shutil.move(str(path), str(target))
                moved.append((target, name))
                _logger.info("保留: %s", path)
        # 长路径安全删除：node_modules/.pnpm 等深层路径超 MAX_PATH 260 时
        # 普通 rmtree 抛 WinError 3 中途残留
        rmtree_longpath(dist_dir)
        if moved:
            # 有保留文件才重建 dist 存放；无保留文件时 dist 整体移除
            dist_dir.mkdir(parents=True, exist_ok=True)
            for src, name in moved:
                shutil.move(str(src), str(dist_dir / name))
    finally:
        _restore_moved(dist_dir, moved)
        if keep_dir.is_dir():
            try:
                keep_dir.rmdir()
            except OSError:
                # 目录非空说明有文件未能恢复，保留现场便于人工找回
                _logger.warning("临时保留目录未清空: %s", keep_dir)
    _logger.info("已清理: %s", dist_dir)


def clean_dist(project: Path) -> None:
    """清理项目下的 dist 目录，保留 ``installer.nsi`` 与 ``.build_failed``.

    ``fsp c`` 的实现（iter-140 扩展）：

    - ``installer.nsi``：NSIS 脚本，保留便于改代码后 ``fsp p --no-build`` 重打包
    - ``.build_failed``：失败诊断标记，保留便于用户排查上次构建失败原因
    - ``.build_ok``：成功完成标记，非诊断信息，随清理删除——避免残留标记让
      ``_has_build_stamps`` 在空 dist 上误判"已完成构建"

    无保留文件时 dist 目录整体移除（不重建空目录）；全清场景
    （``fsp b --auto-clean``）调用 :func:`_clean_dist_dir` 并传
    ``keep_diagnostics=False``。
    """
    dist = Path(project) / "dist"
    if not dist.is_dir():
        _logger.info("无 dist 目录可清理: %s", dist)
        return
    _clean_dist_dir(dist, keep_diagnostics=True)
