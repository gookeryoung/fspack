"""python3X._pth 文件生成：运行时 sys.path 控制.

拆自 :mod:`fspack.packaging.runtime` facade（原模块级函数），经 facade
re-export 保持 ``from fspack.packaging.runtime import write_pth`` 兼容。
"""

from __future__ import annotations

from pathlib import Path

from fspack.packaging.runtime.urls import embed_dirname

__all__ = ["write_pth"]


def write_pth(
    dist_dir: Path,
    version: str,
    extra_paths: tuple[str, ...] = (),
    *,
    enable_site: bool = True,
) -> Path:
    """在 runtime 目录生成 python3X._pth，控制 sys.path.

    _pth 必须与 python311.dll 同目录（dist/runtime/），路径相对 runtime 解析：
    python311.zip 标准库、..\\site-packages 第三方依赖（与 runtime 平级的
    dist/site-packages）、..\\src 用户源码。

    ``enable_site=False`` 时省略 ``import site`` 行，启动时跳过 ``site.py``
    执行（约节省 20-30ms）。wrapper 已显式 ``sys.path.insert`` site-packages，
    故禁用 site.py 不影响第三方依赖发现，但会丢失 ``user site`` 与
    ``.pth`` 文件处理——纯运行时场景无需这些功能。

    参考 rimsort 与 CPython 文档：``site.py`` 主要负责 site-packages 添加、
    ``.pth`` 文件扫描与 ``ENABLE_USER_SITE`` 处理，运行时无需重复执行。
    """
    pyxy = embed_dirname(version)
    runtime_dir = dist_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    pth = runtime_dir / f"{pyxy}._pth"
    lines = [
        f"{pyxy}.zip",
        ".",
        "..\\site-packages",
        "..\\src",
        *extra_paths,
    ]
    if enable_site:
        lines.append("import site")
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pth
