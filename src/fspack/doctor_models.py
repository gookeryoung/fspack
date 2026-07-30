"""``fsp doctor`` 核心数据模型.

定义 :class:`CheckStatus`/:class:`CheckResult`/:class:`DoctorReport`/
:class:`TemplateRunResult`/:class:`TemplateBuildResult` 等不可变数据类，
供 :mod:`fspack.cli_doctor` facade 与各职责子模块
（:mod:`fspack.doctor_envs`/``doctor_tools``/``doctor_report``/
``doctor_templates``/``doctor_bench``）共享。

独立成模块避免 facade ↔ 子模块循环导入：子模块从本模块 import 数据类，
facade 从子模块 import 函数并 re-export 数据类保持 ``fspack.cli_doctor.XXX``
导入路径兼容。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
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
