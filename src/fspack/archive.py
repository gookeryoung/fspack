"""归档条目安全校验原语.

``packaging.runtime.extract`` 负责下载产物（python-build-standalone 等）的安全
解压；``slim`` 解包 wheel 时同样需要「拒绝绝对路径 / 路径穿越 / 盘符 / 符号
链接」这层校验。若 ``slim`` 直接从 ``packaging.runtime`` 导入，会让精简层反向
依赖运行时装配层（层级倒挂），因此把这条校验原语下沉到本叶子模块——
只依赖标准库与 :mod:`fspack.exceptions`。
"""

from __future__ import annotations

import stat
import zipfile

from fspack.exceptions import EmbedError

__all__ = ["validate_zip_member"]


def validate_zip_member(info: zipfile.ZipInfo) -> None:
    """校验 zip 条目路径安全：拒绝绝对路径、路径穿越（``..``）、符号链接."""
    name = info.filename.replace("\\", "/")
    if name.startswith("/"):
        raise EmbedError(f"embed zip 含绝对路径条目: {info.filename}")
    if len(name) >= 2 and name[1] == ":":
        raise EmbedError(f"embed zip 含盘符条目: {info.filename}")
    if ".." in name.split("/"):
        raise EmbedError(f"embed zip 含路径穿越条目: {info.filename}")
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        raise EmbedError(f"embed zip 含符号链接条目: {info.filename}")
