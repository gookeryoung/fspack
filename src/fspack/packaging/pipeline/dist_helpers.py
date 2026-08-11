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
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fspack._util.fsutil import atomic_write_text
from fspack._util.jsoncache import load_json_dict

if TYPE_CHECKING:
    from fspack.progress import BuildTracker

__all__ = [
    "_BUILD_FAILED",
    "_KEEP_NSI",
    "_NUITKA_STAMP",
    "_PYC_STAMP",
    "_clean_dist_dir",
    "_handle_dist_incomplete",
    "_has_build_stamps",
    "_has_dist_artifacts",
    "_load_build_failure",
    "_remove_build_failure",
    "_save_build_failure",
    "clean_dist",
]

_logger = logging.getLogger(__name__)

# 清理 dist 时保留的 NSIS 脚本文件名（便于改代码后重新打包分发）
_KEEP_NSI = "installer.nsi"

# 构建失败标记文件：构建异常时写入，下次 fsp b 检测到时提示用户
_BUILD_FAILED = ".build_failed"

# 编译阶段产出的 stamp 文件名：存在即说明上次构建至少完成到编译阶段
_PYC_STAMP = ".pyc_stamp"
_NUITKA_STAMP = ".nuitka_compile_stamp"


def _has_dist_artifacts(dist_dir: Path) -> bool:
    """dist 目录是否含构建产物（子目录或 .exe，排除 NSI/诊断文件）."""
    return any(
        p.name not in (_KEEP_NSI, _BUILD_FAILED) and (p.is_dir() or p.suffix == ".exe") for p in dist_dir.iterdir()
    )


def _has_build_stamps(dist_dir: Path) -> bool:
    """dist 目录是否含编译 stamp 文件（说明上次构建至少完成到编译阶段）."""
    return (dist_dir / _PYC_STAMP).is_file() or (dist_dir / _NUITKA_STAMP).is_file()


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


def _save_build_failure(dist_dir: Path, tracker: BuildTracker, exc: Exception) -> None:
    """构建异常时写入 ``dist/.build_failed`` JSON 记录失败信息.

    iter-140 引入：供下次 ``fsp b`` 检测并提示用户。记录内容：

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


def _clean_dist_dir(dist_dir: Path, *, keep_diagnostics: bool) -> None:
    """清空 dist 目录，按 keep_diagnostics 决定是否保留诊断文件.

    :param keep_diagnostics: True 时保留 ``installer.nsi`` 与 ``.build_failed``
        （供 ``fsp c`` 使用，用户排查后保留诊断信息）；False 时全清（供
        ``--auto-clean`` 使用，全新开始构建）。
    """
    if not dist_dir.is_dir():
        return

    keep_names: list[str] = [_KEEP_NSI]
    if keep_diagnostics:
        keep_names.append(_BUILD_FAILED)

    preserved: dict[str, str] = {}
    for name in keep_names:
        path = dist_dir / name
        if path.is_file():
            try:
                preserved[name] = path.read_text(encoding="utf-8")
                _logger.info("保留: %s", path)
            except OSError:
                pass

    shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    for name, content in preserved.items():
        try:
            (dist_dir / name).write_text(content, encoding="utf-8")
        except OSError as e:
            _logger.warning("恢复 %s 失败: %s", name, e)
    _logger.info("已清理: %s", dist_dir)


def clean_dist(project: Path) -> None:
    """清理项目下的 dist 目录，保留 ``installer.nsi`` 与 ``.build_failed``.

    ``fsp c`` 的实现（iter-140 扩展）：

    - ``installer.nsi``：NSIS 脚本，保留便于改代码后 ``fsp p --no-build`` 重打包
    - ``.build_failed``：失败诊断标记，保留便于用户排查上次构建失败原因

    全清场景（``fsp b --auto-clean``）调用 :func:`_clean_dist_dir` 并传
    ``keep_diagnostics=False``。
    """
    dist = Path(project) / "dist"
    if not dist.is_dir():
        _logger.info("无 dist 目录可清理: %s", dist)
        return
    _clean_dist_dir(dist, keep_diagnostics=True)
