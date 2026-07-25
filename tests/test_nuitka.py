"""NuitkaCompiler 单元测试：用户源码编译为本机 .pyd/.so.

nuitka 装到本地缓存 ``~/.fspack/cache/nuitka/<py_version>/site-packages``，
不污染 ``dist/runtime``。编译时用 ``runtime/python.exe -c`` 注入 sys.path
调用 nuitka，绕过 ``python3X._pth`` 对 ``PYTHONPATH`` 的限制。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack.config import (
    DEFAULT_NUITKA_VERSION,
    NUITKA_VERSIONS,
    get_mirror,
    nuitka_version_for,
)
from fspack.exceptions import NuitkaError
from fspack.packaging.nuitka import NuitkaCompiler
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


def test_compile_src_invokes_c_bootstrap_with_sys_path_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile_src 用 `-c "sys.path.insert(0, <cache>); from nuitka.__main__ import main; main()"` 调用 nuitka."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "util.py").write_text("x = 1")
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured: list[list[str]] = []
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kw: captured.append(cmd) or _CompileOK(),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 每个 .py 一次编译调用（无 is_available subprocess 调用，_is_nuitka_cached 是文件系统检查）
    assert len(captured) == 2
    for cmd in captured:
        assert str(runtime / "python.exe") in cmd[0]
        # -c 注入 sys.path
        assert "-c" in cmd
        c_idx = cmd.index("-c")
        bootstrap = cmd[c_idx + 1]
        assert "sys.path.insert" in bootstrap
        assert str(cache) in bootstrap
        assert "from nuitka.__main__ import main" in bootstrap
        # nuitka 编译参数
        assert "--module" in cmd
        assert "--no-pyi-file" in cmd
        assert "--remove-output" in cmd
        assert "--quiet" in cmd


def test_compile_src_deletes_non_init_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """编译后删除非 __init__.py 的 .py，保留 __init__.py 维持包标识."""
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

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # __init__.py 保留
    assert (src / "__init__.py").is_file()
    assert (src / "sub" / "__init__.py").is_file()
    # 非 __init__.py 被删
    assert not (src / "app.py").exists()
    assert not (src / "sub" / "mod.py").exists()


def test_compile_src_failure_warns_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """单文件编译失败仅告警不中断，后续文件继续编译."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.py").write_text("x = 1")
    (src / "bad.py").write_text("invalid syntax !!!")
    cache = _make_nuitka_cache(tmp_path / "cache")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        # cmd 最后一个元素是 py_file 路径
        if "bad.py" in cmd[-1]:
            return _CompileFail()
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # bad.py 编译失败告警
    assert any("Nuitka 编译失败" in r.message and "bad.py" in r.message for r in caplog.records)
    # detail 含失败计数
    assert "失败 1" in st._detail
    assert "编译 1" in st._detail


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
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kw: captured.append(cmd) or _CompileOK(),
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

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 2 个非 __init__.py 被剥离（__init__.py 保留维持包标识）
    assert st._skipped == 2
    # 3 个 .py 编译成功（__init__.py + app.py + util.py）
    assert st._items == 3


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

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

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
    """stamp 未命中时调用 ensure_env + compile_src 并写入 stamp."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
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
    """源码变化使 stamp 失效，重新调用 ensure_env + compile_src."""
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

    calls = {"ensure": 0, "compile": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("ensure", calls["ensure"] + 1) or "4.1.3"),
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

    # stamp 不匹配，调用 ensure_env 与 compile_src
    assert calls["ensure"] == 1
    assert calls["compile"] == 1


def test_stamp_key_includes_nuitka_version_py_version_src_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stamp 键含 nuitka_version + py_version + src_fingerprint."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    key = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    assert "4.1.3" in key
    assert "3.11.9" in key
    # 三段式：version|py_version|src_fp
    assert key.count("|") == 2


def test_stamp_path_under_dist(tmp_path: Path) -> None:
    """stamp 文件位于 dist/.nuitka_compile_stamp."""
    dist = tmp_path / "dist"
    assert NuitkaCompiler._stamp_path(dist) == dist / ".nuitka_compile_stamp"
