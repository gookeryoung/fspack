"""Nuitka 失败文件列表管理.

与编译流程解耦的纯模块级函数，不依赖 mixin 类。被测试 patch 的名字
（``_atomic_write_text``/``_safe_unlink``）由
:mod:`fspack.packaging.nuitka.compile` 顶层 re-export 维持导入路径兼容。

职责：

- 失败文件列表（``dist/.nuitka_failed_files.json``）：记录上次构建编译失败的
  文件 POSIX 路径，**仅作诊断**——每次构建全量重编，不据此跳过文件
  （跳过会让用户修复后的文件永不编译）。

与 stamp 文件同目录（``dist/``），删除 dist 时一并清理，避免跨构建污染。
写入采用原子写（临时文件 + rename），避免半写入被下次构建误读。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fspack.fsutil import atomic_write_text, safe_unlink

# 共享 logger 名：测试用 caplog.at_level(..., logger="fspack.packaging.nuitka") 锁定
_logger = logging.getLogger("fspack.packaging.nuitka")

# 延迟 dispatch 缓存：保存已解析的 compile 模块级函数引用，避免每次调用都 import。
# 仅在首次调用时解析一次，后续直接用。模块对象是单例，monkeypatch.setattr 修改的
# 就是同一对象的属性，故缓存引用后 patch 仍然生效（getattr 每次都动态获取）。
_compile_mod_holder: list[Any] = [None]


def _dispatch(fn_name: str, fallback_fn: Callable[..., Any]) -> Callable[..., Any]:
    """返回 compile 模块级的 ``fn_name`` 函数，不可用时 fallback 到 ``fallback_fn``.

    测试中常用 ``monkeypatch.setattr("fspack.packaging.nuitka.compile.<fn>", ...)``
    替换薄封装注入 OSError。由于本模块 indexes 与 compile 存在潜在循环导入，采用
    **运行时延迟导入 + 动态 getattr**：

    - 首次调用时尝试 ``import fspack.packaging.nuitka.compile``
      （此时 compile 已完成顶层初始化，re-export 名字就绪）
    - 每次调用 ``getattr(mod, fn_name, None)`` 动态拿属性，保证
      monkeypatch 后属性变化能被感知（缓存的是模块对象，不是函数引用）
    - 获取不到 fallback 到 :mod:`fspack.fsutil` 的原始实现
    """
    mod = _compile_mod_holder[0]
    if mod is None:
        try:
            from fspack.packaging.nuitka import compile as _compile_mod

            mod = _compile_mod
            _compile_mod_holder[0] = mod
        except ImportError:
            return fallback_fn
    return getattr(mod, fn_name, fallback_fn)


def _atomic_write_text_fallback(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    """原子写 fallback：直接调 util 层实现（compile 层 patch 不可用时）."""
    atomic_write_text(target, content, encoding=encoding)


def _safe_unlink_fallback(path: Path) -> None:
    """safe_unlink fallback：直接调 util 层实现，沿用本模块 logger（compile 层 patch 不可用时）."""
    safe_unlink(path, logger=_logger)


def _atomic_write_text_dispatch(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    """走 dispatch 的原子写：优先 compile 层的 _atomic_write_text（保证 monkeypatch 生效）."""
    _dispatch("_atomic_write_text", _atomic_write_text_fallback)(target, content, encoding=encoding)


def _safe_unlink_dispatch(path: Path) -> None:
    """走 dispatch 的 safe_unlink：优先 compile 层的 _safe_unlink（保证 monkeypatch 生效）."""
    _dispatch("_safe_unlink", _safe_unlink_fallback)(path)


def _failed_files_path(dist_dir: Path) -> Path:
    """返回 Nuitka 失败文件列表路径：``dist/.nuitka_failed_files.json``.

    记录上次构建编译失败的 .py 文件相对 ``src_dir`` 的 POSIX 路径。与 stamp
    文件同目录（dist/），删除 dist 时一并清理。仅作诊断记录，
    :meth:`NuitkaCompile.compile_with_stamp` 不据此跳过任何文件。
    """
    return dist_dir / ".nuitka_failed_files.json"


def _load_failed_files(dist_dir: Path) -> frozenset[str]:
    """读取失败文件列表，返回相对 ``src_dir`` 的 POSIX 路径集合.

    文件不存在或损坏返回空 frozenset（不影响构建，相当于"无上次失败文件"）。
    内容损坏时删除文件，与依赖解析缓存的"损坏即删"策略一致。
    """
    path = _failed_files_path(dist_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except (OSError, UnicodeDecodeError):
        _logger.warning("读取失败文件列表失败，视为空: %s", path)
        return frozenset()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        _logger.warning("失败文件列表损坏，删除并重建: %s: %s", path, e)
        _safe_unlink_dispatch(path)
        return frozenset()
    if not isinstance(data, list):
        _logger.warning("失败文件列表非 list，删除并重建: %s", path)
        _safe_unlink_dispatch(path)
        return frozenset()
    # 类型校验：仅保留 str 条目
    return frozenset(s for s in data if isinstance(s, str))


def _save_failed_files(dist_dir: Path, failed_files: list[str]) -> None:
    """写入失败文件列表到 ``dist/.nuitka_failed_files.json``.

    原子写入（与 stamp 一致，避免半写入）。空列表也写入（覆盖上次
    失败记录，表示本次无失败）。任何 I/O 错误仅告警不中断构建（失败文件
    列表是诊断记录，写入失败不影响主流程）。
    """
    path = _failed_files_path(dist_dir)
    try:
        _atomic_write_text_dispatch(path, json.dumps(failed_files, ensure_ascii=False, indent=2))
    except OSError as e:
        _logger.warning("写入失败文件列表失败（不影响构建）: %s: %s", path, e)


# 对外暴露的模块级名字（供 compile.py re-export，维持
# fspack.packaging.nuitka.compile._atomic_write_text / _safe_unlink patch 路径）。
# 注意：compile.py 顶层会 re-export 这两个名字为它自己模块属性，然后 monkeypatch
# 会修改 compile 模块的属性。本模块 indexes 内的实际调用通过 dispatch 机制走 compile 层
# 的同名属性，保证 patch 生效。
_atomic_write_text = _atomic_write_text_dispatch
_safe_unlink = _safe_unlink_dispatch
