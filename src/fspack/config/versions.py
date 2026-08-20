"""Python 与 Nuitka 版本管理.

本模块从 :mod:`fspack.config` 抽离，含 Python embed/standalone 版本映射、
Nuitka 版本锁定，以及 PEP 440 ``requires-python`` 规范符解析与匹配。
``config.py`` 通过 re-export 保持公开 API 不变。

依赖 :mod:`fspack.platform` 与 :mod:`fspack.exceptions`，独立于
:mod:`fspack.config.models` / :mod:`fspack.config.parsing`。
"""

from __future__ import annotations

import codecs
import logging
import operator
import re
from collections.abc import Callable
from pathlib import Path

from fspack.exceptions import ProjectError
from fspack.platform import Platform

__all__ = [
    "DEFAULT_LINUX_PY_VERSION",
    "DEFAULT_NUITKA_VERSION",
    "DEFAULT_PY_VERSION",
    "KNOWN_EMBED_VERSIONS",
    "KNOWN_STANDALONE_VERSIONS",
    "NUITKA_VERSIONS",
    "_split_t_suffix",
    "known_versions",
    "nuitka_version_for",
    "resolve_py_version",
]

_logger = logging.getLogger(__name__)

# Windows embed python 版本映射：major.minor → 完整版本号
# python.org 在 minor 维护期内发布二进制 embed zip；进入 security-only 阶段后仅发
# 源码包，不再提供 embed zip。下表为各 minor 最后一个含二进制 installer 的版本：
#   3.8 → 3.8.10（EOL）、3.9 → 3.9.13、3.10 → 3.10.11、3.11 → 3.11.9、
#   3.12 → 3.12.10；3.13/3.14 仍处 bugfix 阶段，取最新发布版本。
KNOWN_EMBED_VERSIONS: dict[str, str] = {
    "3.8": "3.8.10",
    "3.9": "3.9.13",
    "3.10": "3.10.11",
    "3.11": "3.11.9",
    "3.12": "3.12.10",
    "3.13": "3.13.14",
    "3.14": "3.14.6",
    # free-threaded build（PEP 703/779）：版本号末尾 't' 后缀标记，仅 Win10+ 目标。
    # python.org 官方**不**提供 freethreaded embed zip（``python-3.13.Xt-embed-amd64.zip``
    # 不存在），仅随完整 installer 提供 ``python-3.13.Xt-amd64.exe``（含 t 变体运行时）。
    # Windows+t 目标改用 astral-sh python-build-standalone 的 ``-freethreaded-install_only``
    # tarball（见 KNOWN_STANDALONE_VERSIONS），DLL 名 python313t.dll（非 python313.dll）。
    # 此处保留 3.13t/3.14t 键供版本解析/展示，但 KNOWN_EMBED_VERSIONS 不参与 t 版本下载。
    # 不支持 Win7：上游重编译版（adang1345/PythonVista）无 t 变体，检测到 Win7 目标直接报错。
    "3.13t": "3.13.14t",
    "3.14t": "3.14.6t",
}

# Linux python-build-standalone 版本映射：major.minor → 完整版本号
# astral-sh 每个 release tag 持续构建每个 minor 的最新补丁版本（含 security-only
# 阶段的源码-only 版本），版本号须与 STANDALONE_RELEASE_TAG（runtime.py）实际提供
# 的一致，否则下载 404。3.8/3.9 已 EOL，astral-sh 不再发布。
KNOWN_STANDALONE_VERSIONS: dict[str, str] = {
    "3.10": "3.10.20",
    "3.11": "3.11.15",
    "3.12": "3.12.13",
    "3.13": "3.13.14",
    "3.14": "3.14.6",
    # free-threaded build：astral-sh python-build-standalone 自 20241011 起提供
    # -freethreaded-install_only 变体 tarball（版本号无 t 后缀），覆盖 Linux/macOS/Windows 三平台。
    "3.13t": "3.13.14t",
    "3.14t": "3.14.6t",
}

# 默认 Python 版本：从对应平台的版本表派生，确保 EMBED 与 STANDALONE 各自使用最新版。
# 更新版本表时默认值自动跟随，避免硬编码常量与版本表不同步。
DEFAULT_PY_VERSION = KNOWN_EMBED_VERSIONS["3.11"]
DEFAULT_LINUX_PY_VERSION = KNOWN_STANDALONE_VERSIONS["3.11"]


# Nuitka 版本按目标 Python major.minor 锁定。
# - 3.8/3.9：nuitka 2.5.1（2.x 末尾稳定版，4.x 已不再维护 Python 3.8 EOL）
# - 3.10+：nuitka 4.1.3（当前最新稳定版，覆盖 3.10/3.11/3.12/3.13/3.14）
# 键用 major.minor 与 KNOWN_*_VERSIONS 风格一致，避免每个补丁版本重复
# （3.11.9 与 3.11.15 共用 4.1.3）。
NUITKA_VERSIONS: dict[str, str] = {
    "3.8": "2.5.1",
    "3.9": "2.5.1",
    "3.10": "4.1.3",
    "3.11": "4.1.3",
    "3.12": "4.1.3",
    "3.13": "4.1.3",
    "3.14": "4.1.3",
    # free-threaded build（PEP 703/779）：Nuitka 4.1.3 起实验性支持 free-threaded，
    # 编译产物 .pyd 链接 python3XXt.dll（与标准版 ABI 不兼容，必须同源）。
    "3.13t": "4.1.3",
    "3.14t": "4.1.3",
}

# 默认 Nuitka 版本：py_version 不在 NUITKA_VERSIONS 时回退（如未来 3.15）。
DEFAULT_NUITKA_VERSION = "4.1.3"


def _split_t_suffix(version: str) -> tuple[str, bool]:
    """剥离版本号末尾 ``t`` 后缀（free-threaded build 标记），返回 (纯数字版本, 是否自由线程).

    PEP 703/779 free-threaded build 在版本号末尾加 ``t`` 后缀（如 ``3.13.14t``、
    ``3.13t``），非合法 PEP 440 版本号。比较/解析时需先剥离后缀（``int("14t")``
    会抛 ``ValueError``），仅在命名/abi tag 等场景使用 ``t`` 标记本身。

    Args:
        version: 可能含 ``t`` 后缀的版本号字符串。

    Returns:
        ``(纯数字版本, 是否自由线程)`` 二元组。无 ``t`` 后缀时第二项为 ``False``。
    """
    if version.endswith("t"):
        return version[:-1], True
    return version, False


def nuitka_version_for(py_version: str) -> str:
    """按目标 Python 版本返回锁定的 Nuitka 版本.

    支持自由线程版本（``py_version`` 末尾 ``t`` 后缀，如 ``3.13.14t``）：
    查表时用剥离 ``t`` 后的 ``major.minor`` 拼回含 ``t`` 的键，命中 ``3.13t``/
    ``3.14t`` 后返回锁定版本；未收录时回退 :data:`DEFAULT_NUITKA_VERSION`。

    Args:
        py_version: 完整 Python 版本号（如 ``3.11.9`` 或 ``3.13.14t``）。

    Returns:
        对应的 Nuitka 版本号（如 ``4.1.3``）；未知 Python 版本回退
        :data:`DEFAULT_NUITKA_VERSION`。
    """
    base, is_t = _split_t_suffix(py_version)
    major, minor = base.split(".")[:2]
    key = f"{major}.{minor}" + ("t" if is_t else "")
    return NUITKA_VERSIONS.get(key, DEFAULT_NUITKA_VERSION)


def known_versions(target: Platform) -> dict[str, str]:
    """按目标平台返回已知 Python 版本映射.

    Windows 用 :data:`KNOWN_EMBED_VERSIONS`（python.org embed zip 可用版本），
    Linux 与 macOS 用 :data:`KNOWN_STANDALONE_VERSIONS`（python-build-standalone
    release 可用版本）。两侧最新补丁版本可能不同：如 3.11 Windows 最新 embed
    为 3.11.9，Linux/macOS standalone 为 3.11.15。
    """
    if target in (Platform.LINUX, Platform.MACOS):
        return KNOWN_STANDALONE_VERSIONS
    return KNOWN_EMBED_VERSIONS


def _ver_key(v: str) -> tuple[int, ...]:
    # 先剥离 t 后缀：free-threaded 版本号末尾 't' 非数字，直接 int 会抛 ValueError。
    base, _ = _split_t_suffix(v)
    return tuple(int(x) for x in base.split("."))


# PEP 440 版本规范符正则：第三组捕获可选 ``.*`` 后缀（``==3.12.*`` 前缀匹配）
_SPEC_RE = re.compile(r"(>=|<=|==|!=|~=|>|<)\s*(\d+(?:\.\d+)*)(\.\*)?")


def _read_python_version(path: Path) -> str:
    """读取 ``.python-version`` 文件内容，自动识别 BOM 编码.

    ``.python-version`` 可能由不同编辑器保存为 UTF-8（含/不含 BOM）或 UTF-16，
    通过字节序标记自动选择解码方式。无 BOM 且非 UTF-8（如 GBK）时退回宽松
    解码（``errors="replace"``），避免 ``UnicodeDecodeError`` 崩溃命令。

    Args:
        path: ``.python-version`` 文件路径。

    Returns:
        去除首尾空白后的版本字符串。
    """
    data = path.read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig").strip()
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16").strip()
    # 无 BOM：优先按 UTF-8 严格解码；文件为 GBK/Latin-1 等非 UTF-8 编码且含
    # 非法字节时（无 BOM 无法自动识别），退回 errors="replace" 宽松解码，避免
    # UnicodeDecodeError 以原始 traceback 崩溃命令。版本号仅含 ASCII，宽松解码
    # 至多把非法字节替换为占位符，随后由 _normalize_py_version 校验时告警回退。
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace").strip()


def _normalize_py_version(version: str, versions: dict[str, str]) -> str | None:
    """将版本号规范化为完整版本（``major.minor.micro``）。

    短版本号（``major.minor``，如 ``"3.13"``）查 ``versions`` 映射得到完整版本号
    （如 ``"3.13.14"``）；完整版本号（>=3 段）原样返回；未知短版本号（无映射）
    告警并返回 ``None``，避免拼出错误下载 URL。

    Args:
        version: 用户输入的版本号，可能为短版本（``"3.13"``）或完整版本（``"3.13.14"``）。
        versions: 平台对应的已知版本映射（embed 或 standalone）。

    Returns:
        完整版本号字符串，或 ``None``（未知短版本号）。
    """
    if version in versions:
        return versions[version]
    if len(version.split(".")) >= 3:
        return version
    _logger.warning("版本号 %s 不在已知版本映射中", version)
    return None


def resolve_py_version(
    project_dir: Path,
    explicit: str | None,
    requires_python: str | None,
    default: str = DEFAULT_PY_VERSION,
    target: Platform = Platform.WINDOWS,
) -> str:
    """解析最终使用的 Python 版本。

    优先级：
    1. ``explicit``（``--py-version`` CLI 标志）—— 不满足 ``requires-python`` 时告警但仍使用
    2. ``.python-version`` 文件 —— 不满足 ``requires-python`` 时告警并回退到自动选择
    3. ``requires-python`` 约束 —— 自动选择最高兼容已知版本
    4. ``default``

    ``explicit`` 与 ``.python-version`` 均支持短版本号（如 ``"3.13"``），通过
    :func:`known_versions` 按目标平台选取映射（embed 或 standalone），映射为完整版本号
    （如 ``"3.13.14"``），避免拼出不存在的下载 URL。

    Args:
        project_dir: 项目目录，用于读取 ``.python-version``。
        explicit: ``--py-version`` CLI 显式指定的版本号。
        requires_python: ``pyproject.toml`` 的 ``requires-python`` 约束。
        default: 无任何线索时的默认版本。
        target: 目标平台，决定短版本号映射查 embed 还是 standalone 表。
    """
    versions = known_versions(target)
    if explicit:
        full = _normalize_py_version(explicit, versions)
        resolved = full if full is not None else explicit
        if requires_python and not _satisfies(resolved, requires_python):
            _logger.warning("Python %s 不满足 requires-python: %s", resolved, requires_python)
        return resolved

    pv_file = project_dir / ".python-version"
    if pv_file.is_file():
        pv = _read_python_version(pv_file)
        full = _normalize_py_version(pv, versions)
        if full is not None:
            if requires_python and not _satisfies(full, requires_python):
                _logger.warning(
                    ".python-version %s 不满足 requires-python: %s，自动选择兼容版本", full, requires_python
                )
            else:
                return full

    if requires_python:
        # 按目标平台选取候选版本：Windows 用 embed，Linux 用 standalone
        candidates = sorted(versions.values(), key=_ver_key, reverse=True)
        for ver in candidates:
            if _satisfies(ver, requires_python):
                return ver
        raise ProjectError(f"requires-python: {requires_python}，无已知兼容 python 版本")

    return default


def _satisfies_wildcard(ver_parts: tuple[int, ...], op: str, spec_parts: tuple[int, ...]) -> bool:
    """PEP 440 通配符前缀匹配：``==3.12.*`` 匹配任意以 ``3.12`` 开头的版本.

    版本前缀与规范版本比较，短的补 0 后取规范版本长度范围内的部分对比。
    """
    length = max(len(ver_parts), len(spec_parts))
    ver_prefix = ver_parts + (0,) * (length - len(ver_parts))
    ver_head = ver_prefix[: len(spec_parts)]
    if op == "==":
        return ver_head == spec_parts
    return ver_head != spec_parts  # !=


# 常序比较运算符 → operator 函数映射（_SPEC_RE 仅捕获这些运算符，直接查表无缺键风险）
_OP_FUNCS: dict[str, Callable[[tuple[int, ...], tuple[int, ...]], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}


def _satisfies_compatible(ver_parts: tuple[int, ...], spec_parts: tuple[int, ...], spec_ver: str) -> bool:
    """``~=`` 兼容发行符判定：下界为完整 spec，上界为 minor+1（限定 minor 系列）.

    示例：``~=3.11`` 匹配 ``3.11 <= ver < 3.12``，``~=3.11.5`` 匹配
    ``3.11.5 <= ver < 3.12.0``。PEP 440 要求 ``~=`` 至少两段（``~=3`` 非法），
    单段时 warning 后宽松跳过（返回 ``True``）。
    """
    if len(spec_parts) < 2:
        _logger.warning("忽略非法的单段兼容发行符: ~= %s", spec_ver)
        return True
    upper_parts = (spec_parts[0], spec_parts[1] + 1)
    length = max(len(ver_parts), len(spec_parts), len(upper_parts))
    ver_n = ver_parts + (0,) * (length - len(ver_parts))
    lower_n = spec_parts + (0,) * (length - len(spec_parts))
    upper_n = upper_parts + (0,) * (length - len(upper_parts))
    return lower_n <= ver_n < upper_n


def _satisfies(version: str, specifiers: str) -> bool:
    """检查版本是否满足 PEP 440 ``requires-python`` 规范符.

    支持通配符前缀匹配：``==3.12.*`` 匹配任意以 ``3.12`` 开头的版本
    （PEP 440 version prefix match），``!=3.12.*`` 则相反。
    支持兼容发行符 ``~=``：限定 minor 系列，如 ``~=3.11`` 匹配
    ``3.11 <= ver < 3.12``，``~=3.11.5`` 匹配 ``3.11.5 <= ver < 3.12.0``。

    支持自由线程版本（``version`` 末尾 ``t`` 后缀）：比较时剥离后缀按纯数字
    版本判定，``requires-python`` 规范符不区分 ``t`` 变体（标准版与 free-threaded
    版本号主体相同，``requires-python>=3.13`` 同时匹配 ``3.13.14`` 与 ``3.13.14t``）。

    整串无可识别规范符（如 ``"abc"``）时保持宽松放行（返回 ``True``），
    但记 warning 日志便于发现 ``requires-python`` 配置错误。
    """
    base, _ = _split_t_suffix(version)
    ver_parts = tuple(int(x) for x in base.split("."))
    matches = _SPEC_RE.findall(specifiers)
    if not matches:
        _logger.warning("requires-python 规范符无法解析，宽松放行: %r", specifiers)
        return True
    for op, spec_ver, wildcard in matches:
        spec_parts = tuple(int(x) for x in spec_ver.split("."))
        if wildcard:
            if not _satisfies_wildcard(ver_parts, op, spec_parts):
                return False
            continue
        if op == "~=":
            if not _satisfies_compatible(ver_parts, spec_parts, spec_ver):
                return False
            continue
        length = max(len(ver_parts), len(spec_parts))
        ver = ver_parts + (0,) * (length - len(ver_parts))
        spec = spec_parts + (0,) * (length - len(spec_parts))
        if not _OP_FUNCS[op](ver, spec):
            return False
    return True
