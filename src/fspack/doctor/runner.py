"""``fsp doctor`` 诊断编排：``run_doctor`` 聚合入口 + 缓存完整性检查入口.

从 :mod:`fspack.doctor` facade 迁入的业务实现（facade 仅保留 re-export）。
``run_doctor`` 按当前平台过滤并聚合环境信息与工具检查项，
``run_doctor_cache_check`` 执行缓存完整性检查并渲染结果。

内部调用的 ``_check_*`` 函数通过本模块全局名字解析——拦截 ``run_doctor``
内部调用的 monkeypatch 请 patch ``fspack.doctor.runner._check_*``（定义所在
模块）；patch ``fspack.doctor.shutil.which``/``subprocess.Popen`` 等标准库
模块属性仍全局生效。
"""

from __future__ import annotations

from typing import Callable

from fspack.doctor.cache_contents import _cache_content_fns
from fspack.doctor.envs import (
    _check_cache_dir,
    _check_cache_integrity,
    _check_fspack_version,
    _check_mirror_config,
    _check_platform_info,
    _check_python,
)
from fspack.doctor.models import CheckResult, DoctorReport
from fspack.doctor.report import _build_table
from fspack.doctor.tools import (
    _check_clang,
    _check_gcc,
    _check_makensis_on_linux,
    _check_mingw,
    _check_nsis,
    _check_pillow,
    _check_pip,
    _check_uv,
    _check_wine,
)
from fspack.doctor.win7 import _check_win7_compat


def run_doctor() -> DoctorReport:
    """执行环境诊断，返回完整报告.

    按当前平台过滤工具检查项（Windows 查 mingw/NSIS，Linux 查 gcc/wine），
    聚合环境信息与工具检查结果。不抛异常：所有失败转为 :class:`CheckResult`
    标记 ERROR/WARN，便于报告统一渲染。

    慢速检查（工具版本子进程探测/缓存目录扫描/Win7 zip 抽检）用
    :class:`~concurrent.futures.ThreadPoolExecutor` 并行执行：每项工具
    探测 spawn 子进程 ~150ms，串行 5 项 ~0.75s，并行后墙钟时间取决于
    最慢一项（subprocess 等待释放 GIL，线程是真并行）。``executor.map``
    保序，报告各项顺序与串行执行完全一致。前 4 项环境信息为纯内存/
    配置读取（<5ms），保持串行避免线程池调度开销。
    """
    from concurrent.futures import ThreadPoolExecutor

    from fspack.config import DEFAULT_MIRROR, MIRRORS
    from fspack.config.cache import cache_root
    from fspack.platform import Platform, detect_platform

    platform = detect_platform()
    # 快速检查串行：纯内存/配置读取，无 I/O 等待
    env_info: list[CheckResult] = [
        _check_python(),
        _check_platform_info(platform),
        _check_fspack_version(),
        _check_mirror_config(DEFAULT_MIRROR, MIRRORS),
    ]

    # 慢速检查并行：缓存目录扫描 + Win7 兼容自检（仅 Windows）+ 压缩包缓存
    # 内容盘点 + 全部工具检查。函数对象在函数体内经模块全局名字解析，
    # monkeypatch ``fspack.doctor.runner._check_*`` 语义与串行版一致；
    # 缓存内容盘点项定义在 :mod:`fspack.doctor.cache_contents`，patch 对应
    # 函数需落在该定义模块。
    slow_env_fns: list[Callable[[], CheckResult]] = [lambda: _check_cache_dir(cache_root())]
    if platform is Platform.WINDOWS:
        # Win7 兼容自检（清单对齐/shim 资产/缓存 zip 抽检）仅 Windows 目标相关
        slow_env_fns.append(_check_win7_compat)
    # 压缩包缓存内容盘点（embed/standalone/nuitka/tkinter/winlibs 版本清单，
    # 离线排查缓存缺失）
    slow_env_fns.extend(_cache_content_fns(platform))

    # 通用工具：pip/uv/Pillow（两平台都需要）
    tool_fns: list[Callable[[], CheckResult]] = [_check_pip, _check_uv, _check_pillow]
    if platform is Platform.WINDOWS:
        tool_fns.extend([_check_mingw, _check_nsis])
    elif platform is Platform.MACOS:
        tool_fns.append(_check_clang)
    else:
        tool_fns.extend([_check_gcc, _check_wine, _check_makensis_on_linux])

    all_fns = slow_env_fns + tool_fns
    with ThreadPoolExecutor(max_workers=min(len(all_fns), 8)) as pool:
        results = list(pool.map(lambda fn: fn(), all_fns))
    env_info.extend(results[: len(slow_env_fns)])
    tool_checks = results[len(slow_env_fns) :]

    return DoctorReport(
        env_info=tuple(env_info),
        tool_checks=tuple(tool_checks),
    )


def run_doctor_cache_check() -> CheckResult:
    """执行缓存完整性检查，渲染结果到控制台并返回 :class:`CheckResult`.

    iter-128 引入：``fsp doctor --check-cache`` 调用。
    iter-139 扩展：详情中追加 stale deps（引用缺失 wheel）与 orphan wheels
    （未被任何 deps 引用）计数，并提示用户用 ``fsp cache clean`` 清理。
    """
    from fspack.config.cache import wheel_cache_dir
    from fspack.console import console

    result = _check_cache_integrity(wheel_cache_dir())
    console.step("缓存完整性检查")
    console.rich.print(_build_table("缓存完整性", (result,)))
    return result
