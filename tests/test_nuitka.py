"""NuitkaCompiler 单元测试：用户源码编译为本机 .pyd/.so.

nuitka 装到本地缓存 ``~/.fspack/cache/nuitka/<py_version>/site-packages``，
不污染 ``dist/runtime``。编译时用 ``runtime/python.exe <bootstrap.py>`` 注入
sys.path 调用 nuitka，绕过 ``python3X._pth`` 对 ``PYTHONPATH`` 的限制。
用临时脚本文件而非 ``-c``：Nuitka 的 ``reExecuteNuitka`` 无条件访问
``sys.modules["__main__"].__file__``，``-c`` 模式下该属性不存在会
``AttributeError``。
"""

from __future__ import annotations

import io
import logging
import os
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.config import (
    DEFAULT_NUITKA_VERSION,
    KNOWN_STANDALONE_VERSIONS,
    NUITKA_VERSIONS,
    get_mirror,
    nuitka_version_for,
)
from fspack.exceptions import NuitkaError
from fspack.packaging.nuitka import NuitkaCompiler
from fspack.packaging.runtime import STANDALONE_RELEASE_TAG
from fspack.platform import Platform
from fspack.progress import StageRecorder


class _CompileOK:
    """subprocess.run 成功返回值桩."""

    returncode = 0
    stdout = ""
    stderr = ""


class _CompileFail:
    """subprocess.run 失败返回值桩."""

    returncode = 1
    stdout = ""
    stderr = "syntax error in foo.py"


class _ImportAbsent:
    """subprocess.run 失败返回值桩（模拟 import 失败）."""

    returncode = 1
    stdout = ""
    stderr = "ModuleNotFoundError: No module named 'pip'"


def _make_nuitka_cache(cache_dir: Path) -> Path:
    """在 cache_dir 下创建 nuitka/__init__.py 模拟已装 nuitka，返回 cache_dir."""
    nuitka_pkg = cache_dir / "nuitka"
    nuitka_pkg.mkdir(parents=True, exist_ok=True)
    (nuitka_pkg / "__init__.py").write_text("", encoding="utf-8")
    return cache_dir


# ---- _nuitka_cache_dir 与 _is_nuitka_cached 测试 ----


def test_nuitka_cache_dir_path(tmp_path: Path) -> None:
    """_nuitka_cache_dir 返回 cache_root / py_version / site-packages."""
    cache_root = tmp_path / "nuitka_cache"
    cache_dir = NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9")
    assert cache_dir == cache_root / "3.11.9" / "site-packages"


def test_is_nuitka_cached_true_when_init_exists(tmp_path: Path) -> None:
    """缓存目录有 nuitka/__init__.py 时 _is_nuitka_cached 返回 True."""
    cache_dir = _make_nuitka_cache(tmp_path / "cache")
    assert NuitkaCompiler._is_nuitka_cached(cache_dir) is True


def test_is_nuitka_cached_false_when_missing(tmp_path: Path) -> None:
    """缓存目录无 nuitka 包时 _is_nuitka_cached 返回 False."""
    assert NuitkaCompiler._is_nuitka_cached(tmp_path / "empty") is False


def test_is_nuitka_cached_false_when_init_missing(tmp_path: Path) -> None:
    """缓存目录有 nuitka/ 但无 __init__.py 时返回 False（PEP 420 命名空间包不算）."""
    (tmp_path / "nuitka").mkdir()
    assert NuitkaCompiler._is_nuitka_cached(tmp_path) is False


# ---- _runtime_python 路径解析测试 ----


def test_runtime_python_windows(tmp_path: Path) -> None:
    """Windows 平台 runtime python 路径为 runtime/python.exe."""
    runtime = tmp_path / "runtime"
    py = NuitkaCompiler._runtime_python(runtime, "3.11.9", Platform.WINDOWS)
    assert py == runtime / "python.exe"


def test_runtime_python_linux(tmp_path: Path) -> None:
    """Linux 平台 runtime python 路径为 runtime/python/bin/python{major}.{minor}."""
    runtime = tmp_path / "runtime"
    py = NuitkaCompiler._runtime_python(runtime, "3.11.9", Platform.LINUX)
    assert py == runtime / "python" / "bin" / "python3.11"


# ---- _build_python_cache_dir 与 _build_python_exe 路径解析测试 ----


def test_build_python_cache_dir(tmp_path: Path) -> None:
    """_build_python_cache_dir 返回 cache_root / py_version（按版本隔离避免 ABI 冲突）."""
    cache_root = tmp_path / "python_cache"
    result = NuitkaCompiler._build_python_cache_dir(cache_root, "3.11.15")
    assert result == cache_root / "3.11.15"


def test_build_python_exe_windows(tmp_path: Path) -> None:
    """Windows standalone python 路径为 <dir>/python/python.exe."""
    build_dir = tmp_path / "3.11.15"
    result = NuitkaCompiler._build_python_exe(build_dir, "3.11.15", Platform.WINDOWS)
    assert result == build_dir / "python" / "python.exe"


def test_build_python_exe_linux(tmp_path: Path) -> None:
    """Linux standalone python 路径为 <dir>/python/bin/python{major}.{minor}."""
    build_dir = tmp_path / "3.11.15"
    result = NuitkaCompiler._build_python_exe(build_dir, "3.11.15", Platform.LINUX)
    assert result == build_dir / "python" / "bin" / "python3.11"


# ---- _ensure_build_python standalone python 就绪测试 ----


def _make_standalone_tarball(dest: Path, version: str, tag: str, *, with_python: bool = True) -> None:
    """构造 standalone python tarball，模拟 python-build-standalone 解压结构.

    真实 tarball 结构：``cpython-<ver>+<tag>-x86_64-pc-windows-msvc-install_only/python/python.exe``。
    ``with_python=False`` 时内层无 ``python/`` 目录，用于模拟结构异常场景。
    """
    inner_root = f"cpython-{version}+{tag}-x86_64-pc-windows-msvc-install_only"
    with tarfile.open(dest, "w:gz") as tf:
        if with_python:
            data = b"fake-python-exe"
            info = tarfile.TarInfo(f"{inner_root}/python/python.exe")
        else:
            data = b"readme"
            info = tarfile.TarInfo(f"{inner_root}/README.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))


def test_ensure_build_python_linux_returns_placeholder(tmp_path: Path) -> None:
    """Linux runtime 已是完整 standalone，返回空 Path 占位（compile_src 内部回退 runtime python）."""
    cache_root = tmp_path / "cache"
    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.11.15", Platform.LINUX, stage=st)
    assert result == Path()
    # Linux 分支不触发下载：缓存目录不应被创建
    assert not cache_root.exists()


def test_ensure_build_python_unknown_version_raises(tmp_path: Path) -> None:
    """py_version 的 major.minor 不在 KNOWN_STANDALONE_VERSIONS 时 raise NuitkaError."""
    st = StageRecorder("standalone python")
    with pytest.raises(NuitkaError, match="无对应 python-build-standalone Windows 版本"):
        NuitkaCompiler._ensure_build_python(tmp_path / "cache", "3.15.0", Platform.WINDOWS, stage=st)


def test_ensure_build_python_cache_hit_skips_download(tmp_path: Path) -> None:
    """standalone python.exe 已存在时缓存命中，跳过下载并标注 stage."""
    ver = KNOWN_STANDALONE_VERSIONS["3.11"]
    cache_root = tmp_path / "cache"
    build_dir = NuitkaCompiler._build_python_cache_dir(cache_root, ver)
    py_exe = NuitkaCompiler._build_python_exe(build_dir, ver, Platform.WINDOWS)
    py_exe.parent.mkdir(parents=True)
    py_exe.write_bytes(b"fake-python")

    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.11.9", Platform.WINDOWS, stage=st)

    assert result == py_exe
    assert st._hits == 1
    assert "已就绪" in st._detail


def test_ensure_build_python_download_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载 standalone python 失败（OSError）时包装为 NuitkaError."""

    class _FailDownloader:
        """Downloader 桩：模拟网络失败."""

        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            raise OSError("network unreachable")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _FailDownloader)

    st = StageRecorder("standalone python")
    with pytest.raises(NuitkaError, match="下载 standalone python 失败"):
        NuitkaCompiler._ensure_build_python(tmp_path / "cache", "3.11.9", Platform.WINDOWS, stage=st)


def test_ensure_build_python_download_extract_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载并解压 standalone python 成功：内层 python/ 提升到缓存根，tarball 与解压根被清理."""
    ver = KNOWN_STANDALONE_VERSIONS["3.11"]
    cache_root = tmp_path / "cache"

    class _OKDownloader:
        """Downloader 桩：写入真实 tarball 供解压流程测试."""

        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            _make_standalone_tarball(dest, ver, STANDALONE_RELEASE_TAG)
            return dest.stat().st_size

    monkeypatch.setattr("fspack.packaging.net.Downloader", _OKDownloader)

    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.11.9", Platform.WINDOWS, stage=st)

    expected_exe = cache_root / ver / "python" / "python.exe"
    assert result == expected_exe
    assert expected_exe.is_file()
    # tarball 已删除节省空间
    assert not list((cache_root / ver).glob("*.tar.gz"))
    # 内层解压根（share/doc 等）已清理
    inner_root = cache_root / ver / f"cpython-{ver}+{STANDALONE_RELEASE_TAG}-x86_64-pc-windows-msvc-install_only"
    assert not inner_root.exists()
    assert "安装完成" in st._detail


def test_ensure_build_python_corrupt_tarball_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tarball 损坏（无法解压）时 raise NuitkaError."""

    class _CorruptDownloader:
        """Downloader 桩：写入非 gzip 内容模拟损坏 tarball."""

        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            dest.write_bytes(b"not-a-gzip-file")
            return 16

    monkeypatch.setattr("fspack.packaging.net.Downloader", _CorruptDownloader)

    st = StageRecorder("standalone python")
    with pytest.raises(NuitkaError, match="standalone python tarball 损坏"):
        NuitkaCompiler._ensure_build_python(tmp_path / "cache", "3.11.9", Platform.WINDOWS, stage=st)


def test_ensure_build_python_missing_exe_after_extract_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """解压后未找到 python.exe（tarball 结构异常）时 raise NuitkaError."""
    ver = KNOWN_STANDALONE_VERSIONS["3.11"]

    class _NoPythonDownloader:
        """Downloader 桩：tarball 内层无 python/ 目录."""

        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            _make_standalone_tarball(dest, ver, STANDALONE_RELEASE_TAG, with_python=False)
            return dest.stat().st_size

    monkeypatch.setattr("fspack.packaging.net.Downloader", _NoPythonDownloader)

    st = StageRecorder("standalone python")
    with pytest.raises(NuitkaError, match="standalone python 解压后未找到"):
        NuitkaCompiler._ensure_build_python(tmp_path / "cache", "3.11.9", Platform.WINDOWS, stage=st)


# ---- compile_src 测试 ----


def test_compile_src_skips_when_python_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """runtime python 未就绪时告警并跳过编译."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    cache = _make_nuitka_cache(tmp_path / "cache")
    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert any("runtime python 未就绪" in r.message for r in caplog.records)
    assert "未就绪" in st._detail


def test_compile_src_skips_when_nuitka_not_cached(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """缓存目录无 nuitka 时告警并跳过（回退到 .pyc 模式）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    cache = tmp_path / "empty_cache"  # 无 nuitka 包

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert any("缓存目录无 nuitka" in r.message for r in caplog.records)
    assert "未安装" in st._detail


def test_compile_src_no_py_files(tmp_path: Path) -> None:
    """src 目录无 .py 文件时直接返回，detail 标注无文件."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    cache = _make_nuitka_cache(tmp_path / "cache")

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert "无 .py 文件" in st._detail


def test_compile_src_invokes_bootstrap_script_with_sys_path_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile_src 用临时脚本文件注入 sys.path 调用 nuitka（非 -c，因 reExecute 需要 __file__）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "util.py").write_text("x = 1")
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured: list[list[str]] = []
    script_contents: list[str] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        # 在 finally 清理前捕获脚本内容
        script_path = Path(cmd[1])
        if script_path.is_file():
            script_contents.append(script_path.read_text(encoding="utf-8"))
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 每个 .py 一次编译调用（无 is_available subprocess 调用，_is_nuitka_cached 是文件系统检查）
    assert len(captured) == 2
    bootstrap_scripts: set[str] = set()
    for cmd in captured:
        assert str(runtime / "python.exe") in cmd[0]
        # cmd[1] 是临时 bootstrap 脚本路径（非 -c，因 reExecute 需要 __main__.__file__）
        assert "-c" not in cmd
        bootstrap_script = cmd[1]
        bootstrap_scripts.add(bootstrap_script)
        # 所有调用复用同一 bootstrap 脚本
        assert bootstrap_script.endswith("_nuitka_bootstrap.py")
        # nuitka 编译参数（--show-progress 在 4.x 已 obsolete，不加；不用 --quiet 抑制 INFO）
        assert "--module" in cmd
        assert "--no-pyi-file" in cmd
        assert "--remove-output" in cmd
        assert "--show-progress" not in cmd
        assert "--quiet" not in cmd
        # 不再使用 --python-for-scons：改用 standalone python（完整 CPython）运行 nuitka，
        # scons 自动继承 sys.executable，无需另指定。embed runtime python 不完整会触发
        # Nuitka reExecute fork bomb（详见 compile_with_stamp 文档）。
        assert "--python-for-scons" not in cmd
        # --jobs=N 控制 C 编译并行度（N=os.cpu_count()），单进程内并行无膨胀风险。
        # 必须用 = 形式：Nuitka 4.x 的 argparse 配置要求 --jobs=N 格式，
        # 空格分隔（"--jobs", "N"）会报 "requires an argument with '--jobs='" 错误。
        jobs_args = [arg for arg in cmd if arg.startswith("--jobs=")]
        assert len(jobs_args) == 1, f"应仅一个 --jobs=N 参数，实际 {jobs_args}"
        assert jobs_args[0].split("=", 1)[1].isdigit(), f"--jobs=N 的 N 应为数字，实际 {jobs_args[0]}"
        # 不应出现独立的 --jobs（避免空格分隔形式）
        assert "--jobs" not in [arg for arg in cmd if not arg.startswith("--jobs=")]
    # 复用同一脚本文件
    assert len(bootstrap_scripts) == 1
    # 脚本内容含 sys.path 注入与 nuitka main 调用
    assert len(script_contents) == 2
    for content in script_contents:
        assert "sys.path.insert" in content
        assert str(cache) in content
        assert "from nuitka.__main__ import main" in content


def test_compile_src_skips_init_py_not_compiled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """__init__.py 不编译不删除：跳过编译（无收益），保留 .py 维持包标识."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('hi')")
    (src / "sub").mkdir()
    (src / "sub" / "__init__.py").write_text("")
    (src / "sub" / "mod.py").write_text("x = 1")
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # __init__.py 保留（不编译不删除）
    assert (src / "__init__.py").is_file()
    assert (src / "sub" / "__init__.py").is_file()
    # 非 __init__.py 被删（.pyd 已生成）
    assert not (src / "app.py").exists()
    assert not (src / "sub" / "mod.py").exists()
    # 仅编译非 __init__.py 文件（app.py + sub/mod.py = 2 次，__init__.py 跳过）
    assert len(captured) == 2
    compiled_names = [Path(cmd[-1]).name for cmd in captured]
    assert "__init__.py" not in compiled_names
    assert "app.py" in compiled_names
    assert "mod.py" in compiled_names


def test_compile_src_prefers_standalone_python_over_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_src 收到真实存在的 build_python_exe 时优先用它而非 runtime python.

    验证 standalone python 接入生效：之前 compile_with_stamp 没传 build_python_exe，
    导致 _ensure_build_python 成死代码，编译回退到 embed runtime python 触发
    Nuitka reExecute fork bomb（Windows 反复衍生 python.exe 进程导致 CPU 卡死）。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")  # embed runtime python（不应被使用）
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    cache = _make_nuitka_cache(tmp_path / "cache")

    # standalone python：真实存在的文件，compile_src 据此选用
    standalone_py = tmp_path / "standalone" / "python.exe"
    standalone_py.parent.mkdir(parents=True)
    standalone_py.write_bytes(b"")

    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(
        src,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        cache,
        stage=st,
        build_python_exe=standalone_py,
    )

    # 编译命令首参（python 可执行文件）必须是 standalone python 而非 runtime python
    assert len(captured) == 1
    assert captured[0][0] == str(standalone_py)
    assert str(runtime / "python.exe") not in captured[0][0]


def test_compile_src_failure_warns_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """单文件编译失败仅告警不中断，后续文件继续编译.

    失败的 .py 必须保留（运行时回退到 .pyc 加载），仅删除成功编译的 .py。
    避免编译失败导致 dist/src 无可用代码。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.py").write_text("x = 1")
    (src / "bad.py").write_text("invalid syntax !!!")
    cache = _make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # cmd 最后一个元素是 py_file 路径
        if "bad.py" in cmd[-1]:
            return (1, "", "syntax error")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # bad.py 编译失败告警
    assert any("Nuitka 编译失败" in r.message and "bad.py" in r.message for r in caplog.records)
    # detail 含失败计数
    assert "失败 1" in st._detail
    assert "编译 1" in st._detail
    # 成功编译的 ok.py 被删除（.pyd 已生成替代）
    assert not (src / "ok.py").exists()
    # 失败的 bad.py 必须保留：运行时回退到 .pyc 加载，避免 dist/src 无可用代码
    assert (src / "bad.py").is_file(), "编译失败的 .py 不应被删除，需保留供 .pyc 回退加载"


def test_compile_src_linux_uses_python3_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 平台用 runtime/python/bin/python{ver} 调 nuitka."""
    runtime = tmp_path / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    (runtime / "python" / "bin" / "python3.11").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured: list[list[str]] = []
    monkeypatch.setattr(
        NuitkaCompiler,
        "_stream_compile",
        staticmethod(lambda cmd, **kw: captured.append(cmd) or (0, "", "")),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.LINUX, cache, stage=st)

    assert len(captured) == 1
    assert "python3.11" in captured[0][0]


def test_compile_src_records_stage_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_src 调用 stage.processed 与 stage.skip 记录编译与剥离计数."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('hi')")
    (src / "util.py").write_text("x = 1")
    cache = _make_nuitka_cache(tmp_path / "cache")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 2 个非 __init__.py 被剥离（__init__.py 保留维持包标识）
    assert st._skipped == 2
    # 2 个非 __init__.py 编译成功（app.py + util.py，__init__.py 收集时跳过不编译）
    assert st._items == 2


def test_compile_src_excludes_nuitka_build_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_src 排除 Nuitka 残留的 <name>.build/ 目录下的 .py 文件.

    --remove-output 只在编译成功时清理 .build/，失败时残留。下次构建若不排除会扫到
    scons-debug.py 等产物并尝试编译（无意义且可能失败）。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "snake.py").write_text("print('hi')")
    # 模拟上次失败留下的 Nuitka 构建目录
    build_dir = src / "snake.build"
    build_dir.mkdir()
    (build_dir / "scons-debug.py").write_text("# scons artifact")
    (build_dir / "scons_input.txt").write_text("ignored")
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured_files: list[str] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # cmd 最后一个元素是 py_file 路径
        captured_files.append(cmd[-1])
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 只编译用户的 snake.py，不编译 .build/ 下的 scons-debug.py
    assert len(captured_files) == 1
    assert captured_files[0].endswith("snake.py")
    assert not any("scons-debug" in f for f in captured_files)
    # 编译 1 个，剥离 1 个
    assert st._items == 1
    assert st._skipped == 1


def test_compile_src_skips_entry_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """entry_rels 指定的入口文件不编译不删除，保留 .py 供 runpy.run_path() 调用.

    入口包装器用 runpy.run_path(os.path.join(_SRC_DIR, _ENTRY_REL)) 显式指定 .py 路径，
    若入口 .py 被 Nuitka 编译后删除，run_path 会 FileNotFoundError。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "snake.py").write_text("print('entry')")  # 入口文件
    (src / "game_logic.py").write_text("x = 1")  # 普通模块
    (src / "utils.py").write_text("y = 2")  # 普通模块
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured_files: list[str] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured_files.append(cmd[-1])
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(
        src,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        cache,
        stage=st,
        entry_rels=frozenset({"snake.py"}),
    )

    # 只编译非入口文件：game_logic.py 和 utils.py
    assert len(captured_files) == 2
    compiled_names = {Path(f).name for f in captured_files}
    assert compiled_names == {"game_logic.py", "utils.py"}
    assert "snake.py" not in compiled_names, "入口文件不应被编译"
    # 入口 .py 必须保留（runpy.run_path 调用需要）
    assert (src / "snake.py").is_file(), "入口 .py 必须保留供 run_path 调用"
    # 非入口 .py 被剥离
    assert not (src / "game_logic.py").exists()
    assert not (src / "utils.py").exists()
    # 编译 2 个，剥离 2 个
    assert st._items == 2
    assert st._skipped == 2


def test_compile_src_unlink_failure_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """删除 .py 失败时仅告警不中断（OSError 容错）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    cache = _make_nuitka_cache(tmp_path / "cache")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))

    # 让 Path.unlink 抛 OSError
    def fake_unlink(self: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # unlink 失败告警
    assert any("删除 .py 失败" in r.message for r in caplog.records)
    # stripped 仍为 0（unlink 失败不计入）
    assert st._skipped == 0
    # 编译仍计入
    assert st._items == 1


# ---- nuitka_version_for 字典查询测试 ----


def test_nuitka_version_for_311_returns_413() -> None:
    """Python 3.11.x 锁定 nuitka 4.1.3."""
    assert nuitka_version_for("3.11.9") == "4.1.3"
    assert nuitka_version_for("3.11.15") == "4.1.3"


def test_nuitka_version_for_38_returns_251() -> None:
    """Python 3.8.x 锁定 nuitka 2.5.1（4.x 不再维护 EOL 3.8）."""
    assert nuitka_version_for("3.8.10") == "2.5.1"
    assert nuitka_version_for("3.9.18") == "2.5.1"


def test_nuitka_version_for_unknown_returns_default() -> None:
    """未知 Python 版本（如 3.15）回退 DEFAULT_NUITKA_VERSION."""
    assert nuitka_version_for("3.15.0") == DEFAULT_NUITKA_VERSION


def test_nuitka_version_for_uses_major_minor_only() -> None:
    """版本查询按 major.minor 匹配，补丁版本不影响结果."""
    # 所有 3.10.x 都映射到同一个 nuitka 版本
    ver_a = nuitka_version_for("3.10.0")
    ver_b = nuitka_version_for("3.10.14")
    assert ver_a == ver_b == NUITKA_VERSIONS["3.10"]


# ---- _check_c_compiler C 编译器检查测试 ----


def test_check_c_compiler_windows_no_mingw_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标缺 mingw 交叉编译器时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: False)
    with pytest.raises(NuitkaError, match="mingw-w64"):
        NuitkaCompiler._check_c_compiler(Platform.WINDOWS)


def test_check_c_compiler_windows_with_mingw_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标有 mingw 时不 raise."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    # 不抛异常即通过
    NuitkaCompiler._check_c_compiler(Platform.WINDOWS)


def test_check_c_compiler_linux_no_gcc_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标缺 gcc 时 raise NuitkaError（用户要求显式报错）."""
    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: False)
    with pytest.raises(NuitkaError, match="gcc"):
        NuitkaCompiler._check_c_compiler(Platform.LINUX)


def test_check_c_compiler_linux_with_gcc_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标有 gcc 时不 raise."""
    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    NuitkaCompiler._check_c_compiler(Platform.LINUX)


# ---- ensure_env 环境就绪测试 ----


def test_ensure_env_cache_hit_skips_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存目录已有 nuitka 时跳过 pip install，stage 标注缓存命中."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    cache_root = tmp_path / "nuitka_cache"
    # 预装 nuitka 到缓存
    _make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9"))

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    assert st._hits == 1
    assert "4.1.3" in st._detail


def test_ensure_env_pip_install_target_to_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存未命中时用构建机 pip install --target 装 nuitka 到缓存目录（非 dist/runtime）."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    cache_root = tmp_path / "nuitka_cache"
    expected_cache_dir = NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9")

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    captured_cmd: list[list[str]] = []

    # _ensure_pip_available 检查有 pip → 成功；pip install → 成功
    def stateful_run(cmd: list[str], **kw: Any) -> object:
        captured_cmd.append(cmd)
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    # pip install 成功后需要缓存目录有 nuitka 包，模拟文件系统写入
    def fake_is_cached(cache_dir: Path) -> bool:
        return cache_dir == expected_cache_dir and bool(captured_cmd)  # pip install 调用后返回 True

    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_cached))

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    # 找到 pip install 命令
    pip_cmds = [c for c in captured_cmd if "install" in c and "--target" in c]
    assert len(pip_cmds) == 1, f"应仅一次 pip install，实际 {len(pip_cmds)}"
    cmd = pip_cmds[0]
    # 用构建机 python
    assert cmd[0] == fake_build_python
    assert cmd[1:4] == ["-m", "pip", "install"]
    # --target 指向缓存目录（非 dist/runtime）
    target_idx = cmd.index("--target")
    assert cmd[target_idx + 1] == str(expected_cache_dir)
    assert "--no-compile" in cmd
    assert "--no-cache-dir" in cmd
    assert "-i" in cmd
    assert "nuitka==4.1.3" in cmd
    assert "安装完成" in st._detail


def test_ensure_env_no_pip_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构建机缺 pip 且 ensurepip 与 uv 两轮自救均失败时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # 缓存未命中（_is_nuitka_cached 返回 False）
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(lambda cache_dir: False))

    # 调用顺序：
    # 1. _has_pip (import pip) → 失败（缺 pip）
    # 2. _try_ensurepip (python -m ensurepip) → 失败
    # 3. _try_uv_install_pip (uv pip install pip) → 失败
    # → raise NuitkaError
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        return _ImportAbsent()  # 所有调用均失败

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match="缺 pip 模块且两轮自助安装失败"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_ensurepip_self_heal_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 pip 时 ensurepip 自救成功，继续 pip install nuitka."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # _is_nuitka_cached：首次 False，pip install 后 True
    is_cached_state = {"first": True}

    def fake_is_cached(cache_dir: Path) -> bool:
        if is_cached_state["first"]:
            is_cached_state["first"] = False
            return False
        return True

    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_cached))

    # 调用顺序：
    # 1. _has_pip (import pip) → 失败（缺 pip）
    # 2. _try_ensurepip (python -m ensurepip) → 成功
    # 3. _has_pip (再次检查) → 成功（ensurepip 装好了）
    # 4. pip install --target nuitka → 成功
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        if state["n"] == 1:
            return _ImportAbsent()  # _has_pip 失败（缺 pip）
        return _CompileOK()  # ensurepip + has_pip + pip install

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


def test_ensure_env_uv_self_heal_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensurepip 失败但 uv pip install pip 自救成功."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    is_cached_state = {"first": True}

    def fake_is_cached(cache_dir: Path) -> bool:
        if is_cached_state["first"]:
            is_cached_state["first"] = False
            return False
        return True

    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_cached))

    # 调用顺序（注意短路求值：_try_ensurepip 返回 False 时不调用 _has_pip）：
    # 1. _has_pip (import pip) → 失败（缺 pip）
    # 2. _try_ensurepip → 失败（uv venv 无 ensurepip 模块，短路不调用 _has_pip）
    # 3. _try_uv_install_pip → 成功
    # 4. _has_pip (再次检查) → 成功
    # 5. pip install --target nuitka → 成功
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        if state["n"] == 1:
            return _ImportAbsent()  # _has_pip 失败
        if state["n"] == 2:
            return _CompileFail()  # _try_ensurepip 失败
        return _CompileOK()  # uv pip install pip 与后续

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


def test_has_pip_returns_bool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_has_pip 按 import pip 返回值返回 bool."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")

    # 成功
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())
    assert NuitkaCompiler._has_pip(str(py)) is True

    # 失败
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _ImportAbsent())
    assert NuitkaCompiler._has_pip(str(py)) is False


def test_try_ensurepip_invokes_python_m_ensurepip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_ensurepip 调用 `python -m ensurepip --default-pip`."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")

    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> object:
        captured.append(cmd)
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    assert NuitkaCompiler._try_ensurepip(str(py)) is True
    assert captured[0] == [str(py), "-m", "ensurepip", "--default-pip"]


def test_try_uv_install_pip_invokes_uv_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_uv_install_pip 调用 `uv pip install pip`."""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> object:
        captured.append(cmd)
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    assert NuitkaCompiler._try_uv_install_pip() is True
    assert captured[0] == ["uv", "pip", "install", "pip"]


def test_ensure_env_pip_install_fails_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip install 返回非零退出码时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # 缓存未命中
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(lambda cache_dir: False))

    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        # 第 1 次：_has_pip → 成功
        # 第 2 次：pip install → 失败
        if state["n"] == 2:
            return _CompileFail()
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match=r"pip install nuitka==4\.1\.3 失败"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_install_fails_cache_still_empty_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip install 成功但缓存目录仍无 nuitka 包时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # _is_nuitka_cached 始终返回 False（pip install 成功但缓存仍空）
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(lambda cache_dir: False))

    # _has_pip 成功，pip install 成功
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match="安装后缓存目录仍无 nuitka 包"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


# ---- compile_with_stamp stamp 缓存测试 ----


def test_compile_with_stamp_cache_hit_skips_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 命中时跳过 ensure_env 与 compile_src."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写匹配的 stamp
    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    NuitkaCompiler._stamp_path(dist).write_text(expected_key, encoding="utf-8")

    ensure_called = {"n": 0}
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: ensure_called.__setitem__("n", ensure_called["n"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    assert ensure_called["n"] == 0
    assert compile_called["n"] == 0
    assert st._hits == 1
    assert "stamp 命中" in st._detail


def test_compile_with_stamp_writes_stamp_after_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 未命中时调用 ensure_env + ensure_build_python + compile_src 并写入 stamp."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    # mock standalone python 下载：返回占位路径（compile_src 也被 mock 不会真用到）
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: fake_py),
    )
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: None))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # stamp 文件已写入，内容匹配 _stamp_key
    stamp = NuitkaCompiler._stamp_path(dist)
    assert stamp.is_file()
    nuitka_ver = nuitka_version_for("3.11.9")
    expected = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    assert stamp.read_text(encoding="utf-8") == expected


def test_compile_with_stamp_invalidates_on_src_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """源码变化使 stamp 失效，重新调用 ensure_env + ensure_build_python + compile_src."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写基于旧源码内容的 stamp
    nuitka_ver = nuitka_version_for("3.11.9")
    old_key = f"{nuitka_ver}|3.11.9|old_fingerprint"
    NuitkaCompiler._stamp_path(dist).write_text(old_key, encoding="utf-8")

    calls = {"ensure": 0, "build_python": 0, "compile": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("ensure", calls["ensure"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("build_python", calls["build_python"] + 1) or Path()),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("compile", calls["compile"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # stamp 不匹配，调用 ensure_env、_ensure_build_python 与 compile_src
    assert calls["ensure"] == 1
    assert calls["build_python"] == 1
    assert calls["compile"] == 1


def test_compile_with_stamp_passes_build_python_to_compile_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_with_stamp 将 _ensure_build_python 返回的路径传给 compile_src.

    验证 standalone python 接入闭环：之前该步骤被遗漏导致 _ensure_build_python
    成死代码，编译回退到 embed runtime python 触发 Nuitka reExecute fork bomb
    （Windows 下反复衍生 python.exe 进程导致 CPU 卡死）。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    # standalone python 路径：mock 返回真实存在的文件路径
    fake_py = tmp_path / "fake_standalone_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: fake_py),
    )

    captured: dict[str, object] = {}

    def _capture_compile(cls: Any, *a: Any, **kw: Any) -> None:
        captured["build_python_exe"] = kw.get("build_python_exe")

    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(_capture_compile))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # 关键断言：compile_src 收到的 build_python_exe 正是 _ensure_build_python 的返回值
    assert captured["build_python_exe"] == fake_py


def test_stamp_key_includes_nuitka_version_py_version_src_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stamp 键含 nuitka_version + py_version + src_fingerprint + entry_rels."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    key = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    assert "4.1.3" in key
    assert "3.11.9" in key
    # 五段式：version|py_version|src_fp|entry_part|pkg_part（entry_rels=None 时 entry_part 为空）
    assert key.count("|") == 4
    # 末尾两段为空（entry_rels=None + nuitka_packages=()）
    assert key.endswith("||")


def test_stamp_key_includes_entry_rels(tmp_path: Path) -> None:
    """entry_rels 纳入 stamp key：入口集合变化时 stamp 失效，强制重编.

    避免上次编译删除了 .py、本次新增入口跳过但 .py 已不在导致 run_path 失败。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "snake.py").write_text("print('entry')")
    (src / "util.py").write_text("x = 1")

    key_no_entry = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    key_with_entry = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"snake.py"}))
    key_different_entry = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"util.py"}))

    # entry_rels 不同则 stamp key 不同
    assert key_no_entry != key_with_entry
    assert key_with_entry != key_different_entry
    # entry_rels 出现在 key 中（排序后拼接）
    assert "snake.py" in key_with_entry
    assert "util.py" in key_different_entry


def test_stamp_key_entry_rels_order_independent(tmp_path: Path) -> None:
    """entry_rels 集合迭代顺序不影响 stamp key（排序后拼接）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("")
    key1 = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"snake.py", "util.py"}))
    key2 = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"util.py", "snake.py"}))
    assert key1 == key2


def test_stamp_path_under_dist(tmp_path: Path) -> None:
    """stamp 文件位于 dist/.nuitka_compile_stamp."""
    dist = tmp_path / "dist"
    assert NuitkaCompiler._stamp_path(dist) == dist / ".nuitka_compile_stamp"


def test_compile_with_stamp_read_oserror_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 读取 OSError（如磁盘错误）时容错继续编译流程，不崩溃."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写 stamp 文件使 is_file() 为 True，随后 read_text 抛 OSError
    stamp = NuitkaCompiler._stamp_path(dist)
    stamp.write_text("stale", encoding="utf-8")

    orig_read_text = Path.read_text

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == stamp:
            raise OSError("disk error")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    calls = {"compile": 0}
    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: Path()))
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("compile", calls["compile"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # OSError 被容错：继续执行编译流程
    assert calls["compile"] == 1


def test_compile_with_stamp_write_oserror_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """stamp 写入 OSError（如只读文件系统）时仅告警不中断."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    stamp = NuitkaCompiler._stamp_path(dist)
    orig_write_text = Path.write_text

    def fake_write_text(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if self == stamp:
            raise OSError("read-only file system")
        return orig_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: Path()))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: None))

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        # 不抛异常即通过（写入失败仅告警）
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    assert any("写入 Nuitka stamp 失败" in r.message for r in caplog.records)


# ---- compile_with_stamp 环境就绪失败回退测试 ----


def test_compile_with_stamp_ensure_env_failure_falls_back_to_pyc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ensure_env 抛 NuitkaError（如 pip install 失败、C 编译器缺失）时回退到 .pyc 模式.

    Nuitka 是可选优化，环境就绪失败不应中断构建。回退后不写 stamp（下次构建仍会尝试）。
    """

    def _fail_ensure_env(cls: Any, *a: Any, **kw: Any) -> str:
        raise NuitkaError("pip install nuitka==4.1.3 失败（退出码 1）")

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(_fail_ensure_env))
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        # 不抛异常即通过（回退到 .pyc 模式）
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    assert any("回退到 .pyc 模式" in r.message for r in caplog.records)
    assert "回退到 .pyc 模式" in st._detail
    # 回退后不调用 compile_src
    assert compile_called["n"] == 0
    # 回退后不写 stamp（下次构建仍会尝试，避免永久跳过 Nuitka）
    assert not NuitkaCompiler._stamp_path(dist).is_file()


def test_compile_with_stamp_ensure_build_python_failure_falls_back_to_pyc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_ensure_build_python 抛 NuitkaError（如 standalone python 下载失败）时回退到 .pyc 模式."""

    def _fail_build_python(cls: Any, *a: Any, **kw: Any) -> Path:
        raise NuitkaError("下载 standalone python 失败: network unreachable")

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(_fail_build_python))
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    assert any("回退到 .pyc 模式" in r.message for r in caplog.records)
    assert compile_called["n"] == 0
    assert not NuitkaCompiler._stamp_path(dist).is_file()


def test_compile_with_stamp_compile_src_failure_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile_src 内部单文件编译失败已有 warning 继续，不触发回退机制.

    回退仅捕获环境就绪阶段（ensure_env + _ensure_build_python）的 NuitkaError，
    不捕获 compile_src 的编译失败（那是用户代码问题，非环境问题）。
    此处验证 compile_src 被 mock 为正常返回时，stamp 正常写入（不回退）。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: None))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # compile_src 正常调用 → stamp 写入（非回退路径）
    assert NuitkaCompiler._stamp_path(dist).is_file()
    assert "回退" not in st._detail


# ---- compile_packages 测试 ----


def test_compile_packages_empty_packages_noop(tmp_path: Path) -> None:
    """packages 为空时 compile_packages 直接返回，不调用任何编译逻辑."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = _make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    sp.mkdir()
    st = StageRecorder("Nuitka 包编译")
    # 不应抛异常，不应调用 _resolve_compile_python
    NuitkaCompiler.compile_packages(sp, (), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert st._hits == 0


def test_compile_packages_missing_package_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """指定包在 site-packages 不存在时 warning 并跳过."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = _make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    sp.mkdir()
    # nonexistent_pkg 不存在
    st = StageRecorder("Nuitka 包编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_packages(sp, ("nonexistent_pkg",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert any("未找到包目录" in r.message for r in caplog.records)


def test_compile_packages_compiles_specified_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_packages 编译指定包下的 .py 文件（跳过 __init__.py）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = _make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    pkg = sp / "rich"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "_extension.py").write_text("x = 1")
    (pkg / "console.py").write_text("y = 2")

    captured: list[list[Path]] = []

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], int]:
        captured.append(py_files)
        return (set(py_files), 0)

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))

    st = StageRecorder("Nuitka 包编译")
    NuitkaCompiler.compile_packages(sp, ("rich",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 收集了 _extension.py 与 console.py（跳过 __init__.py）
    assert len(captured) == 1
    names = {p.name for p in captured[0]}
    assert names == {"_extension.py", "console.py"}
    # 编译成功的 .py 被删除
    assert not (pkg / "_extension.py").exists()
    assert not (pkg / "console.py").exists()
    # __init__.py 保留
    assert (pkg / "__init__.py").is_file()


def test_compile_with_stamp_passes_nuitka_packages_to_compile_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile_with_stamp 透传 nuitka_packages 到 compile_packages."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: None))

    # 创建 site-packages 目录使 compile_with_stamp 进入 compile_packages 分支
    (runtime / "Lib" / "site-packages").mkdir(parents=True)

    captured_pkgs: list[tuple[str, ...]] = []

    def fake_compile_packages(cls: Any, *args: Any, **kwargs: Any) -> None:
        captured_pkgs.append(args[1] if len(args) > 1 else kwargs.get("packages", ()))

    monkeypatch.setattr(NuitkaCompiler, "compile_packages", classmethod(fake_compile_packages))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src,
        dist,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        get_mirror("aliyun"),
        cache_root,
        stage=st,
        nuitka_packages=("rich", "click"),
    )

    assert captured_pkgs == [("rich", "click")]
    # stamp 写入（含 pkg_part）
    assert NuitkaCompiler._stamp_path(dist).is_file()


def test_stamp_key_includes_nuitka_packages(tmp_path: Path) -> None:
    """nuitka_packages 纳入 stamp key：包列表变化时 stamp 失效."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x")
    key_empty = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, ())
    key_with_pkgs = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, ("rich", "click"))
    assert key_empty != key_with_pkgs
    assert "rich,click" in key_with_pkgs


# ---- _stream_compile 流式输出测试 ----


def test_stream_compile_captures_stdout_and_stderr(capfd: pytest.CaptureFixture[str]) -> None:
    """_stream_compile 捕获子进程 stdout/stderr 并实时写入终端 fd."""
    cmd = [sys.executable, "-c", "import sys; print('out-msg'); sys.stderr.write('err-msg\\n')"]
    returncode, stdout, stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 0
    assert "out-msg" in stdout
    assert "err-msg" in stderr
    # 验证输出被实时写入终端 fd（capfd 捕获 fd 级输出）
    captured = capfd.readouterr()
    assert "out-msg" in captured.out
    assert "err-msg" in captured.err


def test_stream_compile_captures_delayed_output(capfd: pytest.CaptureFixture[str]) -> None:
    """_stream_compile 能捕获子进程延迟输出（模拟 nuitka 编译耗时的多段输出）."""
    cmd = [
        sys.executable,
        "-c",
        "import sys, time; print('step1'); time.sleep(0.3); print('step2'); sys.stderr.write('warn\\n')",
    ]
    returncode, stdout, stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 0
    assert "step1" in stdout
    assert "step2" in stdout
    assert "warn" in stderr
    captured = capfd.readouterr()
    assert "step1" in captured.out
    assert "step2" in captured.out
    assert "warn" in captured.err


def test_stream_compile_returns_nonzero_on_failure(capfd: pytest.CaptureFixture[str]) -> None:
    """子进程退出码非零时 _stream_compile 正确返回 returncode."""
    cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    returncode, _stdout, _stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 3


def test_stream_compile_captures_multiline_output(capfd: pytest.CaptureFixture[str]) -> None:
    """_stream_compile 能捕获多行输出（模拟 nuitka --show-progress 的多步骤输出）."""
    script = (
        "print('Nuitka:INFO:Started Python compilation'); "
        "print('Nuitka:INFO:Completed Python level compilation'); "
        "print('Nuitka:INFO:Generating C source code'); "
        "print('Nuitka:INFO:Running C compilation')"
    )
    cmd = [sys.executable, "-c", script]
    returncode, stdout, _stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 0
    assert "Started Python compilation" in stdout
    assert "Completed Python level compilation" in stdout
    assert "Generating C source code" in stdout
    assert "Running C compilation" in stdout
    captured = capfd.readouterr()
    assert "Running C compilation" in captured.out


# ---- 心跳线程测试 ----


def test_compile_src_heartbeat_logs_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """compile_src 在编译期间通过心跳线程输出进度日志.

    nuitka 的 reExecute 机制导致子进程输出不可靠（Windows close_fds=True 不继承 PIPE），
    心跳线程是唯一的进度反馈。mock _stream_compile 模拟耗时编译，验证心跳日志输出。
    """
    import time as _time

    from fspack.progress import StageRecorder

    # 缩短心跳间隔到 0.05 秒，避免测试等待 10 秒
    monkeypatch.setattr("fspack.packaging.nuitka._HEARTBEAT_INTERVAL", 0.05)

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "app.py").write_text("print('hello')", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_text("", encoding="utf-8")
    cache = tmp_path / "cache"
    # 创建 nuitka 包假文件，让 _is_nuitka_cached 检查通过
    (cache / "nuitka").mkdir(parents=True)
    (cache / "nuitka" / "__init__.py").write_text("", encoding="utf-8")

    # mock _stream_compile 模拟耗时 0.2 秒的编译（触发至少 1 次心跳）
    def slow_stream(_cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        _time.sleep(0.2)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(slow_stream))

    with caplog.at_level(logging.INFO, logger="fspack.packaging.nuitka"):
        st = StageRecorder("Nuitka 编译")
        NuitkaCompiler.compile_src(src, runtime, "3.10.11", Platform.WINDOWS, cache, stage=st)

    # 验证心跳日志输出（至少 1 次 "Nuitka 编译中... 已耗时"）
    heartbeat_logs = [r for r in caplog.records if "Nuitka 编译中" in r.message]
    assert len(heartbeat_logs) >= 1, f"期望至少 1 次心跳日志，实际 {len(heartbeat_logs)} 次"
    # 验证心跳消息格式
    assert "已耗时" in heartbeat_logs[0].message


def test_compile_src_heartbeat_stops_after_compile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """编译完成后心跳线程立即停止，不输出多余日志."""
    from fspack.progress import StageRecorder

    # 心跳间隔设为较长值，确保编译期间不触发心跳
    monkeypatch.setattr("fspack.packaging.nuitka._HEARTBEAT_INTERVAL", 10.0)

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "app.py").write_text("print('hello')", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_text("", encoding="utf-8")
    cache = tmp_path / "cache"
    # 创建 nuitka 包假文件，让 _is_nuitka_cached 检查通过
    (cache / "nuitka").mkdir(parents=True)
    (cache / "nuitka" / "__init__.py").write_text("", encoding="utf-8")

    # mock _stream_compile 立即返回
    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))

    # 验证不会因为心跳线程阻塞
    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.10.11", Platform.WINDOWS, cache, stage=st)
    # 编译成功，无异常即通过


# ---- ccache 相关测试 ----


def test_resolve_jobs_returns_cpu_count() -> None:
    """``_resolve_jobs`` 返回 CPU 核心数，最低 4."""
    from fspack.packaging.nuitka import NuitkaCompiler

    jobs = NuitkaCompiler._resolve_jobs()
    assert jobs == (os.cpu_count() or 4)


def test_build_ccache_env_returns_none_when_no_ccache() -> None:
    """ccache_exe 为 None 时返回 None（继承环境，不注入 CC）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    assert NuitkaCompiler._build_ccache_env(Platform.LINUX, None) is None
    assert NuitkaCompiler._build_ccache_env(Platform.WINDOWS, None) is None


def test_build_ccache_env_sets_cc_linux(tmp_path: Path) -> None:
    """Linux ccache 环境设置 CC="ccache gcc"."""
    from fspack.packaging.nuitka import NuitkaCompiler

    ccache_exe = tmp_path / "ccache"
    ccache_exe.write_bytes(b"")
    env = NuitkaCompiler._build_ccache_env(Platform.LINUX, ccache_exe)
    assert env is not None
    assert env["CC"] == f"{ccache_exe} gcc"
    assert "CCACHE_DIR" in env


def test_build_ccache_env_sets_cc_windows_mingw(tmp_path: Path) -> None:
    """Windows ccache 环境设置 CC="ccache x86_64-w64-mingw32-gcc"."""
    from fspack.packaging.nuitka import NuitkaCompiler

    ccache_exe = tmp_path / "ccache.exe"
    ccache_exe.write_bytes(b"")
    env = NuitkaCompiler._build_ccache_env(Platform.WINDOWS, ccache_exe)
    assert env is not None
    assert "x86_64-w64-mingw32-gcc" in env["CC"]


def test_ensure_ccache_finds_system_ccache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 中有 ccache 时优先使用系统 ccache."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    fake_ccache = tmp_path / "ccache"
    fake_ccache.write_bytes(b"")
    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: str(fake_ccache))
    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(tmp_path / "nuitka", Platform.LINUX, st)
    assert result == fake_ccache


def test_ensure_ccache_finds_local_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 无 ccache 但本地缓存有时使用本地缓存."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "nuitka"
    ccache_dir = tmp_path / "ccache"
    ccache_exe = ccache_dir / "ccache"
    ccache_exe.parent.mkdir(parents=True)
    ccache_exe.write_bytes(b"")

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)
    st = StageRecorder("Nuitka 编译")
    # cache_root.parent / "ccache" = tmp_path / "ccache"
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.LINUX, st)
    assert result == ccache_exe


def test_ensure_ccache_windows_uses_exe_extension(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标查找 ccache.exe."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "nuitka"
    ccache_dir = tmp_path / "ccache"
    ccache_exe = ccache_dir / "ccache.exe"
    ccache_exe.parent.mkdir(parents=True)
    ccache_exe.write_bytes(b"")

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)
    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.WINDOWS, st)
    assert result == ccache_exe


def test_ensure_ccache_migrates_nested_local_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地缓存根目录无 ccache 但有 ccache-<ver>-<platform>/ 子目录时自动迁移复用.

    用户已下载旧版 ccache（解压后子目录结构未迁移），_ensure_ccache 应识别并
    迁移到根目录，避免重新下载。覆盖 Windows 反复下载的真实场景。
    """
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "nuitka"
    ccache_dir = tmp_path / "ccache"
    nested_dir = ccache_dir / "ccache-4.10.2-windows-x86_64"
    nested_dir.mkdir(parents=True)
    (nested_dir / "ccache.exe").write_bytes(b"fake-exe")
    (nested_dir / "LICENSE.html").write_bytes(b"license")
    # 根目录无 ccache.exe（旧 bug 导致未迁移）

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)
    # 拦截下载，确保不触发
    download_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "_download_and_extract_ccache",
        staticmethod(lambda *a, **kw: download_called.__setitem__("n", download_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.WINDOWS, st)

    # 返回迁移后的根目录 ccache.exe
    assert result == ccache_dir / "ccache.exe"
    assert (ccache_dir / "ccache.exe").is_file()
    # 子目录已清理
    assert not nested_dir.exists()
    assert not list(ccache_dir.glob("ccache-*/"))
    # 未触发下载
    assert download_called["n"] == 0


def test_ensure_ccache_migrates_nested_local_cache_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 本地缓存子目录结构同样支持自动迁移."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "nuitka"
    ccache_dir = tmp_path / "ccache"
    nested_dir = ccache_dir / "ccache-4.10.2-linux-x86_64"
    nested_dir.mkdir(parents=True)
    (nested_dir / "ccache").write_bytes(b"#!/bin/sh\nexit 0\n")

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)
    download_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "_download_and_extract_ccache",
        staticmethod(lambda *a, **kw: download_called.__setitem__("n", download_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.LINUX, st)

    assert result == ccache_dir / "ccache"
    assert (ccache_dir / "ccache").is_file()
    assert not list(ccache_dir.glob("ccache-*/"))
    assert download_called["n"] == 0


def test_ensure_ccache_unsupported_platform_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无预编译二进制的平台（理论上所有平台都有，但 mock URL 缺失时）返回 None."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    # 清空 URL 映射模拟不支持的平台
    monkeypatch.setattr("fspack.packaging.nuitka.CCACHE_URLS", {})
    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)
    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(tmp_path, Platform.LINUX, st)
    assert result is None


def test_ensure_ccache_download_failure_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载失败时回退到 None（warning 不中断）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)

    def _fail_download(url: str, ccache_dir: Path, target: object) -> None:
        raise OSError("network error")

    monkeypatch.setattr(NuitkaCompiler, "_download_and_extract_ccache", staticmethod(_fail_download))
    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(tmp_path, Platform.LINUX, st)
    assert result is None


def test_compile_src_passes_ccache_to_compile_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ccache=True 时 compile_src 调 _ensure_ccache 并传 ccache_exe 到 _compile_files."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("x = 1")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = tmp_path / "nuitka"
    (cache / "nuitka").mkdir(parents=True)
    (cache / "nuitka" / "__init__.py").write_text("")

    captured_ccache: list[Path | None] = []

    def fake_compile_files(cls: Any, *args: Any, **kwargs: Any) -> tuple[set[Path], int]:
        captured_ccache.append(kwargs.get("ccache_exe"))
        return set(), 0

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))
    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))

    fake_ccache = tmp_path / "ccache_bin"
    fake_ccache.write_bytes(b"")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_ccache", classmethod(lambda cls, *a, **kw: fake_ccache))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st, ccache=True, cache_root=cache)
    assert len(captured_ccache) == 1
    assert captured_ccache[0] == fake_ccache


def test_compile_src_ccache_false_skips_ensure_ccache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ccache=False 时不调 _ensure_ccache，_compile_files 收到 ccache_exe=None."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("x = 1")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = tmp_path / "nuitka"
    (cache / "nuitka").mkdir(parents=True)
    (cache / "nuitka" / "__init__.py").write_text("")

    ensure_called: list[bool] = []

    def fake_ensure_ccache(cls: Any, *a: Any, **kw: Any) -> None:
        ensure_called.append(True)

    monkeypatch.setattr(NuitkaCompiler, "_ensure_ccache", classmethod(fake_ensure_ccache))

    captured: list[Path | None] = []

    def fake_compile_files(cls: Any, *args: Any, **kwargs: Any) -> tuple[set[Path], int]:
        captured.append(kwargs.get("ccache_exe"))
        return set(), 0

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))

    st = StageRecorder("Nuitka 编译")
    # 用 Platform.WINDOWS 匹配 runtime/python.exe（Linux 路径为 runtime/python/bin/python3.11）
    NuitkaCompiler.compile_src(
        src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st, ccache=False, cache_root=cache
    )
    assert not ensure_called  # ccache=False 不调 _ensure_ccache
    assert captured[0] is None  # ccache_exe=None


def test_compile_with_stamp_passes_ccache_to_compile_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_with_stamp 透传 ccache=True 到 compile_src."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = tmp_path / "nuitka"

    captured_ccache: list[bool] = []

    def fake_compile_src(cls: Any, *args: Any, **kwargs: Any) -> None:
        captured_ccache.append(kwargs.get("ccache", False))

    def fake_ensure_env(cls: Any, *args: Any, **kwargs: Any) -> str:
        return "4.1.3"

    def fake_is_nuitka_cached(cache_dir: Any) -> bool:
        return True

    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(fake_compile_src))
    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(fake_ensure_env))
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_nuitka_cached))
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: None),
    )
    # 让 stamp 未命中
    monkeypatch.setattr(NuitkaCompiler, "_stamp_path", staticmethod(lambda dist: tmp_path / "nonexistent_stamp"))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, tmp_path, runtime, "3.11.9", Platform.LINUX, get_mirror("huawei"), cache, stage=st, ccache=True
    )
    assert captured_ccache[0] is True


def test_build_options_from_defaults_translates_ccache() -> None:
    """build_options_from_defaults 透传 ccache 字段."""
    from fspack.config import BuildDefaults, build_options_from_defaults

    # ccache=True
    defaults = BuildDefaults(ccache=True)
    opts = build_options_from_defaults(defaults)
    assert opts.ccache is True

    # ccache=False
    defaults = BuildDefaults(ccache=False)
    opts = build_options_from_defaults(defaults)
    assert opts.ccache is False

    # ccache=None → 默认值 False
    defaults = BuildDefaults()
    opts = build_options_from_defaults(defaults)
    assert opts.ccache is False


# ---- _download_and_extract_ccache 与 _ensure_ccache 成功路径测试 ----


def _make_ccache_linux_tarball(dest: Path) -> None:
    """构造 ccache Linux tarball：内含 ccache-<ver>-linux-x86_64/ccache."""
    inner_dir = "ccache-4.10.2-linux-x86_64"
    with tarfile.open(dest, "w:xz") as tf:
        data = b"#!/bin/sh\nexit 0\n"
        info = tarfile.TarInfo(f"{inner_dir}/ccache")
        info.size = len(data)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))


def _make_ccache_windows_zip(dest: Path) -> None:
    """构造 ccache Windows zip：内含 ccache-<ver>-windows-x86_64/ccache.exe.

    真实 ccache releases 的 Windows zip 解压后是 ``ccache-<ver>-windows-x86_64/``
    子目录，内含 ``ccache.exe`` 与 LICENSE/MANUAL 等附属文件。
    """
    inner_dir = "ccache-4.10.2-windows-x86_64"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(f"{inner_dir}/ccache.exe", b"fake-exe")
        zf.writestr(f"{inner_dir}/LICENSE.html", b"license")
        zf.writestr(f"{inner_dir}/README.md", b"readme")


def test_download_and_extract_ccache_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 归档解压后 ccache 从 ccache-<ver>-linux-x86_64/ccache 移到根目录."""
    from fspack.packaging.nuitka import NuitkaCompiler

    ccache_dir = tmp_path / "ccache"

    class _StubDownloader:
        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            _make_ccache_linux_tarball(dest)
            return dest.stat().st_size

    monkeypatch.setattr("fspack.packaging.net.Downloader", _StubDownloader)
    NuitkaCompiler._download_and_extract_ccache("http://fake/ccache.tar.xz", ccache_dir, Platform.LINUX)

    # ccache 已从内层目录移到根
    assert (ccache_dir / "ccache").is_file()
    # 内层目录已清理
    assert not list(ccache_dir.glob("ccache-*/"))
    # tarball 已删除
    assert not (ccache_dir / "ccache.tar.xz").exists()


def test_download_and_extract_ccache_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows zip 解压后 ccache.exe 从 ccache-<ver>-windows-x86_64/ 移到根目录."""
    from fspack.packaging.nuitka import NuitkaCompiler

    ccache_dir = tmp_path / "ccache"

    class _StubDownloader:
        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            _make_ccache_windows_zip(dest)
            return dest.stat().st_size

    monkeypatch.setattr("fspack.packaging.net.Downloader", _StubDownloader)
    NuitkaCompiler._download_and_extract_ccache("http://fake/ccache.zip", ccache_dir, Platform.WINDOWS)

    # ccache.exe 已从内层目录移到根
    assert (ccache_dir / "ccache.exe").is_file()
    # 内层目录（含 LICENSE/README 等）已清理
    assert not list(ccache_dir.glob("ccache-*/"))
    # zip 已删除
    assert not (ccache_dir / "ccache.zip").exists()


def test_ensure_ccache_download_success_returns_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 无 ccache 且本地缓存无时下载预编译二进制成功，返回 ccache 路径并设 stage."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)

    def _fake_download_and_extract(url: str, ccache_dir: Path, target: object) -> None:
        ccache_dir.mkdir(parents=True, exist_ok=True)
        exe_name = "ccache.exe" if target is Platform.WINDOWS else "ccache"
        (ccache_dir / exe_name).write_bytes(b"fake")

    monkeypatch.setattr(NuitkaCompiler, "_download_and_extract_ccache", staticmethod(_fake_download_and_extract))

    st = StageRecorder("Nuitka 编译")
    cache_root = tmp_path / "nuitka"
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.WINDOWS, st)
    assert result is not None
    assert result.name == "ccache.exe"
    assert "已下载" in st._detail


def test_ensure_ccache_download_success_linux_chmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 下载成功后 chmod 0o755（容错 OSError 不中断）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.platform import Platform
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)

    def _fake_download_and_extract(url: str, ccache_dir: Path, target: object) -> None:
        ccache_dir.mkdir(parents=True, exist_ok=True)
        (ccache_dir / "ccache").write_bytes(b"fake")

    monkeypatch.setattr(NuitkaCompiler, "_download_and_extract_ccache", staticmethod(_fake_download_and_extract))

    st = StageRecorder("Nuitka 编译")
    # Linux chmod 在 Windows 上无意义但不抛错（contextlib.suppress 容错）
    result = NuitkaCompiler._ensure_ccache(tmp_path / "nuitka", Platform.LINUX, st)
    assert result is not None
    assert result.name == "ccache"


def test_ensure_ccache_download_succeeds_but_exe_missing_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载"成功"但 ccache_exe 未出现在预期路径时返回 None（warning 不中断）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)

    def _empty_download(url: str, ccache_dir: Path, target: object) -> None:
        ccache_dir.mkdir(parents=True, exist_ok=True)
        # 不写 ccache.exe，模拟归档内容异常

    monkeypatch.setattr(NuitkaCompiler, "_download_and_extract_ccache", staticmethod(_empty_download))

    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(tmp_path / "nuitka", Platform.WINDOWS, st)
    assert result is None
