"""package 子命令参数声明."""

from __future__ import annotations

import argparse


def _add_package_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 package/p 子命令：生成发行包."""
    p = sub.add_parser("package", aliases=["p"], help="生成发行包")
    p.add_argument("project", nargs="?", default=".", help="项目目录")
    p.add_argument("--mirror", default=None, metavar="MIRROR", help="镜像源（huawei/aliyun/tsinghua，默认 aliyun）")
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


__all__ = ["_add_package_subparser"]
