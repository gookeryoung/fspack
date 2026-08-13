"""``fsp doctor`` 核心数据模型.

定义 :class:`CheckStatus`/:class:`CheckResult`/:class:`DoctorReport`/
:class:`TemplateRunResult`/:class:`TemplateBuildResult` 等不可变数据类，
供 :mod:`fspack.doctor` facade 与各职责子模块
（:mod:`fspack.doctor.envs`/``doctor.tools``/``doctor.report``/
``doctor.templates``/``doctor.bench``）共享。

独立成模块避免 facade ↔ 子模块循环导入：子模块从本模块 import 数据类，
facade 从子模块 import 函数并 re-export 数据类保持 ``fspack.doctor.XXX``
导入路径兼容。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "CacheHealthReport",
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "TemplateBuildResult",
    "TemplateRunResult",
]


class CheckStatus(str, Enum):
    """诊断项状态：OK 绿 / WARN 黄 / ERROR 红.

    继承 ``str`` 便于序列化与测试断言（``status == "ok"``）。
    """

    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    """单项诊断结果.

    :param name: 诊断项名称（如 ``"mingw-w64"``/``"Python"``）
    :param status: 状态（:class:`CheckStatus`）
    :param detail: 详细信息（如版本号 ``"13.2.0"`` 或路径 ``"C:\\...\\gcc.exe"``）
    :param suggestion: 修复建议（ERROR/WARN 时填，OK 时为空字符串）
    """

    name: str
    status: CheckStatus
    detail: str
    suggestion: str = ""


@dataclass(frozen=True)
class DoctorReport:
    """完整诊断报告：环境信息 + 工具检查结果列表."""

    env_info: tuple[CheckResult, ...]
    tool_checks: tuple[CheckResult, ...]

    @property
    def has_error(self) -> bool:
        """是否存在 ERROR 级别诊断项（任一即阻塞打包）."""
        return any(c.status is CheckStatus.ERROR for c in self.tool_checks)

    @property
    def has_warn(self) -> bool:
        """是否存在 WARN 级别诊断项（不阻塞但建议处理）."""
        return any(c.status is CheckStatus.WARN for c in self.tool_checks)


@dataclass(frozen=True)
class TemplateRunResult:
    """单个模板运行验证结果.

    构建成功后实际运行产出的可执行文件，验证「能调用」：
    CLI 应用应正常退出（退出码 0）；GUI/Web 应用启动后进入事件循环
    不退出，用超时判定为「启动成功」并主动终止进程。

    :param success: 运行是否成功（退出码 0 或超时视为 GUI 事件循环正常）
    :param timed_out: 是否因超时被终止（True 表示应用进入事件循环不退出，
        视为 GUI/Web 应用启动成功；False 表示进程自行退出）
    :param exit_code: 进程退出码（超时被终止时为 ``None``）
    :param duration_sec: 运行耗时（秒，超时场景为超时阈值）
    :param error: 失败时的错误信息（stderr 首行，截断到 200 字符）
    """

    success: bool
    timed_out: bool
    exit_code: int | None
    duration_sec: float
    error: str = ""


@dataclass(frozen=True)
class TemplateBuildResult:
    """单个模板构建结果.

    :param template_id: 模板目录名
    :param success: 构建是否成功
    :param duration_sec: 总构建耗时（秒）
    :param error: 失败时的错误信息（截断到 200 字符）
    :param dist_size: 构建产物 ``dist/`` 目录大小（字节），失败时为 0
    :param entry_count: 入口 exe 数量
    :param run_result: 构建成功后的运行验证结果；构建失败或未运行验证时为 ``None``
    """

    template_id: str
    success: bool
    duration_sec: float
    error: str = ""
    dist_size: int = 0
    entry_count: int = 0
    run_result: TemplateRunResult | None = None


@dataclass(frozen=True)
class CacheHealthReport:
    """缓存目录健康扫描报告（iter-139 引入，多 cache 类型扩展）.

    由各 ``_scan_*_health`` 函数生成，供 :func:`fspack.doctor.run_doctor_cache_check`/
    ``run_cache_status``/``run_cache_clean`` 复用，避免重复扫描。

    字段分两类：

    - **wheels 专用**（``total_deps_files``/``corrupt_deps_files``/``stale_deps_files``/
      ``missing_wheels``/``orphan_wheels``/``total_wheels``/``orphan_size_bytes``）：
      由 :func:`fspack.doctor.envs._scan_cache_health` 填充，描述 wheels 目录特有的
      ``.deps-*.json`` ↔ ``*.whl`` 引用关系。
    - **通用**（``cache_type``/``corrupt_files``/``stale_files``/``orphan_files``/
      ``total_files``/``issues_size_bytes``）：所有 cache 类型共用，描述无引用关系
      的纯文件级健康状态（embed/standalone/nuitka/loaders/ccache/tkinter 用）。

    扫描期间已删除 JSON 损坏的 ``.deps-*.json`` 文件（与 iter-128
    :func:`_check_cache_integrity` 行为一致）；``stale_deps`` 与 ``orphan_wheels``
    需显式 :func:`_clean_cache_issues` 才会被删除。其他 cache 类型的损坏文件
    同样在扫描阶段 best-effort 删除，过期/孤儿文件需 ``--stale`` 显式启用清理。

    :param cache_dir: 扫描的缓存目录
    :param cache_type: cache 类型标识（``"wheels"``/``"embed"``/``"standalone"``/
        ``"nuitka"``/``"loaders"``/``"ccache"``/``"tkinter"``），默认 ``"wheels"``
        保持向后兼容
    :param total_deps_files: 扫描到的 ``.deps-*.json`` 文件总数（含已删除的损坏文件）
    :param corrupt_deps_files: JSON 结构损坏已删除的 ``.deps-*.json`` 文件名列表
    :param stale_deps_files: 引用了不存在 wheel 的 ``.deps-*.json`` 文件名列表
        （文件本身 JSON 有效，但 ``wheels`` 列表指向已缺失的 wheel，未删除）
    :param missing_wheels: 被 ``.deps-*.json`` 引用但 cache_dir 中不存在的 wheel 文件名列表
    :param orphan_wheels: cache_dir 中未被任何 ``.deps-*.json`` 引用的 wheel 文件名列表
    :param total_wheels: cache_dir 中 wheel 文件总数（``*.whl``，含孤儿）
    :param orphan_size_bytes: 孤儿 wheel 占用字节数（供清理建议展示收益）
    :param corrupt_files: 通用损坏文件名列表（非 wheels cache 类型用，扫描期已删除）
    :param stale_files: 通用过期文件名列表（如版本不在 KNOWN_*_VERSIONS 的旧 zip/tar）
    :param orphan_files: 通用孤儿文件名列表（未被引用的残留产物）
    :param total_files: 通用文件总数（扫描的顶层文件数，wheels 用 total_deps/total_wheels）
    :param issues_size_bytes: 可释放字节数（清理 corrupt + stale + orphan 的累计体积）
    """

    cache_dir: Path
    cache_type: str = "wheels"
    # wheels 专用字段（保持向后兼容）
    total_deps_files: int = 0
    corrupt_deps_files: tuple[str, ...] = ()
    stale_deps_files: tuple[str, ...] = ()
    missing_wheels: tuple[str, ...] = ()
    orphan_wheels: tuple[str, ...] = ()
    total_wheels: int = 0
    orphan_size_bytes: int = 0
    # 通用字段（embed/standalone/nuitka/loaders/ccache/tkinter 用）
    corrupt_files: tuple[str, ...] = ()
    stale_files: tuple[str, ...] = ()
    orphan_files: tuple[str, ...] = ()
    total_files: int = 0
    issues_size_bytes: int = 0

    @property
    def has_issues(self) -> bool:
        """是否存在需要清理的问题（wheels 专用 + 通用 任一非空即 True）."""
        return bool(
            self.corrupt_deps_files
            or self.stale_deps_files
            or self.orphan_wheels
            or self.corrupt_files
            or self.stale_files
            or self.orphan_files
        )

    @property
    def issues_count(self) -> int:
        """问题文件总数（wheels 专用 + 通用三类合计），便于汇总展示."""
        return (
            len(self.corrupt_deps_files)
            + len(self.stale_deps_files)
            + len(self.orphan_wheels)
            + len(self.corrupt_files)
            + len(self.stale_files)
            + len(self.orphan_files)
        )
