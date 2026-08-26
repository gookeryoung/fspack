"""``NuitkaEnv`` 环境就绪测试：nuitka 安装、pip 自愈、C 编译器检查与编译环境变量."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fspack.config import (
    get_mirror,
)
from fspack.exceptions import NuitkaError
from fspack.packaging.nuitka import NuitkaCompiler
from fspack.platform import Platform
from fspack.progress import StageRecorder
from tests._stubs import (
    CompletedStub,
    FailedStub,
    make_nuitka_cache,
    patch_winlibs_hit,
)


class _ImportAbsent:
    """subprocess.run 失败返回值桩（模拟 import 失败）."""

    returncode = 1
    stdout = ""
    stderr = "ModuleNotFoundError: No module named 'pip'"


def _make_local_sdist(wheels_dir: Path, name: str = "Nuitka-4.1.3.tar.gz") -> Path:
    """在 wheel 缓存目录放置 nuitka sdist 归档，返回归档路径.

    识别只看文件名（``_find_local_nuitka_sdist`` 纯文件系统扫描），
    空文件即可；``ensure_env`` 的 pip install 在测试中均被 mock。
    """
    wheels_dir.mkdir(parents=True, exist_ok=True)
    sdist = wheels_dir / name
    sdist.write_bytes(b"")
    return sdist


# ---- _nuitka_cache_dir 与 _is_nuitka_cached 测试 ----


def test_nuitka_cache_dir_path(tmp_path: Path) -> None:
    """_nuitka_cache_dir 返回 cache_root / py_version / site-packages."""
    cache_root = tmp_path / "nuitka_cache"
    cache_dir = NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9")
    assert cache_dir == cache_root / "3.11.9" / "site-packages"


def test_is_nuitka_cached_true_when_init_exists(tmp_path: Path) -> None:
    """缓存目录有 nuitka/__init__.py 时 _is_nuitka_cached 返回 True."""
    cache_dir = make_nuitka_cache(tmp_path / "cache")
    assert NuitkaCompiler._is_nuitka_cached(cache_dir) is True


def test_is_nuitka_cached_false_when_missing(tmp_path: Path) -> None:
    """缓存目录无 nuitka 包时 _is_nuitka_cached 返回 False."""
    assert NuitkaCompiler._is_nuitka_cached(tmp_path / "empty") is False


def test_is_nuitka_cached_false_when_init_missing(tmp_path: Path) -> None:
    """缓存目录有 nuitka/ 但无 __init__.py 时返回 False（PEP 420 命名空间包不算）."""
    (tmp_path / "nuitka").mkdir()
    assert NuitkaCompiler._is_nuitka_cached(tmp_path) is False


# ---- _find_local_nuitka_sdist 本地 sdist 归档识别测试 ----


def test_find_local_nuitka_sdist_official_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """wheels 目录下有官方命名 Nuitka-<ver>.tar.gz 时识别命中（大小写不敏感）."""
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    sdist = _make_local_sdist(tmp_path / "cache" / "wheels", "Nuitka-4.1.3.tar.gz")
    assert NuitkaCompiler._find_local_nuitka_sdist("4.1.3") == sdist


def test_find_local_nuitka_sdist_lowercase_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """小写规范化命名 nuitka-<ver>.tar.gz（部分镜像重命名）同样命中."""
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    sdist = _make_local_sdist(tmp_path / "cache" / "wheels", "nuitka-4.1.3.tar.gz")
    assert NuitkaCompiler._find_local_nuitka_sdist("4.1.3") == sdist


def test_find_local_nuitka_sdist_nested_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """递归扫描子目录命中（用户按包名归档到 wheels 下的子目录存放）."""
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    sdist = _make_local_sdist(tmp_path / "cache" / "wheels" / "nuitka", "Nuitka-4.1.3.tar.gz")
    assert NuitkaCompiler._find_local_nuitka_sdist("4.1.3") == sdist


def test_find_local_nuitka_sdist_version_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """版本不匹配的归档不识别（避免装错版本破坏版本锁定约束）."""
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    _make_local_sdist(tmp_path / "cache" / "wheels", "Nuitka-2.5.1.tar.gz")
    assert NuitkaCompiler._find_local_nuitka_sdist("4.1.3") is None


def test_find_local_nuitka_sdist_ignores_other_tarballs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """其他包的 tar.gz 归档不误识别（仅精确匹配 nuitka-<ver>.tar.gz 文件名）."""
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    _make_local_sdist(tmp_path / "cache" / "wheels", "odfpy-1.4.1.tar.gz")
    assert NuitkaCompiler._find_local_nuitka_sdist("4.1.3") is None


def test_find_local_nuitka_sdist_missing_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """wheels 目录不存在时返回 None（缓存未初始化的首次构建）."""
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    assert NuitkaCompiler._find_local_nuitka_sdist("4.1.3") is None


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


def test_ensure_env_windows_prefills_winlibs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_env 在 Windows 且 py<3.13 时预填充 winlibs（scons fallback 到 winlibs）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"
    make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9"))

    called: list[str] = []
    real = NuitkaCompiler.ensure_winlibs_mingw.__func__

    def _spy(cls: object, py_version: str, stage: StageRecorder) -> Path:
        called.append(py_version)
        return real(cls, py_version, stage)  # type: ignore[arg-type]

    monkeypatch.setattr(NuitkaCompiler, "ensure_winlibs_mingw", classmethod(_spy))

    st = StageRecorder("Nuitka 环境")
    NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert called == ["3.11.9"]


def test_ensure_env_windows_py313_prefills_winlibs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_env 在 Windows py>=3.13 也预填充 winlibs（编译命令 force-mingw64 强制走 winlibs）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch, nuitka_ver="4.1.3")
    cache_root = tmp_path / "nuitka_cache"
    make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.13.1"))

    called: list[str] = []
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_winlibs_mingw",
        classmethod(lambda cls, py_version, stage: called.append(py_version) or tmp_path),
    )

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.13.1", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"
    assert called == ["3.13.1"]


def test_ensure_env_windows_msvc_skips_winlibs_prefill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_env 在 Windows 检测到 MSVC 时跳过 winlibs 预填充（scons 优先 MSVC，200MB 下载纯浪费）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: True)
    cache_root = tmp_path / "nuitka_cache"
    make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.13.1"))

    # 预填充被调用即失败（MSVC 机器应跳过）
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_winlibs_mingw",
        classmethod(lambda cls, *a, **kw: (_ for _ in ()).throw(AssertionError("MSVC 机器不应预填充 winlibs"))),
    )

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.13.1", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


def test_ensure_env_compiler_mingw_prefills_with_msvc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compiler=mingw 时无视 MSVC 存在恒预填充 winlibs（force flag 顶掉 MSVC，scons 需缓存）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: True)
    cache_root = tmp_path / "nuitka_cache"
    make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.13.1"))

    called: list[str] = []
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_winlibs_mingw",
        classmethod(lambda cls, py_version, stage: called.append(py_version) or tmp_path),
    )

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(
        cache_root, "3.13.1", Platform.WINDOWS, get_mirror("aliyun"), stage=st, compiler="mingw"
    )
    assert nuitka_ver == "4.1.3"
    assert called == ["3.13.1"]


def test_ensure_env_compiler_msvc_skips_prefill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compiler=msvc 时跳过 winlibs 预填充（MSVC 缺失由构建入口 fail-fast 校验）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    # msvc_available=False 模拟无 MSVC 机器：compiler=msvc 也不应触发预填充
    # （入口校验缺失时报错，此处验证 ensure_env 层不再重复处理）
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: False)
    cache_root = tmp_path / "nuitka_cache"
    make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.13.1"))

    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_winlibs_mingw",
        classmethod(lambda cls, *a, **kw: (_ for _ in ()).throw(AssertionError("compiler=msvc 不应预填充"))),
    )

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(
        cache_root, "3.13.1", Platform.WINDOWS, get_mirror("aliyun"), stage=st, compiler="msvc"
    )
    assert nuitka_ver == "4.1.3"


def test_ensure_env_compiler_non_windows_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """非 Windows 目标显式指定 compiler 时告警忽略（Linux/macOS 用系统 gcc/clang）."""
    import logging

    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    cache_root = tmp_path / "nuitka_cache"
    make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9"))

    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_winlibs_mingw",
        classmethod(lambda cls, *a, **kw: (_ for _ in ()).throw(AssertionError("Linux 不应预填充"))),
    )

    st = StageRecorder("Nuitka 环境")
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.nuitka.env"):
        NuitkaCompiler.ensure_env(
            cache_root, "3.11.9", Platform.LINUX, get_mirror("aliyun"), stage=st, compiler="mingw"
        )
    assert any("仅对 Windows" in r.message for r in caplog.records)


def test_ensure_env_linux_skips_winlibs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_env 在 Linux 时不预填充 winlibs（Linux 用系统 gcc）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    cache_root = tmp_path / "nuitka_cache"
    make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9"))

    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_winlibs_mingw",
        classmethod(lambda cls, *a, **kw: (_ for _ in ()).throw(AssertionError("不应预填充"))),
    )

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.LINUX, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


# ---- ensure_env 环境就绪测试 ----


def test_ensure_env_cache_hit_skips_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存目录已有 nuitka 时跳过 pip install，stage 标注缓存命中."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"
    # 预装 nuitka 到缓存
    make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9"))

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    # winlibs 与 nuitka 两层缓存均命中
    assert st._hits == 2
    assert "4.1.3" in st._detail


def test_ensure_env_pip_install_target_to_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存未命中时用构建机 pip install --target 装 nuitka 到缓存目录（非 dist/runtime）."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"
    expected_cache_dir = NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9")

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    captured_cmd: list[list[str]] = []

    # _ensure_pip_available 检查有 pip → 成功；pip install → 成功
    def stateful_run(cmd: list[str], **kw: Any) -> object:
        captured_cmd.append(cmd)
        return CompletedStub()

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
    patch_winlibs_hit(tmp_path, monkeypatch)
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
    patch_winlibs_hit(tmp_path, monkeypatch)
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
        return CompletedStub()  # ensurepip + has_pip + pip install

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


def test_ensure_env_uv_self_heal_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensurepip 失败但 uv pip install pip 自救成功."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch)
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
            return FailedStub()  # _try_ensurepip 失败
        return CompletedStub()  # uv pip install pip 与后续

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


def test_has_pip_returns_bool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_has_pip 按 import pip 返回值返回 bool."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")

    # 成功
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: CompletedStub())
    assert NuitkaCompiler._has_pip(str(py)) is True

    # 失败
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _ImportAbsent())
    assert NuitkaCompiler._has_pip(str(py)) is False


def test_has_pip_timeout_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_has_pip 探测超时按无 pip 处理，不抛异常不永久挂起."""

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise subprocess.TimeoutExpired(cmd, timeout=kw.get("timeout", 60))

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)
    assert NuitkaCompiler._has_pip("C:/fake/python.exe") is False


def test_try_ensurepip_timeout_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_ensurepip 超时按失败处理，返回 False 交由调用方进入第二轮自救."""

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise subprocess.TimeoutExpired(cmd, timeout=kw.get("timeout", 300))

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)
    assert NuitkaCompiler._try_ensurepip("C:/fake/python.exe") is False


def test_try_uv_install_pip_timeout_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_uv_install_pip 超时按失败处理，返回 False 交由调用方报错."""

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise subprocess.TimeoutExpired(cmd, timeout=kw.get("timeout", 300))

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)
    assert NuitkaCompiler._try_uv_install_pip() is False


def test_try_ensurepip_invokes_python_m_ensurepip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_ensurepip 调用 `python -m ensurepip --default-pip`."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")

    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> object:
        captured.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    assert NuitkaCompiler._try_ensurepip(str(py)) is True
    assert captured[0] == [str(py), "-m", "ensurepip", "--default-pip"]


def test_try_uv_install_pip_invokes_uv_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_uv_install_pip 调用 `uv pip install pip`."""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> object:
        captured.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    assert NuitkaCompiler._try_uv_install_pip() is True
    assert captured[0] == ["uv", "pip", "install", "pip"]


def test_ensure_env_pip_install_fails_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip install 返回非零退出码时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch)
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
            return FailedStub()
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match=r"pip install nuitka==4\.1\.3 失败"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_pip_install_timeout_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip install nuitka 超时（网络半开挂起）时 raise NuitkaError，不永久阻塞."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # 缓存未命中
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(lambda cache_dir: False))

    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        # 第 1 次：_has_pip → 成功
        # 第 2 次：pip install → 超时挂起
        if state["n"] == 2:
            raise subprocess.TimeoutExpired(cmd, timeout=kw.get("timeout", 1800))
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match=r"pip install nuitka==4\.1\.3 超时"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_install_fails_cache_still_empty_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip install 成功但缓存目录仍无 nuitka 包时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # _is_nuitka_cached 始终返回 False（pip install 成功但缓存仍空）
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(lambda cache_dir: False))

    # _has_pip 成功，pip install 成功
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: CompletedStub())

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match="安装后缓存目录仍无 nuitka 包"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


# ---- ensure_env 本地 sdist 归档安装测试 ----


def test_ensure_env_local_sdist_online_prefers_local_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """wheels 目录有锁定版本 tar.gz 时优先本地安装：cmd 含 --find-links 与归档路径，仅一次 install."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch)
    wheels_dir = tmp_path / "cache" / "wheels"
    sdist = _make_local_sdist(wheels_dir, "Nuitka-4.1.3.tar.gz")
    cache_root = tmp_path / "nuitka_cache"
    expected_cache_dir = NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9")

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    captured_cmd: list[list[str]] = []

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        captured_cmd.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    def fake_is_cached(cache_dir: Path) -> bool:
        return cache_dir == expected_cache_dir and any("install" in c and "--target" in c for c in captured_cmd)

    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_cached))

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    pip_cmds = [c for c in captured_cmd if "install" in c and "--target" in c]
    assert len(pip_cmds) == 1, f"本地归档命中时应仅一次 pip install，实际 {len(pip_cmds)}"
    cmd = pip_cmds[0]
    # requirement 为本地归档路径，依赖经 --find-links 从 wheels 缓存解析
    assert cmd[-1] == str(sdist)
    assert cmd[cmd.index("--find-links") + 1] == str(wheels_dir)
    assert "-i" in cmd  # 在线模式仍带镜像源（构建依赖 setuptools 等从索引取）
    assert "--no-index" not in cmd
    assert cmd[cmd.index("--target") + 1] == str(expected_cache_dir)
    assert "本地 sdist" in st._detail
    # 归档保留不删（用户显式放置的资产）
    assert sdist.is_file()


def test_ensure_env_local_sdist_failure_falls_back_online(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地 sdist 安装失败时在线模式回退索引安装（归档保留不删）."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch)
    wheels_dir = tmp_path / "cache" / "wheels"
    sdist = _make_local_sdist(wheels_dir, "Nuitka-4.1.3.tar.gz")
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    captured_cmd: list[list[str]] = []
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        captured_cmd.append(cmd)
        state["n"] += 1
        # 第 1 次 _has_pip → 成功；第 2 次本地 sdist pip install → 失败；
        # 第 3 次在线 pip install → 成功
        if state["n"] == 2:
            return FailedStub()
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    def fake_is_cached(cache_dir: Path) -> bool:
        return any("install" in c and "--target" in c for c in captured_cmd)

    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_cached))

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    pip_cmds = [c for c in captured_cmd if "install" in c and "--target" in c]
    assert len(pip_cmds) == 2, f"本地失败后应回退一次在线安装，实际 {len(pip_cmds)}"
    assert pip_cmds[0][-1] == str(sdist)  # 第一次：本地归档
    assert pip_cmds[1][-1] == "nuitka==4.1.3"  # 第二次：索引解析
    assert "安装完成" in st._detail
    # 归档保留（失败原因可能是依赖缺失而非归档损坏，不自动删用户资产）
    assert sdist.is_file()


def test_ensure_env_wheels_dir_without_sdist_uses_index_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """wheels 目录存在但无匹配 tar.gz 时走原在线安装：cmd 不含 --find-links."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    patch_winlibs_hit(tmp_path, monkeypatch)
    wheels_dir = tmp_path / "cache" / "wheels"
    wheels_dir.mkdir(parents=True)
    (wheels_dir / "somepkg-1.0-py3-none-any.whl").write_bytes(b"")
    cache_root = tmp_path / "nuitka_cache"
    expected_cache_dir = NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9")

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    captured_cmd: list[list[str]] = []

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        captured_cmd.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    def fake_is_cached(cache_dir: Path) -> bool:
        return cache_dir == expected_cache_dir and any("install" in c and "--target" in c for c in captured_cmd)

    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_cached))

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    pip_cmds = [c for c in captured_cmd if "install" in c and "--target" in c]
    assert len(pip_cmds) == 1
    assert pip_cmds[0][-1] == "nuitka==4.1.3"
    assert "--find-links" not in pip_cmds[0]


# ---- ccache 相关测试 ----


def test_build_compile_env_without_ccache_sets_cc_compiler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 设 CC=gcc 避免 zig；Windows 不设 CC 只重定向下载缓存（scons 拒绝外部 gcc）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CC", raising=False)
    monkeypatch.delenv("CFLAGS", raising=False)

    # Linux：CC=gcc；NUITKA_CACHE_DIR 重定向编译缓存到 fspack 干净目录
    # （隔离 %LOCALAPPDATA%/~/.cache 的历史污染条目）
    env_linux = NuitkaCompiler._build_compile_env(Platform.LINUX, None)
    assert env_linux is not None
    assert env_linux["CC"] == "gcc"
    assert "CCACHE_DIR" not in env_linux
    assert env_linux["NUITKA_CACHE_DIR"] == str(tmp_path / "cache" / "nuitka-work")

    # Windows：CC 被 scons 无条件拒绝，不设避免 "Non downloaded winlibs-gcc
    # ... ignored" 噪音提示；NUITKA_CACHE_DIR_DOWNLOADS 重定向到 fspack 缓存目录
    env_win = NuitkaCompiler._build_compile_env(Platform.WINDOWS, None)
    assert env_win is not None
    assert "CC" not in env_win
    assert "CCACHE_DIR" not in env_win
    assert env_win["NUITKA_CACHE_DIR_DOWNLOADS"] == str(tmp_path / "cache" / "nuitka-winlibs-mingw")
    assert env_win["NUITKA_CACHE_DIR"] == str(tmp_path / "cache" / "nuitka-work")


def test_build_compile_env_with_ccache_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux ccache 环境设置 CC='"ccache 路径" gcc'（路径引号包裹防空格截断）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    ccache_exe = tmp_path / "ccache"
    ccache_exe.write_bytes(b"")
    env = NuitkaCompiler._build_compile_env(Platform.LINUX, ccache_exe)
    assert env is not None
    assert env["CC"] == f'"{ccache_exe}" gcc'
    assert "CCACHE_DIR" in env


def test_build_compile_env_with_ccache_windows_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 忽略 ccache_exe：CC 被 scons 拒绝，ccache 前缀无意义不设置."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CC", raising=False)
    ccache_exe = tmp_path / "ccache.exe"
    ccache_exe.write_bytes(b"")
    env = NuitkaCompiler._build_compile_env(Platform.WINDOWS, ccache_exe)
    assert env is not None
    assert "CC" not in env
    assert "CCACHE_DIR" not in env


def test_build_compile_env_windows_clears_host_cc_cflags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 清除宿主残留的 CC/CFLAGS（scons 拒绝外部 gcc，残留仅产生噪音提示）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CC", "x86_64-w64-mingw32-gcc")
    monkeypatch.setenv("CFLAGS", "-D_WIN32_WINNT=0x0601")
    env = NuitkaCompiler._build_compile_env(Platform.WINDOWS, None)
    assert "CC" not in env
    assert "CFLAGS" not in env
    assert "NUITKA_CACHE_DIR_DOWNLOADS" in env


def test_build_compile_env_windows_no_cflags_injected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 不注入 CFLAGS（Nuitka scons 自设 _WIN32_WINNT，注入触发 Inherited CFLAGS 提示）.

    Nuitka 4.1.3 无条件 ``_WIN32_WINNT=0x0601``（Win7），2.5.1 mingw 分支
    ``0x0501``（更保守），fspack 注入同宏纯冗余且触发 "Inherited CFLAGS"
    噪音提示，已删除。
    """
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CFLAGS", raising=False)
    env = NuitkaCompiler._build_compile_env(Platform.WINDOWS, None)
    assert "CFLAGS" not in env


def test_build_compile_env_skips_win32_winnt_for_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标不设置 _WIN32_WINNT（Linux 无此兼容性问题）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CFLAGS", raising=False)
    env = NuitkaCompiler._build_compile_env(Platform.LINUX, None)
    # Linux 不应添加 _WIN32_WINNT
    cflags = env.get("CFLAGS", "")
    assert "_WIN32_WINNT" not in cflags
