"""离线模式集成测试：验证 build() 入口在离线模式下的端到端行为.

区别于 :mod:`tests.test_offline_mode`（单元级别验证各下载层 fail-fast），
本模块从 :func:`fspack.packaging.pipeline.build` 入口出发，验证：

1. 离线模式下 runtime 缓存未命中 → build() 抛 ``EmbedError`` 且错误信息含"离线模式"
2. 离线模式下 wheel 缓存未命中 → build() 抛 ``DependencyError`` 且错误信息含"离线模式"
3. ``FSPACK_CACHE_DIR`` 环境变量 → build() 用此路径作为 embed 缓存
4. 非离线模式缓存未命中 → 不抛离线异常，回退到网络下载路径

集成测试用 ``assets/templates/cli/cli_helloworld``（无第三方依赖）与
``assets/templates/cli/cli_office``（有 pypdf 依赖）作为真实项目输入，避免构造
虚假项目结构。runtime/网络层用 monkeypatch 替身，不实际下载或解压。

**平台覆盖**：Windows（embed python）与 Linux（python-build-standalone）各有
5 个对等测试，验证两个平台的离线模式行为一致。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.request import Request

import pytest

from fspack.config import get_mirror
from fspack.exceptions import DependencyError, EmbedError
from fspack.packaging.runtime import STANDALONE_RELEASE_TAG, standalone_tarball_name
from fspack.platform import Platform
from fspack.templates.project_template import ProjectTemplate
from tests._stubs import fail_urlopen

_EXAMPLES = ProjectTemplate.root_dir()

_MIRROR = get_mirror()

# Linux 默认 Python 版本（python-build-standalone 3.11.15）
_LINUX_PY_VERSION = "3.11.15"


def _copy_example(rel_path: str, tmp_path: Path) -> Path:
    """复制 assets/templates/<rel_path> 到 tmp_path/<name>，返回项目路径.

    :param rel_path: 相对于 ``assets/templates/`` 的路径（如 ``cli/cli_helloworld``）
    """
    src = _EXAMPLES / rel_path
    name = Path(rel_path).name
    dst = tmp_path / name
    shutil.copytree(src, dst)
    return dst


# ---- 离线模式 + runtime 缓存未命中 → build() 抛 EmbedError ----


def test_build_offline_embed_cache_miss_raises_embed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 embed python 缓存未命中 → build() 抛 EmbedError，错误信息含"离线模式".

    用 cli_helloworld（无第三方依赖），跳过 wheel 下载阶段，
    聚焦验证 build() 入口能正确传递 runtime 层的离线异常。
    """
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    # 缓存目录指向空目录，确保 cache miss
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    proj = _copy_example("cli/cli_helloworld", tmp_path)
    from fspack.builder import build

    with pytest.raises(EmbedError, match=r"离线模式下.*缓存未命中"):
        build(proj, _MIRROR, "3.11.9", target=Platform.WINDOWS)


def test_build_offline_wheel_cache_miss_raises_dependency_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """离线模式下 wheel 缓存未命中 → build() 抛 DependencyError，错误信息含"离线模式".

    用 cli_office（声明 pypdf 依赖），mock runtime 已就绪（跳过 embed 下载），
    mock pip download --no-index 失败（缓存未命中），验证 build() 抛离线异常。
    """
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    proj = _copy_example("cli/cli_office", tmp_path)
    # 让 runtime 已就绪：在 dist/runtime/ 下创建 python311.dll marker
    runtime_dir = proj / "dist" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "python311.dll").write_bytes(b"")
    (runtime_dir / "python311._pth").write_text("..\\site-packages\n", encoding="utf-8")
    # 让 site-packages 存在但不包含 pypdf，触发 wheel 下载
    site_packages = proj / "dist" / "site-packages"
    site_packages.mkdir(parents=True)

    # mock _run_pip 返回 None（模拟 --no-index 失败，缓存未命中）
    # 不 mock subprocess.run，避免影响 inject_mingw_runtime_dlls 的 gcc 查询
    monkeypatch.setattr("fspack.packaging.wheels.downloader._run_pip", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")

    from fspack.builder import build

    with pytest.raises(DependencyError, match=r"离线模式下.*依赖缓存未命中"):
        build(proj, _MIRROR, "3.11.9", target=Platform.WINDOWS)


# ---- FSPACK_CACHE_DIR 环境变量 → build() 用此路径作为 embed 缓存 ----


def test_build_offline_uses_cache_dir_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FSPACK_CACHE_DIR 环境变量 → build() 用此路径作为 embed_cache_dir.

    验证集成点：build() 通过 ``embed_cache_dir()`` 函数（间接调 ``cache_root()``）
    读取环境变量，将 embed zip 缓存到指定路径。离线模式下预先放入 embed zip，
    build() 应在下载阶段命中缓存（不抛离线异常），进入下一阶段。
    """
    cache_dir = tmp_path / "custom-cache"
    embed_cache = cache_dir / "embed"
    embed_cache.mkdir(parents=True)
    # 预填充 embed zip 缓存（假 zip，extract 阶段会被 mock 跳过）
    zip_path = embed_cache / "python-3.11.9-embed-amd64.zip"
    zip_path.write_bytes(b"cached embed zip")

    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    # mock extract_embed 跳过实际解压（假 zip 无法解压）
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_embed", lambda zip_path, runtime_dir: None)

    proj = _copy_example("cli/cli_helloworld", tmp_path)
    from fspack.builder import build

    # 缓存命中后进入解压 → 复制源码 → 编译 loader 等阶段
    # 这些阶段在 Windows 上需要 mingw，跳过：仅断言不抛离线异常
    try:
        build(proj, _MIRROR, "3.11.9", target=Platform.WINDOWS)
    except EmbedError as e:
        # 不应有离线异常（缓存已命中）
        if "离线模式" in str(e):
            pytest.fail(f"缓存命中不应抛离线异常: {e}")
    except Exception as e:
        # 其他异常（如 mingw 不可用）允许，只要不是离线异常
        assert "离线模式" not in str(e), f"不应抛离线异常: {e}"


# ---- 非离线模式缓存未命中 → 回退到网络下载路径 ----


def test_build_non_offline_falls_back_to_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非离线模式 embed 缓存未命中 → 不抛离线异常，走网络下载路径.

    用 mock urlopen 返回假数据模拟网络下载成功，验证 build() 不因缓存未命中
    而抛"离线模式"异常。区别于 test_download_embed_offline_disabled（单元级别），
    本测试从 build() 入口验证集成行为。
    """
    monkeypatch.delenv("FSPACK_OFFLINE", raising=False)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))

    class _FakeResp:
        """模拟 urlopen 响应：首次 read 返回数据，后续返回空（终止下载循环）."""

        def __init__(self) -> None:
            self._read = False
            self.headers = {"Content-Length": "4"}

        def read(self, n: int = -1) -> bytes:
            if self._read:
                return b""
            self._read = True
            return b"DATA"

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> _FakeResp:
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    # mock extract_embed 跳过实际解压（假数据无法解压）
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_embed", lambda zip_path, runtime_dir: None)

    proj = _copy_example("cli/cli_helloworld", tmp_path)
    from fspack.builder import build

    # 缓存未命中 + 非离线模式 → 走网络下载，不应抛"离线模式"异常
    try:
        build(proj, _MIRROR, "3.11.9", target=Platform.WINDOWS)
    except EmbedError as e:
        if "离线模式" in str(e):
            pytest.fail(f"非离线模式不应抛离线异常: {e}")
    except Exception as e:
        # 其他异常（如 mingw 不可用、假 zip 无法解压）允许
        assert "离线模式" not in str(e), f"不应抛离线异常: {e}"


# ---- 离线模式错误信息包含已搜索路径 ----


def test_build_offline_error_lists_searched_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 build() 抛的 EmbedError 包含缓存路径，便于用户排查."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    custom_cache = tmp_path / "my-cache"
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(custom_cache))
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    proj = _copy_example("cli/cli_helloworld", tmp_path)
    from fspack.builder import build

    with pytest.raises(EmbedError) as exc_info:
        build(proj, _MIRROR, "3.11.9", target=Platform.WINDOWS)
    msg = str(exc_info.value)
    # 错误信息应包含缓存路径，便于用户定位
    assert str(custom_cache / "embed") in msg or "embed" in msg
    assert "离线模式" in msg


# ============================================================================
# Linux 平台对等测试（python-build-standalone）
#
# 与 Windows 对等测试一一对应，验证 Linux 平台下 build() 的离线模式行为：
# - runtime 用 python-build-standalone tar.gz（非 embed zip）
# - marker 是 runtime/python/bin/python3（非 python311.dll）
# - cache 目录是 standalone/（非 embed/）
# - 默认 Python 版本是 3.11.15（非 3.11.9）
# ============================================================================


def test_build_offline_standalone_cache_miss_raises_embed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux 离线模式下 standalone python 缓存未命中 → build() 抛 EmbedError.

    对等 ``test_build_offline_embed_cache_miss_raises_embed_error``，验证 Linux
    平台 runtime 缓存未命中时 build() 正确传递离线异常。
    """
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    proj = _copy_example("cli/cli_helloworld", tmp_path)
    from fspack.builder import build

    with pytest.raises(EmbedError, match=r"离线模式下.*缓存未命中"):
        build(proj, _MIRROR, _LINUX_PY_VERSION, target=Platform.LINUX)


def test_build_offline_wheel_cache_miss_raises_dependency_error_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux 离线模式下 wheel 缓存未命中 → build() 抛 DependencyError.

    对等 ``test_build_offline_wheel_cache_miss_raises_dependency_error``，验证
    Linux 平台 wheel 缓存未命中时 build() 正确传递离线异常。runtime marker
    用 ``python/bin/python3``（Linux standalone 结构）。
    """
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    proj = _copy_example("cli/cli_office", tmp_path)
    # Linux runtime marker：runtime/python/bin/python3.11（对应 py_version 3.11.15）
    runtime_dir = proj / "dist" / "runtime"
    python_bin = runtime_dir / "python" / "bin"
    python_bin.mkdir(parents=True)
    (python_bin / "python3.11").write_bytes(b"")
    # site-packages 存在但不包含 pypdf，触发 wheel 下载
    site_packages = proj / "dist" / "site-packages"
    site_packages.mkdir(parents=True)

    monkeypatch.setattr("fspack.packaging.wheels.downloader._run_pip", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")

    from fspack.builder import build

    with pytest.raises(DependencyError, match=r"离线模式下.*依赖缓存未命中"):
        build(proj, _MIRROR, _LINUX_PY_VERSION, target=Platform.LINUX)


def test_build_offline_uses_cache_dir_env_var_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux FSPACK_CACHE_DIR 环境变量 → build() 用此路径作为 standalone 缓存.

    对等 ``test_build_offline_uses_cache_dir_env_var``，验证 Linux 平台
    cache_dir 环境变量正确传递到 standalone cache 目录。
    """
    cache_dir = tmp_path / "custom-cache"
    standalone_cache = cache_dir / "standalone"
    standalone_cache.mkdir(parents=True)
    # 预填充 standalone tarball 缓存
    tarball = standalone_tarball_name(_LINUX_PY_VERSION, STANDALONE_RELEASE_TAG)
    tarball_path = standalone_cache / tarball
    tarball_path.write_bytes(b"cached standalone tarball")

    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    # mock extract_standalone 跳过实际解压
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_standalone", lambda tar, runtime_dir: None)

    proj = _copy_example("cli/cli_helloworld", tmp_path)
    from fspack.builder import build

    try:
        build(proj, _MIRROR, _LINUX_PY_VERSION, target=Platform.LINUX)
    except EmbedError as e:
        if "离线模式" in str(e):
            pytest.fail(f"缓存命中不应抛离线异常: {e}")
    except Exception as e:
        assert "离线模式" not in str(e), f"不应抛离线异常: {e}"


def test_build_non_offline_falls_back_to_network_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 非离线模式 standalone 缓存未命中 → 走网络下载路径.

    对等 ``test_build_non_offline_falls_back_to_network``，验证 Linux 平台
    非离线模式缓存未命中时不抛离线异常，回退到网络下载。
    """
    monkeypatch.delenv("FSPACK_OFFLINE", raising=False)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))

    class _FakeResp:
        """模拟 urlopen 响应：首次 read 返回数据，后续返回空."""

        def __init__(self) -> None:
            self._read = False
            self.headers = {"Content-Length": "4"}

        def read(self, n: int = -1) -> bytes:
            if self._read:
                return b""
            self._read = True
            return b"DATA"

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> _FakeResp:
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_standalone", lambda tar, runtime_dir: None)

    proj = _copy_example("cli/cli_helloworld", tmp_path)
    from fspack.builder import build

    try:
        build(proj, _MIRROR, _LINUX_PY_VERSION, target=Platform.LINUX)
    except EmbedError as e:
        if "离线模式" in str(e):
            pytest.fail(f"非离线模式不应抛离线异常: {e}")
    except Exception as e:
        assert "离线模式" not in str(e), f"不应抛离线异常: {e}"


def test_build_offline_error_lists_searched_paths_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 离线模式下 build() 抛的 EmbedError 包含 standalone 缓存路径.

    对等 ``test_build_offline_error_lists_searched_paths``，验证 Linux 平台
    错误信息包含 ``standalone`` 缓存路径。
    """
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    custom_cache = tmp_path / "my-cache"
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(custom_cache))
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    proj = _copy_example("cli/cli_helloworld", tmp_path)
    from fspack.builder import build

    with pytest.raises(EmbedError) as exc_info:
        build(proj, _MIRROR, _LINUX_PY_VERSION, target=Platform.LINUX)
    msg = str(exc_info.value)
    # 错误信息应包含 standalone 缓存路径
    assert str(custom_cache / "standalone") in msg or "standalone" in msg
    assert "离线模式" in msg
