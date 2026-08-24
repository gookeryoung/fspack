"""fspack CLI 参数解析器构建（argparse 声明集中维护）.

从 :mod:`fspack.cli` 拆分而来：parser 构建代码（argparse 声明）与命令分发
逻辑分离，``cli.py`` 聚焦 ``main``/dispatch。顶部仅导入轻量标准库与
``__version__``；``--mirror`` 刻意不做 argparse choices 校验（choices 会在
parser 构建期触发 ``fspack.config`` 导入 ~20ms），改由
:func:`fspack.cli._resolve_mirror` 在执行期校验。

原先按子命令拆分为 ``cli_cmds_build``/``cli_cmds_package``/``cli_cmds_init``/
``cli_cmds_doctor``/``cli_cmds_manifest`` 五个模块，本次整合为单文件：参数声明
用 :class:`_Opt` 规格 + :func:`_add_options` 数据驱动批量注册，消除逐条
``add_argument`` 的样板代码（build 子命令 47 个参数从 240 行压缩为一张规格表）。
manifest 的执行逻辑（``_run_manifest`` 等）移入 :mod:`fspack.cli`。
"""

from __future__ import annotations

import argparse
from typing import Any, NamedTuple

from fspack import __version__

__all__ = ["build_parser"]


# ``default`` 字段的哨兵：区分"显式 default=None"与"不传 default"。
# argparse 的 store_true 不接受 default 以外的多数参数，用哨兵跳过未设置项。
_UNSET: object = object()


class _Opt(NamedTuple):
    """单个 argparse 参数规格（数据驱动批量注册用）.

    字段与 :meth:`argparse.ArgumentParser.add_argument` 参数一一对应，未设置的
    字段用 :data:`_UNSET` 哨兵跳过（不传给 ``add_argument``，由 argparse 用其
    自身默认值）。``flags`` 为位置参数名（如 ``("project",)``）或选项名
    （如 ``("-R", "--recursive")``）。
    """

    flags: tuple[str, ...]
    help: str
    action: str = ""  # "store_true" / "append"，空串表示普通存值
    default: object = _UNSET
    dest: str | None = None
    metavar: str | None = None
    choices: tuple[object, ...] | None = None
    type: object = None
    nargs: str | None = None
    const: object = _UNSET


def _add_options(parser: argparse.ArgumentParser, opts: tuple[_Opt, ...]) -> None:
    """按规格表批量注册参数，跳过未设置（``_UNSET``）的字段."""
    for opt in opts:
        # 用 Any 承载动态 kwargs：argparse.add_argument 是重载函数，字段值类型
        # 各异（str/bool/int/list...），dict[str, object] 解包会被类型检查器
        # 拒绝（object 不可赋给具体形参），dict[str, Any] 是动态 kwargs 惯用法。
        kwargs: dict[str, Any] = {"help": opt.help}
        if opt.action:
            kwargs["action"] = opt.action
        if opt.default is not _UNSET:
            kwargs["default"] = opt.default
        if opt.dest is not None:
            kwargs["dest"] = opt.dest
        if opt.metavar is not None:
            kwargs["metavar"] = opt.metavar
        if opt.choices is not None:
            kwargs["choices"] = list(opt.choices)
        if opt.type is not None:
            kwargs["type"] = opt.type
        if opt.nargs is not None:
            kwargs["nargs"] = opt.nargs
        if opt.const is not _UNSET:
            kwargs["const"] = opt.const
        parser.add_argument(*opt.flags, **kwargs)


# 位置参数：可选的项目目录，默认当前目录。多个子命令复用。
_PROJECT_ARG = _Opt(("project",), "项目目录（默认当前目录）", default=".", nargs="?")

# 三个子命令（build/package）共享的公共选项：镜像源 + py 版本 + 目标平台。
_MIRROR_OPT = _Opt(
    ("--mirror",),
    "镜像源（huawei/aliyun/tsinghua，默认 aliyun）",
    default=None,
    metavar="MIRROR",
)
_PY_VERSION_OPT = _Opt(("--py-version",), "embed python 版本，如 3.11.9", default=None)
_TARGET_OPT = _Opt(
    ("--target",),
    "目标平台（默认当前平台）",
    default=None,
    choices=("windows", "linux", "macos"),
)


# ---------- build 子命令参数规格 ----------
# choices 刻意不写在 --mirror：避免 build_parser() 构建期导入 fspack.config（~20ms）；
# 合法性由 _resolve_mirror 在执行期校验（退出码与 argparse 一致为 2）。
# 镜像键列表与 config.models.MIRRORS 同步维护（有测试守护）。
_BUILD_OPTS: tuple[_Opt, ...] = (
    _PROJECT_ARG,
    _MIRROR_OPT,
    _PY_VERSION_OPT,
    _TARGET_OPT,
    _Opt(
        ("--keep-module",),
        "显式保留子模块（如 PySide2.QtGui），可重复指定",
        action="append",
        default=[],
        dest="keep_modules",
    ),
    _Opt(
        ("--icon",),
        "exe 图标文件路径（.ico/.png/.jpg 等），覆盖 [tool.fspack] icon；"
        "未指定时按 [tool.fspack] icon > 自动搜索 favicon.* > 默认 app.ico 解析",
        default=None,
    ),
    _Opt(
        ("--no-stdlib-trim",),
        "关闭标准库精简（默认剥离 Linux standalone 的 test/ensurepip/idlelib 等无用模块）",
        action="store_true",
    ),
    _Opt(
        ("--no-slim-runtime",),
        "关闭 standalone runtime 精简（默认 strip libpython 调试符号省 ~34MB + "
        "删 python3.X 二进制省 ~53MB + 删 include/share 省 ~9MB + 非 tkinter 项目剥离 Tcl/Tk 省 ~9MB）。"
        "调试 Python 解释器本身或需要保留开发期文件时使用",
        action="store_true",
    ),
    _Opt(
        ("--no-stdlib-zip",),
        "关闭 Linux/macOS 标准库 zip 化（默认打包为 lib/pythonXY[t].zip，"
        "省去 stdlib 目录 stat 遍历，冷启动提速 30-80ms）",
        action="store_true",
    ),
    _Opt(
        ("--splash",),
        "启用 Windows splash 启动画面（默认关闭）：loader 启动期显示应用名无边框画面，"
        "GUI 首窗口出现/WEB server 启动/30s 超时自动关闭。仅 Windows 目标生效",
        action="store_true",
    ),
    _Opt(
        ("--no-pyc",),
        "关闭字节码预编译（默认预编译 src+site-packages 为 .pyc 加速首次启动）",
        action="store_true",
    ),
    _Opt(
        ("--pyc-strip",),
        "剥离非 __init__.py 的 .py 源码（仅保留 .pyc，需配合预编译；保留包标识避免命名空间包问题）",
        action="store_true",
    ),
    _Opt(
        ("--pyc-optimize",),
        "字节码优化级别：0=保留 docstring/assert，1=剥离 assert，"
        "2=剥离 assert+docstring（-OO，体积减 5-15%%，启动提速 5-10%%，默认 2）",
        default=None,
        choices=(0, 1, 2),
        type=int,
    ),
    _Opt(
        ("--no-site",),
        "禁用 site.py 加载（_pth 省略 import site 行，节省 ~20-30ms 启动时间）",
        action="store_true",
    ),
    _Opt(
        ("--nuitka",),
        "启用 Nuitka 编译模式：用户源码编译为 .pyd 本机执行（速度提升 30-50%%）。"
        "Nuitka 自动装到本地缓存 ~/.fspack/cache/nuitka/，不污染 dist/runtime；交叉构建自动跳过；默认关闭",
        action="store_true",
    ),
    _Opt(
        ("--ccache",),
        "Nuitka 编译启用 ccache 缓存：首次下载 ccache 到 ~/.fspack/cache/ccache/，"
        "后续构建缓存 gcc 编译结果加速重复编译。需配合 --nuitka 使用；默认关闭",
        action="store_true",
    ),
    _Opt(
        ("--nuitka-pkg",),
        "指定第三方依赖包名用 Nuitka 编译为 .pyd（可多次指定）。"
        "需配合 --nuitka 使用；编译 site-packages/<package>/ 下 .py 为 .pyd，"
        "编译成功删除 .py，失败保留回退 .pyc。风险由用户承担（动态导入/元编程可能不兼容）",
        action="append",
        default=None,
        metavar="PACKAGE",
        dest="nuitka_pkg",
    ),
    _Opt(
        ("--extra-index-url",),
        "额外 PyPI 索引 URL（私有 PyPI 服务器，可多次指定），透传给 pip/uv 的 --extra-index-url。"
        "与 [tool.fspack] extra-index-urls 合并（CLI 追加在配置之后，去重保留首次出现）",
        action="append",
        default=None,
        metavar="URL",
        dest="extra_index_urls",
    ),
    _Opt(
        ("--find-links",),
        "本地 wheel 目录或远程 wheel 索引页（可多次指定），透传给 pip/uv 的 --find-links。"
        "与 [tool.fspack] find-links 合并（CLI 追加在配置之后，去重保留首次出现）",
        action="append",
        default=None,
        metavar="PATH_OR_URL",
        dest="find_links",
    ),
    _Opt(
        ("-R", "--recursive"),
        "递归扫描 project 目录下所有含 pyproject.toml 的子项目，依次构建。"
        "跳过 .venv/dist/build/.git 等开发期目录；单项目失败不中断，"
        "最后汇总成功/失败列表",
        action="store_true",
    ),
    _Opt(
        ("--dry-run",),
        "仅预览打包计划，不执行实际构建（不下载/不编译/不复制文件）",
        action="store_true",
    ),
    _Opt(
        ("--no-size-report",),
        "关闭构建结束后的体积报告（默认输出 runtime/src/site-packages 分类与 Top 10 包占比）",
        action="store_true",
    ),
    _Opt(
        ("--log-file",),
        "将构建日志写入文件（含时间戳、级别、logger 名、消息、异常栈），便于 CI 上传与问题排查。"
        "文件以追加模式写入，UTF-8 编码；构建开始时创建、结束时自动关闭",
        default=None,
        metavar="PATH",
    ),
    _Opt(
        ("--log-format",),
        "日志文件格式：text=人类可读纯文本（默认），json=结构化 JSON（便于 ELK/Loki 采集）",
        default="text",
        choices=("text", "json"),
    ),
    _Opt(
        ("-P", "--profile"),
        "启用耗时分析报告：构建结束后输出各阶段 wall time/占比/缓存命中/下载/节省，"
        "以及资源总览（wall/CPU/CPU 占比/内存峰值），识别瓶颈阶段。"
        "用 tracemalloc 采集内存峰值（无新依赖）。"
        "同时写入性能日志 JSON（默认 <项目>/.benchmarks/fsp-b-<时间戳>.json），供历史对比",
        action="store_true",
    ),
    _Opt(
        ("-PO", "--profile-out"),
        "性能日志输出路径（需 --profile）：目录则自动命名写入，.json 文件则直写；默认 <项目>/.benchmarks/",
        default=None,
        metavar="PATH",
    ),
    _Opt(
        ("-PC", "--profile-compare"),
        "与历史性能日志对比（需 --profile）：不带值输出历次趋势表"
        "（近 15 次历史+本次，统计基准为环境一致历史的中位数，抗单次抖动），"
        "last=与最近一次对比，正整数=近 N 次趋势，也可指定基准 JSON 文件路径。"
        "差异表格标红回归/标绿改善，阶段仅列差异显著项（>50ms 且 >10%）",
        nargs="?",
        const="trend",
        default=None,
        metavar="REF",
    ),
    _Opt(
        ("--analyze-deps",),
        "启用二进制依赖分析：解析 .dll/.so/.dylib 依赖树，剥离无引用文件"
        "（如 Qt6Core.dll 依赖的 ICU 未用时仍保留）。"
        "Windows 用纯 Python 解析 PE 导入表，Linux 用 objdump，macOS 用 otool；"
        "默认关闭，分析耗时但典型项目体积减少 5-15%%",
        action="store_true",
    ),
    _Opt(
        ("--extra",),
        "启用的 [project.optional-dependencies] 分组（可多次指定，如 --extra gui --extra web）。"
        "等价 pip install pkg[extra] 语义：分组的依赖合并到下载集合；"
        "自引用 my-pkg[extra] 递归展开，第三方 pkg[extra] 原样透传 pip。"
        "指定时完全覆盖 [tool.fspack] extras 配置默认（集合语义，非合并）",
        action="append",
        default=[],
        metavar="NAME",
        dest="extras",
    ),
    _Opt(
        ("--lazy-import",),
        "延迟导入的顶层模块名（逗号分隔，如 --lazy-import numpy,pandas）。"
        "wrapper 注入 _LazyImportFinder meta path finder，首次 import 时不执行"
        "模块 __init__.py，首次属性访问时才加载，降低启动时间。"
        "典型收益：numpy 省 ~80ms，pandas 省 ~150ms；"
        "C 扩展模块（.pyd/.so）无法延迟，仍即时加载。"
        "指定时完全覆盖 [tool.fspack] lazy-imports 配置默认",
        default=None,
        metavar="MODULES",
        dest="lazy_imports",
    ),
    _Opt(
        ("--require-hashes",),
        "依赖下载强制哈希校验：透传 pip download --require-hashes，"
        "在线模式下要求所有 wheel 的 sha256 与 PyPI 声明一致。"
        "缓存命中时跳过校验（缓存目录 wheel 已首次校验）。"
        "启用时若依赖未声明哈希（如 sdist 回退构建的 wheel）会失败",
        action="store_true",
    ),
    _Opt(
        ("--no-sbom",),
        "关闭构建结束后的 SBOM 生成（默认输出 SPDX 2.3 兼容 JSON "
        "到 dist/release/<name>-<version>-sbom.json，含依赖名称/版本/许可证/SHA256）",
        action="store_true",
    ),
    _Opt(
        ("--no-manifest",),
        "关闭构建结束后的产物清单生成（默认输出 JSON "
        "到 dist/release/<name>-<version>-manifest.json，含每个文件的大小/SHA256/分类）",
        action="store_true",
        dest="no_manifest",
    ),
    _Opt(
        ("--no-win7-scan",),
        "关闭构建结束后的 Win7 兼容扫描（默认扫描 dist 下全部 .dll/.pyd/.exe "
        "导入表，输出文本报告到 dist/release/win7-compat-report.txt；仅 Windows 目标，"
        "loader exe 硬门禁不受此开关影响）",
        action="store_true",
        dest="no_win7_scan",
    ),
    _Opt(
        ("--no-win7-dll",),
        "关闭 Win7 兼容 DLL 注入（默认 3.9-3.11 注入 api-ms-win-core-path shim、"
        "3.12+ 整套替换为 GitHub 重编译版组件）。产物仅面向 Win8+/Win10+ 时启用，"
        "避免网络受限环境下载 GitHub 失败阻断构建；启用后产物不支持 Win7",
        action="store_true",
        dest="no_win7_dll",
    ),
    _Opt(
        ("--auto-clean",),
        "构建前自动清理 dist 残留（含上次失败标记 .build_failed），"
        "无需手动 fsp c。检测到半成品时：无此标志则告警并继续（可能因残留文件失败），"
        "有此标志则清空 dist 后重新构建",
        action="store_true",
    ),
    _Opt(
        ("--open-browser",),
        "WEB 应用启动后自动打开浏览器（webbrowser.open）。"
        "WEB 类型（Flask/FastAPI 等）默认启用，无需显式指定；"
        "非 WEB 类型显式指定时也启用（如 GUI 内嵌 WebView 场景）。"
        "与 [tool.fspack] open-browser 配置默认合并（CLI 或配置任一启用 → 启用）",
        action="store_true",
    ),
)


# ---------- run 子命令参数规格 ----------
_RUN_OPTS: tuple[_Opt, ...] = (
    _Opt(("project",), "项目目录", default=".", nargs="?"),
    _Opt(("rest",), "透传给目标程序的参数（以 -- 分隔）", default=[], nargs="*"),
    _Opt(("--debug",), "用 embed python 直跑入口脚本（绕过 GUI loader，输出可见）", action="store_true"),
    _Opt(("--entry",), "多入口项目指定要运行的入口名（与 [project.scripts] 键匹配）", default=None),
    _Opt(
        ("-P", "--profile"),
        "输出启动耗时剖析汇总（loader/环境准备/import 各阶段耗时）并生成性能日志",
        action="store_true",
    ),
    _Opt(
        ("-PO", "--profile-out"),
        "启动剖析日志输出路径（需 --profile）：目录则自动命名写入，.json 文件则直写；默认 <项目>/.benchmarks/",
        default=None,
        metavar="PATH",
    ),
    _Opt(
        ("-PC", "--profile-compare"),
        "与历史启动剖析日志对比（需 --profile）：不带值输出历次趋势表"
        "（近 15 次历史+本次，统计基准为环境一致历史的中位数，抗单次抖动），"
        "last=与最近一次对比，正整数=近 N 次趋势，也可指定基准 JSON 文件路径。"
        "差异表格标红回归/标绿改善",
        nargs="?",
        const="trend",
        default=None,
        metavar="REF",
    ),
)


# ---------- clean 子命令参数规格 ----------
_CLEAN_OPTS: tuple[_Opt, ...] = (_Opt(("project",), "项目目录", default=".", nargs="?"),)


# ---------- package 子命令参数规格 ----------
_PACKAGE_OPTS: tuple[_Opt, ...] = (
    _Opt(("project",), "项目目录", default=".", nargs="?"),
    _MIRROR_OPT,
    _PY_VERSION_OPT,
    _TARGET_OPT,
    _Opt(
        ("--no-build",),
        "不自动构建，dist 缺失时报错（默认 dist 存在则复用，避免 fsp b 后 fsp p 重复构建）",
        action="store_true",
    ),
    _Opt(
        ("--format",),
        "发行包格式：auto=平台默认（Win=nsis，Linux=tar.gz+deb，macOS=pkg+dmg），"
        "zip=跨平台便携包，nsis=Windows 安装包，tar.gz/deb=Linux，"
        "pkg/dmg=macOS，all=平台全部",
        default="auto",
        choices=("auto", "zip", "nsis", "tar.gz", "deb", "pkg", "dmg", "all"),
    ),
    _Opt(
        ("--codesign",),
        "macOS 产物做 ad-hoc 签名（codesign --sign -），仅对 pkg/dmg 格式生效。"
        "ad-hoc 签名仅用于本地执行，真实分发需用 Apple Developer ID 签名；默认关闭",
        action="store_true",
    ),
    _Opt(
        ("--sign-exe",),
        "Windows 产物做代码签名（signtool sign /f <pfx> /p <password>），"
        "需配合 --sign-exe-certificate 指定 PFX 证书文件。"
        "签名 dist 内 exe 与 release 目录的 NSIS 安装包；默认关闭。"
        "签名需 Windows SDK 自带 signtool.exe，离线环境可用",
        action="store_true",
    ),
    _Opt(
        ("--sign-exe-certificate",),
        "Windows 代码签名 PFX 证书文件路径（与 --sign-exe 配套）。"
        "与 [tool.fspack] sign-exe-certificate 配置默认合并（CLI 优先）",
        default=None,
        metavar="PFX_PATH",
        dest="sign_exe_certificate",
    ),
    _Opt(
        ("--sign-exe-password",),
        "Windows 代码签名 PFX 证书密码（与 --sign-exe-certificate 配套）",
        default=None,
        metavar="PASSWORD",
        dest="sign_exe_password",
    ),
    _Opt(
        ("--sign-deb",),
        "Linux .deb 安装包做 GPG 分离签名（gpg --detach-sign --armor）。"
        "需配合 --sign-deb-key 指定 GPG 密钥 ID（默认用 GPG 默认密钥）。"
        "签名产物为 <deb>.asc；默认关闭",
        action="store_true",
    ),
    _Opt(
        ("--sign-deb-key",),
        "Linux .deb GPG 签名密钥 ID（如 0x12345678 或 user@example.com）。"
        "未指定时用 GPG 默认密钥；与 [tool.fspack] sign-deb-key 配置默认合并（CLI 优先）",
        default=None,
        metavar="KEY_ID",
        dest="sign_deb_key",
    ),
    _Opt(
        ("-R", "--recursive"),
        "递归扫描 project 目录下所有含 pyproject.toml 的子项目，依次打包。"
        "跳过 .venv/dist/build/.git 等开发期目录；单项目失败不中断，"
        "最后汇总成功/失败列表",
        action="store_true",
    ),
    _Opt(
        ("--extra",),
        "启用的 [project.optional-dependencies] 分组（可多次指定，如 --extra gui --extra web）。"
        "等价 pip install pkg[extra] 语义；仅在需要重新构建时生效（dist 不存在时），"
        "dist 已就绪时复用构建结果。指定时完全覆盖 [tool.fspack] extras 配置默认",
        action="append",
        default=[],
        metavar="NAME",
        dest="extras",
    ),
)


# ---------- init 子命令参数规格 ----------
_INIT_OPTS: tuple[_Opt, ...] = (
    _Opt(("project_name",), "项目名（默认当前目录名）", nargs="?"),
    _Opt(
        ("--template",),
        "模板 id（未指定且 stdin 是 TTY 时弹出交互式选择；非 TTY 用 helloworld）",
        default=None,
    ),
    _Opt(("--list",), "列出所有可用模板后退出", action="store_true"),
    _Opt(
        ("--directory",),
        "项目父目录（默认当前目录），项目创建在 <directory>/<project_name>",
        default=None,
    ),
    _Opt(("--description",), "项目描述（写入 pyproject.toml 的 description 字段）", default=""),
    _Opt(
        ("--python-version",),
        "指定目标 Python 版本（如 3.8、3.10），覆盖模板默认 requires-python 下限",
        default=None,
        metavar="X.Y",
    ),
)


# ---------- doctor 子命令参数规格 ----------
_DOCTOR_OPTS: tuple[_Opt, ...] = (
    _Opt(("--test",), "运行 assets/templates/ 下所有项目模板构建，打印汇总结果", action="store_true"),
    _Opt(
        ("--bench",),
        "运行所有模板构建并收集性能数据，输出性能分析报告（基准评估）",
        action="store_true",
    ),
    _Opt(
        ("--check-cache",),
        "扫描 wheel 缓存目录的依赖解析缓存文件，删除损坏文件，报告 stale/orphan",
        action="store_true",
    ),
)


def _add_build_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 build/b 子命令：打包项目."""
    p = sub.add_parser("build", aliases=["b"], help="打包项目")
    _add_options(p, _BUILD_OPTS)


def _add_run_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 run/r 子命令：运行已打包项目."""
    p = sub.add_parser("run", aliases=["r"], help="运行已打包项目")
    _add_options(p, _RUN_OPTS)


def _add_clean_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 clean/c 子命令：清理 dist/."""
    p = sub.add_parser("clean", aliases=["c"], help="清理 dist/")
    _add_options(p, _CLEAN_OPTS)


def _add_package_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 package/p 子命令：生成发行包."""
    p = sub.add_parser("package", aliases=["p"], help="生成发行包")
    _add_options(p, _PACKAGE_OPTS)


def _add_init_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 init/i 子命令：从模板创建新项目."""
    p = sub.add_parser("init", aliases=["i"], help="从模板创建新项目")
    _add_options(p, _INIT_OPTS)


def _add_manifest_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 manifest/m 子命令：生成产物清单与差异对比.

    含两个子动作：

    - ``generate``：读取项目 dist 目录生成 manifest JSON（CLI 显式重新生成，
      跳过构建阶段）
    - ``diff``：对比两份 manifest JSON 的差异（新增/删除/修改 + 分类汇总）
    """
    g = sub.add_parser("manifest", aliases=["m"], help="产物清单生成与差异对比")
    sub2 = g.add_subparsers(dest="manifest_action", metavar="<action>")

    p_gen = sub2.add_parser("generate", aliases=["g"], help="扫描 dist 目录生成 manifest JSON（显式重新生成）")
    _add_options(
        p_gen,
        (
            _Opt(("project",), "项目目录", default=".", nargs="?"),
            _Opt(("--py-version",), "embed python 版本（用于构建 ProjectInfo，不传则从项目解析）", default=None),
            _Opt(
                ("-o", "--output"),
                "输出 manifest 路径，默认写入 dist/release/<name>-<version>-manifest.json",
                default=None,
                metavar="OUTPUT",
            ),
        ),
    )

    p_diff = sub2.add_parser("diff", aliases=["d"], help="对比两份 manifest JSON 的差异（新增/删除/修改 + 分类汇总）")
    _add_options(
        p_diff,
        (
            _Opt(("old",), "旧 manifest JSON 路径"),
            _Opt(("new",), "新 manifest JSON 路径"),
            _Opt(
                ("--exit-code",),
                "有差异时以退出码 1 退出（便于 CI 判断变更）；默认仅打印差异不改变退出码",
                action="store_true",
                dest="exit_code",
            ),
        ),
    )


def _add_doctor_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 doctor 子命令：环境诊断 + 模板构建测试 + 性能基准 + 缓存完整性检查.

    无参数时仅执行环境诊断（检查工具可用性与配置）。``--test`` 运行
    ``assets/templates/`` 下所有项目模板的构建，打印汇总结果。``--bench``
    在 ``--test`` 基础上收集性能数据（各阶段耗时、下载量、缓存命中），
    输出性能分析报告，作为后续优化的基准。``--check-cache`` 扫描 wheel
    缓存目录的依赖解析缓存文件，删除损坏文件。
    """
    p = sub.add_parser("doctor", aliases=["d"], help="环境诊断：检查打包工具可用性与配置")
    _add_options(p, _DOCTOR_OPTS)


def _add_cache_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 cache 子命令：缓存健康检查与清理.

    ``fsp cache status`` 扫描 ``~/.fspack/cache`` 下各子目录的健康状态，
    报告：

    - 损坏文件（zip/tar 结构非法、PE 头缺失、空文件，扫描时已自动删除）
    - 过期文件（版本不在 KNOWN_*_VERSIONS 中的旧 zip/tar/子目录，需 ``--stale`` 清理）
    - wheels 专用：stale deps（引用缺失 wheel 的 deps 文件）与孤儿 wheel

    ``fsp cache clean`` 删除损坏文件与 wheels 的 stale/orphan，``--dry-run`` 仅预览，
    ``--stale`` 额外清理非 wheels 类型的过期文件，``--target <name>`` 限定单类型。
    """
    p = sub.add_parser("cache", help="缓存健康检查与清理")
    cache_sub = p.add_subparsers(dest="cache_action", metavar="<action>", required=True)

    # 公共选项：--target 限定单 cache 类型，status/clean 都支持
    target_opt = _Opt(
        ("--target",),
        "指定单 cache 类型（wheels/embed/standalone/nuitka/loaders/ccache/tkinter）；未指定时扫描全部类型",
        default=None,
        choices=("wheels", "embed", "standalone", "nuitka", "loaders", "ccache", "tkinter"),
    )

    status_p = cache_sub.add_parser("status", help="扫描缓存目录健康状态（损坏/过期/孤儿）")
    _add_options(
        status_p,
        (
            target_opt,
            _Opt(
                ("--verify",),
                "全量校验 zip 归档完整性（embed/tkinter 逐文件 CRC 校验，慢但可发现数据区损坏；默认仅快检中心目录）",
                action="store_true",
            ),
        ),
    )

    clean_p = cache_sub.add_parser("clean", help="清理损坏文件与孤儿产物")
    _add_options(
        clean_p,
        (
            target_opt,
            _Opt(("--dry-run",), "仅预览将删除的文件，不实际删除", action="store_true"),
            _Opt(
                ("--stale",),
                "额外清理过期文件（embed/standalone/nuitka/tkinter 的旧版本 zip/tar，"
                "ccache 旧版子目录）；wheels 的 stale_deps/orphan_wheels 始终清理",
                action="store_true",
            ),
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """构建参数解析器."""
    parser = argparse.ArgumentParser(
        prog="fspack",
        description="极速 Python 打包器（cargo 风格短命令）。",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示 DEBUG 级别日志")

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    _add_build_subparser(sub)
    _add_run_subparser(sub)
    _add_clean_subparser(sub)
    _add_package_subparser(sub)
    _add_init_subparser(sub)
    _add_manifest_subparser(sub)
    _add_doctor_subparser(sub)
    _add_cache_subparser(sub)
    return parser
