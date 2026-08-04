"""fspack CLI 参数解析器构建.

从 :mod:`fspack.cli` 拆分而来：parser 构建代码（argparse 声明）与命令分发
逻辑分离，``cli.py`` 聚焦 ``main``/dispatch。顶部仅导入轻量标准库与
``__version__``；``--mirror`` 刻意不做 argparse choices 校验（choices 会在
parser 构建期触发 ``fspack.config`` 导入 ~20ms），改由
:func:`fspack.cli._resolve_mirror` 在执行期校验。
"""

from __future__ import annotations

import argparse

from fspack import __version__

__all__ = ["build_parser"]


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
    _add_doctor_subparser(sub)
    _add_cache_subparser(sub)
    return parser


def _add_build_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 build/b 子命令：打包项目."""
    p = sub.add_parser("build", aliases=["b"], help="打包项目")
    p.add_argument("project", nargs="?", default=".", help="项目目录（默认当前目录）")
    # choices 刻意不写：避免 build_parser() 构建期导入 fspack.config（~20ms）；
    # 合法性由 _resolve_mirror 在执行期校验（退出码与 argparse 一致为 2）。
    # 镜像键列表与 config.models.MIRRORS 同步维护（有测试守护）。
    p.add_argument("--mirror", default=None, metavar="MIRROR", help="镜像源（huawei/aliyun/tsinghua，默认 tsinghua）")
    p.add_argument("--py-version", default=None, help="embed python 版本，如 3.11.9")
    p.add_argument("--target", default=None, choices=["windows", "linux", "macos"], help="目标平台（默认当前平台）")
    p.add_argument(
        "--keep-module",
        action="append",
        default=[],
        dest="keep_modules",
        help="显式保留子模块（如 PySide2.QtGui），可重复指定",
    )
    p.add_argument(
        "--icon",
        default=None,
        help=(
            "exe 图标文件路径（.ico/.png/.jpg 等），覆盖 [tool.fspack] icon；"
            "未指定时按 [tool.fspack] icon > 自动搜索 favicon.* > 默认 app.ico 解析"
        ),
    )
    p.add_argument(
        "--no-stdlib-trim",
        action="store_true",
        help="关闭标准库精简（默认剥离 Linux standalone 的 test/ensurepip/idlelib 等无用模块）",
    )
    p.add_argument(
        "--no-slim-runtime",
        action="store_true",
        help=(
            "关闭 standalone runtime 精简（默认 strip libpython 调试符号省 ~34MB + "
            "删 python3.X 二进制省 ~53MB + 删 include/share 省 ~9MB + 非 tkinter 项目剥离 Tcl/Tk 省 ~9MB）。"
            "调试 Python 解释器本身或需要保留开发期文件时使用"
        ),
    )
    p.add_argument(
        "--no-pyc",
        action="store_true",
        help="关闭字节码预编译（默认预编译 src+site-packages 为 .pyc 加速首次启动）",
    )
    p.add_argument(
        "--pyc-strip",
        action="store_true",
        help="剥离非 __init__.py 的 .py 源码（仅保留 .pyc，需配合预编译；保留包标识避免命名空间包问题）",
    )
    p.add_argument(
        "--pyc-optimize",
        type=int,
        default=None,
        choices=[0, 1, 2],
        help=(
            "字节码优化级别：0=保留 docstring/assert，1=剥离 assert，"
            "2=剥离 assert+docstring（-OO，体积减 5-15%%，启动提速 5-10%%，默认 2）"
        ),
    )
    p.add_argument(
        "--no-site",
        action="store_true",
        help="禁用 site.py 加载（_pth 省略 import site 行，节省 ~20-30ms 启动时间）",
    )
    p.add_argument(
        "--nuitka",
        action="store_true",
        help=(
            "启用 Nuitka 编译模式：用户源码编译为 .pyd 本机执行（速度提升 30-50%%）。"
            "Nuitka 自动装到本地缓存 ~/.fspack/cache/nuitka/，不污染 dist/runtime；交叉构建自动跳过；默认关闭"
        ),
    )
    p.add_argument(
        "--ccache",
        action="store_true",
        help=(
            "Nuitka 编译启用 ccache 缓存：首次下载 ccache 到 ~/.fspack/cache/ccache/，"
            "后续构建缓存 gcc 编译结果加速重复编译。需配合 --nuitka 使用；默认关闭"
        ),
    )
    p.add_argument(
        "--nuitka-pkg",
        action="append",
        default=None,
        metavar="PACKAGE",
        dest="nuitka_pkg",
        help=(
            "指定第三方依赖包名用 Nuitka 编译为 .pyd（可多次指定）。"
            "需配合 --nuitka 使用；编译 site-packages/<package>/ 下 .py 为 .pyd，"
            "编译成功删除 .py，失败保留回退 .pyc。风险由用户承担（动态导入/元编程可能不兼容）"
        ),
    )
    p.add_argument(
        "--extra-index-url",
        action="append",
        default=None,
        metavar="URL",
        dest="extra_index_urls",
        help=(
            "额外 PyPI 索引 URL（私有 PyPI 服务器，可多次指定），透传给 pip/uv 的 --extra-index-url。"
            "与 [tool.fspack] extra-index-urls 合并（CLI 追加在配置之后，去重保留首次出现）"
        ),
    )
    p.add_argument(
        "--find-links",
        action="append",
        default=None,
        metavar="PATH_OR_URL",
        dest="find_links",
        help=(
            "本地 wheel 目录或远程 wheel 索引页（可多次指定），透传给 pip/uv 的 --find-links。"
            "与 [tool.fspack] find-links 合并（CLI 追加在配置之后，去重保留首次出现）"
        ),
    )
    p.add_argument(
        "-R",
        "--recursive",
        action="store_true",
        help=(
            "递归扫描 project 目录下所有含 pyproject.toml 的子项目，依次构建。"
            "跳过 .venv/dist/build/.git 等开发期目录；单项目失败不中断，"
            "最后汇总成功/失败列表"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览打包计划，不执行实际构建（不下载/不编译/不复制文件）",
    )
    p.add_argument(
        "--no-size-report",
        action="store_true",
        help="关闭构建结束后的体积报告（默认输出 runtime/src/site-packages 分类与 Top 10 包占比）",
    )
    p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help=(
            "将构建日志写入文件（含时间戳、级别、logger 名、消息、异常栈），便于 CI 上传与问题排查。"
            "文件以追加模式写入，UTF-8 编码；构建开始时创建、结束时自动关闭"
        ),
    )
    p.add_argument(
        "--log-format",
        default="text",
        choices=["text", "json"],
        help="日志文件格式：text=人类可读纯文本（默认），json=结构化 JSON（便于 ELK/Loki 采集）",
    )
    p.add_argument(
        "--profile",
        action="store_true",
        help=(
            "启用耗时分析报告：构建结束后输出各阶段 wall time/占比/缓存命中/下载/节省，"
            "以及资源总览（wall/CPU/CPU 占比/内存峰值），识别瓶颈阶段。"
            "用 tracemalloc 采集内存峰值（无新依赖）"
        ),
    )
    p.add_argument(
        "--analyze-deps",
        action="store_true",
        help=(
            "启用二进制依赖分析：解析 .dll/.so/.dylib 依赖树，剥离无引用文件"
            "（如 Qt6Core.dll 依赖的 ICU 未用时仍保留）。"
            "Windows 用纯 Python 解析 PE 导入表，Linux 用 objdump，macOS 用 otool；"
            "默认关闭，分析耗时但典型项目体积减少 5-15%%"
        ),
    )
    p.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="NAME",
        dest="extras",
        help=(
            "启用的 [project.optional-dependencies] 分组（可多次指定，如 --extra gui --extra web）。"
            "等价 pip install pkg[extra] 语义：分组的依赖合并到下载集合；"
            "自引用 my-pkg[extra] 递归展开，第三方 pkg[extra] 原样透传 pip。"
            "指定时完全覆盖 [tool.fspack] extras 配置默认（集合语义，非合并）"
        ),
    )
    p.add_argument(
        "--lazy-import",
        default=None,
        metavar="MODULES",
        dest="lazy_imports",
        help=(
            "延迟导入的顶层模块名（逗号分隔，如 --lazy-import numpy,pandas）。"
            "wrapper 注入 _LazyImportFinder meta path finder，首次 import 时不执行"
            "模块 __init__.py，首次属性访问时才加载，降低启动时间。"
            "典型收益：numpy 省 ~80ms，pandas 省 ~150ms；"
            "C 扩展模块（.pyd/.so）无法延迟，仍即时加载。"
            "指定时完全覆盖 [tool.fspack] lazy-imports 配置默认"
        ),
    )
    p.add_argument(
        "--require-hashes",
        action="store_true",
        help=(
            "依赖下载强制哈希校验：透传 pip download --require-hashes，"
            "在线模式下要求所有 wheel 的 sha256 与 PyPI 声明一致。"
            "缓存命中时跳过校验（缓存目录 wheel 已首次校验）。"
            "启用时若依赖未声明哈希（如 sdist 回退构建的 wheel）会失败"
        ),
    )
    p.add_argument(
        "--no-sbom",
        action="store_true",
        help=(
            "关闭构建结束后的 SBOM 生成（默认输出 SPDX 2.3 兼容 JSON "
            "到 dist/release/<name>-<version>-sbom.json，含依赖名称/版本/许可证/SHA256）"
        ),
    )


def _add_run_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 run/r 子命令：运行已打包项目."""
    p = sub.add_parser("run", aliases=["r"], help="运行已打包项目")
    p.add_argument("project", nargs="?", default=".", help="项目目录")
    p.add_argument("rest", nargs="*", default=[], help="透传给目标程序的参数（以 -- 分隔）")
    p.add_argument("--debug", action="store_true", help="用 embed python 直跑入口脚本（绕过 GUI loader，输出可见）")
    p.add_argument(
        "--entry",
        default=None,
        help="多入口项目指定要运行的入口名（与 [tool.fspack.entries] 键匹配）",
    )


def _add_clean_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 clean/c 子命令：清理 dist/."""
    p = sub.add_parser("clean", aliases=["c"], help="清理 dist/")
    p.add_argument("project", nargs="?", default=".", help="项目目录")


def _add_package_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 package/p 子命令：生成发行包."""
    p = sub.add_parser("package", aliases=["p"], help="生成发行包")
    p.add_argument("project", nargs="?", default=".", help="项目目录")
    p.add_argument("--mirror", default=None, metavar="MIRROR", help="镜像源（huawei/aliyun/tsinghua，默认 tsinghua）")
    p.add_argument("--py-version", default=None, help="embed python 版本，如 3.11.9")
    p.add_argument("--target", default=None, choices=["windows", "linux", "macos"], help="目标平台（默认当前平台）")
    p.add_argument(
        "--no-build",
        action="store_true",
        help="不自动构建，dist 缺失时报错（默认 dist 存在则复用，避免 fsp b 后 fsp p 重复构建）",
    )
    p.add_argument(
        "--format",
        default="auto",
        choices=["auto", "zip", "nsis", "tar.gz", "deb", "pkg", "dmg", "all"],
        help=(
            "发行包格式：auto=平台默认（Win=nsis，Linux=tar.gz+deb，macOS=pkg+dmg），"
            "zip=跨平台便携包，nsis=Windows 安装包，tar.gz/deb=Linux，"
            "pkg/dmg=macOS，all=平台全部"
        ),
    )
    p.add_argument(
        "--codesign",
        action="store_true",
        help=(
            "macOS 产物做 ad-hoc 签名（codesign --sign -），仅对 pkg/dmg 格式生效。"
            "ad-hoc 签名仅用于本地执行，真实分发需用 Apple Developer ID 签名；默认关闭"
        ),
    )
    p.add_argument(
        "--sign-exe",
        action="store_true",
        help=(
            "Windows 产物做代码签名（signtool sign /f <pfx> /p <password>），"
            "需配合 --sign-exe-certificate 指定 PFX 证书文件。"
            "签名 dist 内 exe 与 release 目录的 NSIS 安装包；默认关闭。"
            "签名需 Windows SDK 自带 signtool.exe，离线环境可用"
        ),
    )
    p.add_argument(
        "--sign-exe-certificate",
        default=None,
        metavar="PFX_PATH",
        dest="sign_exe_certificate",
        help=(
            "Windows 代码签名 PFX 证书文件路径（与 --sign-exe 配套）。"
            "与 [tool.fspack] sign-exe-certificate 配置默认合并（CLI 优先）"
        ),
    )
    p.add_argument(
        "--sign-exe-password",
        default=None,
        metavar="PASSWORD",
        dest="sign_exe_password",
        help="Windows 代码签名 PFX 证书密码（与 --sign-exe-certificate 配套）",
    )
    p.add_argument(
        "--sign-deb",
        action="store_true",
        help=(
            "Linux .deb 安装包做 GPG 分离签名（gpg --detach-sign --armor）。"
            "需配合 --sign-deb-key 指定 GPG 密钥 ID（默认用 GPG 默认密钥）。"
            "签名产物为 <deb>.asc；默认关闭"
        ),
    )
    p.add_argument(
        "--sign-deb-key",
        default=None,
        metavar="KEY_ID",
        dest="sign_deb_key",
        help=(
            "Linux .deb GPG 签名密钥 ID（如 0x12345678 或 user@example.com）。"
            "未指定时用 GPG 默认密钥；与 [tool.fspack] sign-deb-key 配置默认合并（CLI 优先）"
        ),
    )
    p.add_argument(
        "-R",
        "--recursive",
        action="store_true",
        help=(
            "递归扫描 project 目录下所有含 pyproject.toml 的子项目，依次打包。"
            "跳过 .venv/dist/build/.git 等开发期目录；单项目失败不中断，"
            "最后汇总成功/失败列表"
        ),
    )
    p.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="NAME",
        dest="extras",
        help=(
            "启用的 [project.optional-dependencies] 分组（可多次指定，如 --extra gui --extra web）。"
            "等价 pip install pkg[extra] 语义；仅在需要重新构建时生效（dist 不存在时），"
            "dist 已就绪时复用构建结果。指定时完全覆盖 [tool.fspack] extras 配置默认"
        ),
    )


def _add_init_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 init/i 子命令：从模板创建新项目."""
    p = sub.add_parser("init", aliases=["i"], help="从模板创建新项目")
    p.add_argument("project_name", nargs="?", help="项目名（默认当前目录名）")
    p.add_argument(
        "--template",
        default=None,
        help="模板 id（未指定且 stdin 是 TTY 时弹出交互式选择；非 TTY 用 helloworld）",
    )
    p.add_argument("--list", action="store_true", help="列出所有可用模板后退出")
    p.add_argument(
        "--directory",
        default=None,
        help="项目父目录（默认当前目录），项目创建在 <directory>/<project_name>",
    )
    p.add_argument(
        "--description",
        default="",
        help="项目描述（写入 pyproject.toml 的 description 字段）",
    )
    p.add_argument(
        "--python-version",
        default=None,
        metavar="X.Y",
        help="指定目标 Python 版本（如 3.8、3.10），覆盖模板默认 requires-python 下限",
    )


def _add_doctor_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 doctor 子命令：环境诊断 + 模板构建测试 + 性能基准 + 缓存完整性检查.

    无参数时仅执行环境诊断（检查工具可用性与配置）。``--test`` 运行
    ``assets/templates/`` 下所有项目模板的构建，打印汇总结果。``--bench``
    在 ``--test`` 基础上收集性能数据（各阶段耗时、下载量、缓存命中），
    输出性能分析报告，作为后续优化的基准。``--check-cache`` 扫描 wheel
    缓存目录的依赖解析缓存文件，删除损坏文件（iter-128）。
    """
    p = sub.add_parser("doctor", aliases=["d"], help="环境诊断：检查打包工具可用性与配置")
    p.add_argument(
        "--test",
        action="store_true",
        help="运行 assets/templates/ 下所有项目模板构建，打印汇总结果",
    )
    p.add_argument(
        "--bench",
        action="store_true",
        help="运行所有模板构建并收集性能数据，输出性能分析报告（基准评估）",
    )
    p.add_argument(
        "--check-cache",
        action="store_true",
        help="扫描 wheel 缓存目录的依赖解析缓存文件，删除损坏文件，报告 stale/orphan",
    )


def _add_cache_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 cache 子命令：wheel 缓存健康检查与清理（iter-139）.

    ``fsp cache status`` 扫描 ``~/.fspack/cache/wheels`` 下的 ``.deps-*.json``
    依赖解析缓存与 ``*.whl`` wheel 文件，报告：

    - 损坏 deps（JSON 结构非法，扫描时已自动删除）
    - stale deps（引用了缺失 wheel 的 deps 文件，需 ``fsp cache clean`` 清理）
    - 孤儿 wheel（未被任何 deps 引用的 wheel 文件，需 ``fsp cache clean`` 清理）

    ``fsp cache clean`` 删除 stale deps 与孤儿 wheel，``--dry-run`` 仅预览不删除。
    """
    p = sub.add_parser("cache", help="wheel 缓存健康检查与清理")
    cache_sub = p.add_subparsers(dest="cache_action", metavar="<action>", required=True)
    cache_sub.add_parser("status", help="扫描缓存目录健康状态（损坏/stale/orphan）")
    clean_p = cache_sub.add_parser("clean", help="清理 stale deps 与孤儿 wheel 文件")
    clean_p.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览将删除的文件，不实际删除",
    )
