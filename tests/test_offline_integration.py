"""离线模式集成测试：验证 build() 入口在离线模式下的端到端行为.

区别于 :mod:`tests.test_offline_mode`（单元级别验证各下载层 fail-fast），
本模块从 :func:`fspack.packaging.pipeline.build` 入口出发，验证：

1. 离线模式下 runtime 缓存未命中 → build() 抛 ``EmbedError`` 且错误信息含"离线模式"
2. 离线模式下 wheel 缓存未命中 → build() 抛 ``DependencyError`` 且错误信息含"离线模式"
3. ``FSPACK_CACHE_DIR`` 环境变量 → build() 用此路径作为 runtime 缓存
4. 非离线模式缓存未命中 → 不抛离线异常，回退到网络下载路径

集成测试用本模块内构造的最小项目（无第三方依赖）与
内联构造的 flask 依赖项目（原 ``assets/templates/web/web_app`` 模板已精简
删除）作为真实项目输入。runtime/网络层用 monkeypatch 替身，不实际下载或解压。

**平台覆盖**：Windows（embed python）与 Linux（python-build-standalone）的
差异（默认版本、runtime marker、缓存子目录、解压函数）统一由
``_PLATFORM_CASES`` 参数化表达，每个场景在两种平台上各跑一次。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest

from fspack.config import get_mirror
from fspack.exceptions import DependencyError, EmbedError
from fspack.packaging.runtime import STANDALONE_RELEASE_TAG, standalone_tarball_name
from fspack.platform import Platform
from tests._stubs import fail_urlopen

_MIRROR = get_mirror()

# Linux 默认 Python 版本（python-build-standalone 3.11.15）
_LINUX_PY_VERSION = "3.11.15"

# (平台标识, target, py_version)
_PLATFORM_CASES = [
    pytest.param("windows", Platform.WINDOWS, "3.11.9", id="windows"),
    pytest.param("linux", Platform.LINUX, _LINUX_PY_VERSION, id="linux"),
]


def _cache_subdir(kind: str) -> str:
    """runtime 缓存在 FSPACK_CACHE_DIR 下的子目录名."""
    return "embed" if kind == "windows" else "standalone"


def _make_flask_project(tmp_path: Path, name: str = "flask_app") -> Path:
    """构造声明 flask 依赖的最小项目，返回项目路径.

    原 ``assets/templates/web/web_app`` 模板已精简删除，此处内联构造等价
    输入（pyproject 声明 flask 依赖 + 入口脚本），保持离线 wheel 缓存
    未命中路径的测试语义不变。
    """
    proj = tmp_path / name
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = ["flask"]\n',
        encoding="utf-8",
    )
    (proj / f"{name}.py").write_text("def main():\n    print('hi')\n", encoding="utf-8")
    return proj


def _make_min_project(tmp_path: Path, name: str = "app") -> Path:
    """构造无第三方依赖的最小项目，返回项目路径.

    原无依赖模板 cli_helloworld 已移除，剩余无依赖模板仅 tk_app（tkinter 会
    触发 Windows 内置库补充，偏离离线行为测试焦点），故此处内联构造最小项目。
    """
    proj = tmp_path / name
    proj.mkdir()
    (proj / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n', encoding="utf-8")
    (proj / f"{name}.py").write_text("def main():\n    print('hi')\n", encoding="utf-8")
    return proj


def _prepare_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    *,
    offline: bool,
    cache_dir: Path | None = None,
) -> None:
    """设置离线开关、缓存目录与 Linux 平台守卫（测试可在任意宿主运行）."""
    if offline:
        monkeypatch.setenv("FSPACK_OFFLINE", "1")
    else:
        monkeypatch.delenv("FSPACK_OFFLINE", raising=False)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(cache_dir or tmp_path / "cache"))
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    if kind == "linux":
        monkeypatch.setattr("fspack.packaging.pipeline.executor.detect_platform", lambda: Platform.LINUX)


def _mark_runtime_ready(project: Path, kind: str) -> None:
    """在 dist/ 下写入平台对应的 runtime 就绪 marker，跳过 runtime 下载阶段."""
    runtime_dir = project / "dist" / "runtime"
    if kind == "windows":
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "python311.dll").write_bytes(b"")
        (runtime_dir / "python311._pth").write_text("..\\site-packages\n", encoding="utf-8")
    else:
        python_bin = runtime_dir / "python" / "bin"
        python_bin.mkdir(parents=True, exist_ok=True)
        (python_bin / "python3.11").write_bytes(b"")
    # site-packages 存在但不包含 flask，触发 wheel 下载
    (project / "dist" / "site-packages").mkdir(parents=True, exist_ok=True)


def _prefill_runtime_cache(cache_dir: Path, kind: str, py_version: str) -> None:
    """预填充 runtime 缓存，使离线 build() 能命中缓存."""
    sub = cache_dir / ("embed" if kind == "windows" else "standalone")
    sub.mkdir(parents=True, exist_ok=True)
    if kind == "windows":
        (sub / f"python-{py_version}-embed-amd64.zip").write_bytes(b"cached embed zip")
    else:
        (sub / standalone_tarball_name(py_version, STANDALONE_RELEASE_TAG)).write_bytes(b"cached standalone tarball")


def _mock_extract(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """mock 解压（假归档无法解压）."""
    fn = "extract_embed" if kind == "windows" else "extract_standalone"
    monkeypatch.setattr(f"fspack.packaging.pipeline.stages.{fn}", lambda *a, **kw: None)


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


def _fake_urlopen(req: Request, timeout: int, **kwargs: Any) -> _FakeResp:
    return _FakeResp()


def _build_without_offline_error(proj: Path, py_version: str, target: Platform) -> None:
    """构建并断言不出现"离线模式"异常（其他异常如 mingw 缺失允许）."""
    from fspack.builder import build

    try:
        build(proj, _MIRROR, py_version, target=target)
    except EmbedError as e:
        if "离线模式" in str(e):
            pytest.fail(f"缓存命中/非离线模式不应抛离线异常: {e}")
    except Exception as e:
        # 其他异常（如 mingw 不可用、假归档无法解压）允许
        assert "离线模式" not in str(e), f"不应抛离线异常: {e}"


# ---- 离线模式 + runtime 缓存未命中 → build() 抛 EmbedError ----


@pytest.mark.parametrize(("kind", "target", "py_version"), _PLATFORM_CASES)
def test_build_offline_runtime_cache_miss_raises_embed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    target: Platform,
    py_version: str,
) -> None:
    """离线模式下 runtime 缓存未命中 → build() 抛 EmbedError，错误信息含"离线模式".

    用无第三方依赖的最小项目，跳过 wheel 下载阶段，
    聚焦验证 build() 入口能正确传递 runtime 层的离线异常。
    """
    _prepare_env(tmp_path, monkeypatch, kind, offline=True)

    proj = _make_min_project(tmp_path)
    from fspack.builder import build

    with pytest.raises(EmbedError, match=r"离线模式下.*缓存未命中"):
        build(proj, _MIRROR, py_version, target=target)


@pytest.mark.parametrize(("kind", "target", "py_version"), _PLATFORM_CASES)
def test_build_offline_wheel_cache_miss_raises_dependency_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    target: Platform,
    py_version: str,
) -> None:
    """离线模式下 wheel 缓存未命中 → build() 抛 DependencyError，错误信息含"离线模式".

    用内联 flask 项目（声明 flask 依赖），mock runtime 已就绪（跳过 runtime 下载），
    mock pip download --no-index 失败（缓存未命中），验证 build() 抛离线异常。
    """
    _prepare_env(tmp_path, monkeypatch, kind, offline=True)

    proj = _make_flask_project(tmp_path)
    _mark_runtime_ready(proj, kind)

    # mock _run_pip 返回 None（模拟 --no-index 失败，缓存未命中）
    # 不 mock subprocess.run，避免影响 inject_mingw_runtime_dlls 的 gcc 查询
    monkeypatch.setattr("fspack.packaging.wheels.process._run_pip", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")

    from fspack.builder import build

    with pytest.raises(DependencyError, match=r"离线模式下.*依赖缓存未命中"):
        build(proj, _MIRROR, py_version, target=target)


# ---- FSPACK_CACHE_DIR 环境变量 → build() 用此路径作为 runtime 缓存 ----


@pytest.mark.slow
@pytest.mark.parametrize(("kind", "target", "py_version"), _PLATFORM_CASES)
def test_build_offline_uses_cache_dir_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    target: Platform,
    py_version: str,
) -> None:
    """FSPACK_CACHE_DIR 环境变量 → build() 用此路径作为 runtime 缓存.

    验证集成点：build() 通过 ``embed_cache_dir()`` 函数（间接调 ``cache_root()``）
    读取环境变量，将 runtime 归档缓存到指定路径。离线模式下预先放入归档，
    build() 应在下载阶段命中缓存（不抛离线异常），进入下一阶段。
    """
    cache_dir = tmp_path / "custom-cache"
    _prefill_runtime_cache(cache_dir, kind, py_version)

    _prepare_env(tmp_path, monkeypatch, kind, offline=True, cache_dir=cache_dir)
    _mock_extract(monkeypatch, kind)

    proj = _make_min_project(tmp_path)
    # 缓存命中后进入解压 → 复制源码 → 编译 loader 等阶段；
    # 这些阶段可能因 mingw 等本机条件失败，仅要求不出现离线异常
    _build_without_offline_error(proj, py_version, target)


# ---- 非离线模式缓存未命中 → 回退到网络下载路径 ----


@pytest.mark.slow
@pytest.mark.parametrize(("kind", "target", "py_version"), _PLATFORM_CASES)
def test_build_non_offline_falls_back_to_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    target: Platform,
    py_version: str,
) -> None:
    """非离线模式 runtime 缓存未命中 → 不抛离线异常，走网络下载路径.

    用 mock urlopen 返回假数据模拟网络下载成功，验证 build() 不因缓存未命中
    而抛"离线模式"异常。区别于 test_download_embed_offline_disabled（单元级别），
    本测试从 build() 入口验证集成行为。
    """
    _prepare_env(tmp_path, monkeypatch, kind, offline=False)
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    _mock_extract(monkeypatch, kind)

    proj = _make_min_project(tmp_path)
    # 缓存未命中 + 非离线模式 → 走网络下载，不应抛"离线模式"异常
    _build_without_offline_error(proj, py_version, target)


# ---- 离线模式错误信息包含已搜索路径 ----


@pytest.mark.parametrize(("kind", "target", "py_version"), _PLATFORM_CASES)
def test_build_offline_error_lists_searched_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    target: Platform,
    py_version: str,
) -> None:
    """离线模式下 build() 抛的 EmbedError 包含对应平台的缓存路径，便于用户排查."""
    custom_cache = tmp_path / "my-cache"
    _prepare_env(tmp_path, monkeypatch, kind, offline=True, cache_dir=custom_cache)

    proj = _make_min_project(tmp_path)
    from fspack.builder import build

    with pytest.raises(EmbedError) as exc_info:
        build(proj, _MIRROR, py_version, target=target)
    msg = str(exc_info.value)
    # 错误信息应包含平台缓存路径，便于用户定位
    assert str(custom_cache / _cache_subdir(kind)) in msg or _cache_subdir(kind) in msg
    assert "离线模式" in msg
