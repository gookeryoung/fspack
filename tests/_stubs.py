"""测试共享桩与守卫函数.

集中存放跨多个测试文件重复定义的桩类、守卫函数与测试环境构造辅助，减少冗余：

- :class:`CompletedStub`：``subprocess.run`` 成功返回值桩（替换 5 处 ``_Completed``）
- :class:`FailedStub`：``subprocess.run`` 失败返回值桩（nuitka/pyc 测试族共用）
- :class:`VerifyResultStub`：批量验证成功桩，输出 ``FSPACK_VERIFY_RESULT`` JSON（compile/strip 测试族共用）
- :class:`FakeResp`：``urlopen`` 响应桩，支持分块 ``read(n)``（替换 2 处 ``_FakeResp``）
- :func:`fail_urlopen`：离线模式守卫，断言不应触发网络请求
- :func:`make_nuitka_cache`：构造 nuitka 已安装缓存目录（env/compile 测试族共用）
- :func:`patch_winlibs_hit`：预置 winlibs 缓存命中环境（env/winlibs 测试族共用）
- :func:`setup_embed_mocks`：Windows embed 构建公共 mock（executor/runtime_stage/dist 测试族共用）
- :func:`write_frontend_pkg`：写入前端项目骨架 package.json（sync/frontend 测试族共用）
- :func:`symlink_or_skip`：符号链接守卫，Windows 无权限时跳过（trim/runtime_stage 测试族共用）
- :func:`make_standalone_runtime`：构造最小 standalone runtime 目录树（trim/runtime_stage 测试族共用）
- :func:`fake_compileall_runner`：模拟 compileall 生成真实 .pyc（pyc/executor 测试族共用）

仅提取重复 ≥ 2 处且完全一致的符号；带额外字段的嵌套桩（如 ``test_runner.py``
中带 ``args`` 的 ``_Completed``）保留本地定义。``_make_info``/``_make_tar``/
``_copy_example`` 等仅 1-2 处且签名差异较大，不提取。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.nuitka import NuitkaCompiler

__all__ = [
    "CompletedStub",
    "FailedStub",
    "FakeResp",
    "VerifyResultStub",
    "fail_urlopen",
    "fake_compileall_runner",
    "make_nuitka_cache",
    "make_standalone_runtime",
    "patch_winlibs_hit",
    "setup_embed_mocks",
    "symlink_or_skip",
    "write_frontend_pkg",
]


class CompletedStub:
    """``subprocess.run`` 成功返回值桩.

    提供 ``returncode``/``stdout``/``stderr`` 三属性，用于 mock ``subprocess.run``
    的成功路径。带额外字段或自定义行为的测试应本地定义专属桩。
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeResp:
    """``urlopen`` 响应桩，支持分块 ``read(n)``.

    模拟 ``http.client.HTTPResponse`` 的 ``read(n)`` 行为：``n=-1`` 时返回
    ``block_size`` 字节，``n>0`` 时返回 ``min(n, block_size)`` 字节。配合
    :class:`fspack.packaging.net.Downloader` 的分块下载循环使用。

    上下文管理器协议（``__enter__``/``__exit__``）兼容 ``with urlopen(...) as resp``
    语法，与真实 ``HTTPResponse`` 一致。
    """

    def __init__(self, data: bytes, block_size: int = 64) -> None:
        self._buf = io.BytesIO(data)
        self._block_size = block_size
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._buf.read(self._block_size)
        return self._buf.read(min(n, self._block_size))

    def __enter__(self) -> FakeResp:
        return self

    def __exit__(self, *a: object) -> bool:
        return False


def fail_urlopen(*a: object, **kw: object) -> object:
    """离线模式守卫：被 mock 的 ``urlopen`` 不应被调用.

    用法::

        monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    一旦被调用即抛 :class:`AssertionError`，确保离线模式测试不误触发网络请求。
    """
    raise AssertionError("离线模式不应触发网络请求")


class FailedStub:
    """``subprocess.run`` 失败返回值桩.

    提供 ``returncode=1`` 与样例错误 ``stderr``，用于 mock ``subprocess.run``
    的失败路径（编译报错、pip 安装失败等场景）。
    """

    returncode: int = 1
    stdout: str = ""
    stderr: str = "syntax error in foo.py"


class VerifyResultStub:
    """subprocess.run 桩：模拟批量验证成功（returncode=0，输出 JSON 结果）.

    ``module_status`` 为 {模块名: 是否可加载} 字典，控制每个模块的验证结果；
    输出行带 ``FSPACK_VERIFY_RESULT:`` 前缀，与 nuitka 批量验证协议一致。
    """

    def __init__(self, module_status: dict[str, bool]) -> None:
        import json

        self.returncode = 0
        results_json = json.dumps(module_status)
        self.stdout = f"FSPACK_VERIFY_RESULT:{results_json}\n"
        self.stderr = ""


def make_nuitka_cache(cache_dir: Path) -> Path:
    """在 cache_dir 下创建 nuitka/__init__.py 模拟已装 nuitka，返回 cache_dir."""
    nuitka_pkg = cache_dir / "nuitka"
    nuitka_pkg.mkdir(parents=True, exist_ok=True)
    (nuitka_pkg / "__init__.py").write_text("", encoding="utf-8")
    return cache_dir


def patch_winlibs_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nuitka_ver: str = "4.1.3") -> Path:
    """FSPACK_CACHE_DIR 指向 tmp 并预置 winlibs gcc.exe（模拟缓存命中）.

    返回 winlibs 缓存根目录（``<tmp>/cache/nuitka-winlibs-mingw``）。
    预置的 gcc.exe 路径与 Nuitka ``getCachedDownload`` 约定一致，
    使 :meth:`NuitkaCompiler.ensure_winlibs_mingw` 缓存命中不触发下载。
    同时 mock ``msvc_available`` 为 False：装了 Visual Studio 的机器上
    ensure_env 会跳过 winlibs 预填充（MSVC 优先），mock 保证测试环境无关。
    """
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: False)
    winlibs_root = tmp_path / "cache" / "nuitka-winlibs-mingw"
    gcc_exe = NuitkaCompiler._winlibs_gcc_dir(nuitka_ver) / "mingw64" / "bin" / "gcc.exe"
    gcc_exe.parent.mkdir(parents=True, exist_ok=True)
    gcc_exe.write_bytes(b"")
    return winlibs_root


def setup_embed_mocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, py_version: str) -> None:
    """为 Windows embed 构建注入公共 mock（download/extract/wheels/loader/mingw dll）."""
    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    parts = py_version.split(".", maxsplit=2)
    pyxy = f"python{parts[0]}{parts[1]}"
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / f"{pyxy}.dll").write_bytes(b""),
            (runtime_dir.parent / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", lambda *a, **k: [])
    monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )


def symlink_or_skip(target: str, link: Path) -> None:
    """尝试创建符号链接，Windows 无权限时跳过测试.

    这些测试验证 Linux standalone runtime 的符号链接处理
    （``_trim_standalone_runtime`` 对 Windows 平台直接 return），
    Windows 非管理员环境无法创建符号链接时跳过而非失败。
    启用 Windows 开发者模式或以管理员运行可解除限制。
    """
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"无法创建符号链接（Windows 需开发者模式或管理员权限）: {e}")


def make_standalone_runtime(tmp_path: Path, py_version: str = "3.11.9") -> Path:
    """构造最小 standalone runtime 目录树供 _trim_standalone_runtime 测试."""
    runtime = tmp_path / "runtime"
    major, minor = py_version.split(".")[:2]
    py_tag = f"python{major}.{minor}"

    bin_dir = runtime / "python" / "bin"
    bin_dir.mkdir(parents=True)
    py_bin = bin_dir / py_tag
    py_bin.write_bytes(b"\x7fELF" + b"x" * 1024)
    symlink_or_skip(py_tag, bin_dir / "python3")
    symlink_or_skip(py_tag, bin_dir / "python")
    (bin_dir / f"{py_tag}-config").write_text("#!/bin/sh\n")
    for name in ("2to3", "idle3", "pydoc3", "pip", "pip3"):
        (bin_dir / name).write_text("#!/bin/sh\n")
    (bin_dir / "pip3.11").write_text("#!/bin/sh\n")

    lib_dir = runtime / "python" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / f"libpython{major}.{minor}.so.1.0").write_bytes(b"\x7fELF" + b"y" * 2048)
    symlink_or_skip(f"libpython{major}.{minor}.so.1.0", lib_dir / f"libpython{major}.{minor}.so")
    (lib_dir / "libtcl9.0.so").write_bytes(b"tcl" + b"t" * 512)
    (lib_dir / "libtk9.0.so").write_bytes(b"tk" + b"k" * 512)
    (lib_dir / "tcl9.0").mkdir()
    (lib_dir / "tcl9.0" / "init.tcl").write_text("# tcl")
    (lib_dir / "tk9.0").mkdir()
    (lib_dir / "tk9.0" / "tk.tcl").write_text("# tk")
    (lib_dir / "itcl4.3.5").mkdir()
    (lib_dir / "itcl4.3.5" / "itcl.tcl").write_text("# itcl")
    (lib_dir / "thread3.0.4").mkdir()
    (lib_dir / "thread3.0.4" / "thread.tcl").write_text("# thread")

    stdlib = lib_dir / py_tag
    stdlib.mkdir(parents=True)
    (stdlib / "site-packages").mkdir()

    include_dir = runtime / "python" / "include"
    include_dir.mkdir(parents=True)
    (include_dir / "Python.h").write_text("#define Py_Version")

    share_dir = runtime / "python" / "share"
    share_dir.mkdir(parents=True)
    (share_dir / "man" / "man1").mkdir(parents=True)
    (share_dir / "man" / "man1" / f"{py_tag}.1").write_text(".TH python")

    return runtime


def fake_compileall_runner(cmd: list[str], **kw: Any) -> Any:
    """模拟 subprocess.run 调用 compileall：解析命令并生成真实 .pyc 文件.

    供 ``_precompile_pyc`` 测试使用，使 ``_strip_py_sources`` 能迁移真实的 .pyc。
    用 :func:`py_compile.compile` 生成指定 Python 版本标签的 .pyc 文件名
    （``cpython-{major}{minor}[-opt-N].pyc``），而非当前解释器版本。
    从命令中解析解释器优化标志 ``-O``/``-OO``（等价 ``compileall -o 1/2``）与
    目标目录；py_version 由调用方在 ``cmd`` 中无法获取，故固定用 "3.11"。

    支持多目录合并调用：``python [-O|-OO] -m compileall dir1 dir2 -q -j 0``
    一次编译多目录，本函数收集所有非 flag 的目录参数逐个编译。
    """
    optimize = 0
    target_dirs: list[Path] = []
    for arg in cmd:
        if arg == "-OO":
            optimize = 2
        elif arg == "-O":
            optimize = 1
        elif not arg.startswith("-") and Path(arg).is_dir():
            target_dirs.append(Path(arg))
    for target_dir in target_dirs:
        _compile_dir_with_pyc(target_dir, "3.11", optimize)
    return CompletedStub()


def _compile_dir_with_pyc(target_dir: Path, py_version: str, optimize: int) -> None:
    """用 py_compile 为 target_dir 下所有 .py 生成指定版本标签的 .pyc 文件."""
    import py_compile

    major, minor = py_version.split(".")[:2]
    ver_tag = f"cpython-{major}{minor}"
    opt_suffix = "" if optimize == 0 else f".opt-{optimize}"
    for py in target_dir.rglob("*.py"):
        pycache = py.parent / "__pycache__"
        pycache.mkdir(exist_ok=True)
        pyc_file = pycache / f"{py.stem}.{ver_tag}{opt_suffix}.pyc"
        py_compile.compile(str(py), cfile=str(pyc_file), optimize=optimize)


def write_frontend_pkg(root: Path, *, build: bool = True) -> Path:
    """写入前端项目骨架：package.json（build 脚本按需）."""
    import json

    root.mkdir(parents=True, exist_ok=True)
    scripts = {"build": "vite build"} if build else {"dev": "vite"}
    (root / "package.json").write_text(json.dumps({"name": root.name, "scripts": scripts}), encoding="utf-8")
    return root
