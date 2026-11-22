"""多入口 loader 共享对象、缓存热路径与真实 MinGW 构建性能基线.

普通门禁覆盖确定性不变量：工具调用次数不随入口数增长、预热构建不启动
工具、工具链身份热查询不重复探测。``slow`` 基线用真实 MinGW 测量单入口/
多入口冷构建与多入口热构建，供 ``fsp doctor --bench-compare`` 检测退化。
"""

from __future__ import annotations

import itertools
import shutil
import statistics
import subprocess
import time
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from threading import Lock
from typing import Any

import pytest

from fspack import progress
from fspack.config import AppType
from fspack.metrics import StageRecorder
from fspack.packaging.loader import LoaderVersionInfo, compile_loader, generate_loader_source, mingw_available
from fspack.packaging.loader import toolchain as toolchain_module
from fspack.packaging.loader.toolchain import _find_windres
from fspack.packaging.pipeline.stages import _build_entry_loaders
from fspack.platform import Platform
from tests.test_entry_loaders import _make_multi_entry_context

_WINDOWS = Platform.WINDOWS
_TOOL_NAMES = ("x86_64-w64-mingw32-gcc", "x86_64-w64-mingw32-windres", "gcc", "windres")
_PROBE_FLAGS = ("-v", "--version")


class _ToolSpy:
    """模拟编译器/资源工具，按命令形状区分探测、C 编译、资源与链接."""

    def __init__(self, bin_dir: Path) -> None:
        self.bin_dir = bin_dir
        self.calls: list[str] = []
        self._lock = Lock()

    def which(self, name: str) -> str:
        path = self.bin_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"fake tool")
        return str(path)

    def run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        kind = self.kind(cmd)
        with self._lock:
            self.calls.append(kind)
        if kind == "probe":
            return subprocess.CompletedProcess(cmd, 0, self._probe_output(cmd), "")
        output = Path(cmd[cmd.index("--output" if kind == "resource" else "-o") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(self._artifact(cmd, kind))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    @staticmethod
    def kind(cmd: list[str]) -> str:
        if len(cmd) == 2 and cmd[1].startswith("-"):
            return "probe"
        if "--output" in cmd:
            return "resource"
        if "-c" in cmd:
            return "object"
        return "link"

    @staticmethod
    def _probe_output(cmd: list[str]) -> str:
        flag = cmd[1]
        if flag in _PROBE_FLAGS:
            return "fake tool version\n"
        if flag == "-dumpmachine":
            return "x86_64-w64-mingw32\n"
        if flag.startswith("-print-file-name="):
            return flag.partition("=")[2] + "\n"
        return ""

    @staticmethod
    def _artifact(cmd: list[str], kind: str) -> bytes:
        if kind == "object":
            source = Path(cmd[cmd.index("-c") + 1]).read_bytes()
            return b"object:" + source
        if kind == "resource":
            resource = Path(cmd[cmd.index("--input") + 1])
            payload = resource.read_bytes() + (resource.parent / "app.manifest").read_bytes()
            icon = resource.parent / "icon.ico"
            return payload + (icon.read_bytes() if icon.is_file() else b"")
        objects = b"".join(Path(arg).read_bytes() for arg in cmd if arg.endswith((".o", ".obj")))
        return (b"gui:" if "-mwindows" in cmd else b"cli:") + objects

    def reset(self) -> None:
        with self._lock:
            self.calls = []

    def count(self, kind: str) -> int:
        with self._lock:
            return self.calls.count(kind)


@pytest.fixture(autouse=True)
def _reset_toolchain_memo() -> Iterator[None]:
    """隔离全局工具链/which 记忆，避免用例间探测次数互相影响."""
    toolchain_module._fingerprints.clear()
    toolchain_module._which_results.clear()
    yield
    toolchain_module._fingerprints.clear()
    toolchain_module._which_results.clear()


def _fake_toolchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ToolSpy:
    spy = _ToolSpy(tmp_path / "bin")
    for name in _TOOL_NAMES:
        spy.which(name)
    monkeypatch.setattr("fspack.packaging.loader.shutil.which", spy.which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", spy.run)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache-root"))
    return spy


def test_loader_tool_calls_do_not_scale_with_entry_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 多入口共享一个 C 对象，资源与链接按入口增长，探测数与入口数无关."""
    spy = _fake_toolchain(tmp_path, monkeypatch)
    probe_baseline: int | None = None
    for count in (1, 5, 20):
        root = tmp_path / f"project-{count}"
        ctx = _make_multi_entry_context(root, tuple(f"e{i}" for i in range(count)), target=_WINDOWS)
        monkeypatch.setenv("FSPACK_CACHE_DIR", str(root / "cache-root"))
        toolchain_module._fingerprints.clear()
        toolchain_module._which_results.clear()
        spy.reset()

        _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

        assert spy.count("object") == 1, "共享 C 源码只应编译一次"
        assert spy.count("resource") == count, "Windows 每个入口需独立资源"
        assert spy.count("link") == count, "Windows 每个入口需独立链接"
        probes = spy.count("probe")
        assert probes > 0
        if probe_baseline is None:
            probe_baseline = probes
        assert probes == probe_baseline, "工具链探测次数不应随入口数增长"


def test_loader_warm_build_invokes_no_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """完成一次构建后，新进程模型重建入口应全部命中三层缓存且不启动工具."""
    spy = _fake_toolchain(tmp_path, monkeypatch)
    count = 5
    ctx = _make_multi_entry_context(tmp_path / "project", tuple(f"e{i}" for i in range(count)), target=_WINDOWS)
    _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    # 模拟新进程：清空进程内记忆，仅保留磁盘工具链身份与产物缓存
    toolchain_module._fingerprints.clear()
    toolchain_module._which_results.clear()
    spy.reset()
    _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert spy.calls == [], "热构建不应启动探测/编译器/windres/链接器"
    record = ctx.tracker.records[-1]
    assert record.cache_hit == count
    assert record.items == count


def _counting_stat(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = Path.stat

    def counted(self: Path, *args: Any, **kwargs: Any) -> Any:
        calls.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counted)
    return calls


def test_toolchain_fingerprint_persistent_store_bounds_lookups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """持久化身份命中时只 stat 少量路径、一次 which，不重复启动工具."""
    spy = _fake_toolchain(tmp_path, monkeypatch)
    cache = tmp_path / "identity-cache"
    compiler = "x86_64-w64-mingw32-gcc"
    first = toolchain_module.toolchain_fingerprint(compiler, store=cache)
    assert spy.count("probe") > 0

    toolchain_module._fingerprints.clear()
    toolchain_module._which_results.clear()
    spy.reset()
    which_calls: list[str] = []
    original_which = spy.which

    def counted_which(name: str) -> str:
        which_calls.append(name)
        return original_which(name)

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", counted_which)
    stats = _counting_stat(monkeypatch)
    assert toolchain_module.toolchain_fingerprint(compiler, store=cache) == first
    assert spy.count("probe") == 0, "持久化身份命中不应重新探测"
    assert len(which_calls) <= 2, f"热查询 which 次数应有限，实际 {which_calls}"
    assert len(stats) <= 32, f"热查询 stat 次数应有限，实际 {len(stats)} 次"


def _mingw_ready() -> bool:
    return mingw_available() and bool(shutil.which(_find_windres()))


@pytest.fixture
def _require_mingw() -> None:
    if not _mingw_ready():
        pytest.skip("requires real MinGW gcc and windres")


class _RealToolSpy:
    """转发真实工具调用并记录命令类型，用于断言真实构建的复用边界."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._real_run = subprocess.run
        self._lock = Lock()

    def run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        with self._lock:
            self.calls.append(_ToolSpy.kind(cmd))
        return self._real_run(cmd, **kwargs)

    def reset(self) -> None:
        with self._lock:
            self.calls = []

    def count(self, kind: str) -> int:
        with self._lock:
            return self.calls.count(kind)


def _compile_real(
    source: str,
    cache: Path,
    out_dir: Path,
    count: int,
    spy: _RealToolSpy,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        compile_loader(
            source,
            out_dir / f"e{index}.exe",
            AppType.CLI,
            out_dir / f"w{index}",
            _WINDOWS,
            version_info=LoaderVersionInfo("bench", "1.0.0", "benchmark", "author", f"e{index}.exe"),
            cache_dir=cache,
            stage=StageRecorder("真实 loader 基线"),
        )


@pytest.fixture
def _real_spy(monkeypatch: pytest.MonkeyPatch) -> _RealToolSpy:
    spy = _RealToolSpy()
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", spy.run)
    return spy


@pytest.mark.slow
@pytest.mark.e2e
class TestLoaderBuildPerfBaseline:
    """真实 MinGW 冷/热构建基线；环境缺失时整类跳过."""

    @pytest.fixture(autouse=True)
    def _setup(self, _require_mingw: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(progress, "spinner", lambda *a, **kw: nullcontext())

    def test_loader_single_entry_cold_baseline(self, benchmark: Any, tmp_path: Path, _real_spy: _RealToolSpy) -> None:
        """单入口冷缓存基线：一个 C 对象、一个资源、一次链接."""
        source = generate_loader_source("python311", _WINDOWS)
        rounds = itertools.count()

        def setup() -> tuple[tuple[Any, ...], dict[str, Any]]:
            index = next(rounds)
            return (source, tmp_path / f"single-cache-{index}", tmp_path / f"single-out-{index}", 1, _real_spy), {}

        def run(source: str, cache: Path, out_dir: Path, count: int, spy: _RealToolSpy) -> None:
            spy.reset()
            _compile_real(source, cache, out_dir, count, spy)
            assert (spy.count("object"), spy.count("resource"), spy.count("link")) == (1, 1, 1)

        benchmark.pedantic(run, setup=setup, rounds=3, iterations=1)
        assert _real_spy.count("link") == 1

    def test_loader_multi_entry_cold_baseline(self, benchmark: Any, tmp_path: Path, _real_spy: _RealToolSpy) -> None:
        """5 入口冷缓存基线：C 对象仍只编译一次，资源与链接各 5 次."""
        source = generate_loader_source("python311", _WINDOWS)
        rounds = itertools.count()

        def setup() -> tuple[tuple[Any, ...], dict[str, Any]]:
            index = next(rounds)
            return (source, tmp_path / f"multi-cache-{index}", tmp_path / f"multi-out-{index}", 5, _real_spy), {}

        def run(source: str, cache: Path, out_dir: Path, count: int, spy: _RealToolSpy) -> None:
            spy.reset()
            _compile_real(source, cache, out_dir, count, spy)
            assert (spy.count("object"), spy.count("resource"), spy.count("link")) == (1, 5, 5)

        benchmark.pedantic(run, setup=setup, rounds=3, iterations=1)
        assert _real_spy.count("link") == 5

    def test_loader_multi_entry_warm_baseline(self, benchmark: Any, tmp_path: Path, _real_spy: _RealToolSpy) -> None:
        """5 入口热缓存基线：预热后每轮零工具调用，只做校验与复制."""
        source = generate_loader_source("python311", _WINDOWS)
        cache = tmp_path / "warm-cache"
        _compile_real(source, cache, tmp_path / "warm-seed", 5, _real_spy)
        rounds = itertools.count()

        def setup() -> tuple[tuple[Any, ...], dict[str, Any]]:
            return (source, cache, tmp_path / f"warm-out-{next(rounds)}", 5, _real_spy), {}

        def run(source: str, cache: Path, out_dir: Path, count: int, spy: _RealToolSpy) -> None:
            spy.reset()
            _compile_real(source, cache, out_dir, count, spy)
            assert spy.calls == []

        benchmark.pedantic(run, setup=setup, rounds=5, iterations=1)
        assert _real_spy.calls == []

    def test_loader_warm_build_stays_faster_than_cold(self, tmp_path: Path, _real_spy: _RealToolSpy) -> None:
        """保守比例门禁：5 入口热构建中位数必须显著低于冷构建."""
        source = generate_loader_source("python311", _WINDOWS)
        cold: list[float] = []
        warm: list[float] = []
        for trial in range(3):
            cache = tmp_path / f"ratio-cache-{trial}"
            start = time.perf_counter()
            _compile_real(source, cache, tmp_path / f"ratio-cold-{trial}", 5, _real_spy)
            cold.append(time.perf_counter() - start)
            start = time.perf_counter()
            _compile_real(source, cache, tmp_path / f"ratio-warm-{trial}", 5, _real_spy)
            warm.append(time.perf_counter() - start)

        cold_median = statistics.median(cold)
        warm_median = statistics.median(warm)
        assert cold_median > 0
        assert warm_median < cold_median * 0.8, (
            f"5 入口热构建 {warm_median:.4f}s 应显著快于冷构建 {cold_median:.4f}s，"
            "否则说明对象/资源/最终缓存或工具链身份复用退化"
        )
