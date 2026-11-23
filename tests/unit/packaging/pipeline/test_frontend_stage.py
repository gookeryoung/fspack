"""``pipeline/frontend_stage.py`` 前端构建测试：detect/build、包管理器解析、_run_cmd 与裁剪映射."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from fspack.exceptions import FspackError
from fspack.packaging.pipeline.frontend_stage import (
    _build_frontend,
    _detect_frontends,
    _frontend_prune_map,
    _is_wsl_windows_mount,
    _run_cmd,
)
from tests._stubs import write_frontend_pkg

# ---- 前端构建阶段（fsp b 自动识别 web 结构） ----


def test_detect_frontends_configured_walk_up(tmp_path: Path) -> None:
    """web-static-dirs 配置的产物目录向上定位最近 package.json（flask/fastapi 布局）."""
    fe = write_frontend_pkg(tmp_path / "frontend")
    fps = _detect_frontends(tmp_path, ("frontend/deploy",))
    assert len(fps) == 1
    assert fps[0].root == fe.resolve()
    assert fps[0].output_dirs == ((tmp_path / "frontend" / "deploy").resolve(),)


def test_detect_frontends_auto_scan_nested(tmp_path: Path) -> None:
    """未配置项目结构扫描：src/<pkg>/frontend 命中，node_modules 与超深目录剪枝."""
    fe = write_frontend_pkg(tmp_path / "src" / "webview_app" / "frontend")
    # node_modules 内的 package.json 不触发识别
    nm_pkg = fe / "node_modules" / "left-pad"
    nm_pkg.mkdir(parents=True)
    (nm_pkg / "package.json").write_text('{"scripts": {"build": "x"}}', encoding="utf-8")
    # 超过扫描深度的目录不触发识别
    write_frontend_pkg(tmp_path / "a" / "b" / "c" / "d" / "frontend")

    fps = _detect_frontends(tmp_path, ())
    assert [fp.root for fp in fps] == [fe]
    assert fps[0].output_dirs == (fe / "deploy", fe / "dist")


def test_detect_frontends_pure_static_dir_not_detected(tmp_path: Path) -> None:
    """纯手写 html 的最小模板（无 package.json/build 脚本）不识别、不构建."""
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "index.html").write_text("<html/>", encoding="utf-8")
    # 配置路径：向上找不到 package.json；扫描路径：无 build 脚本
    assert _detect_frontends(tmp_path, ("frontend",)) == []
    write_frontend_pkg(tmp_path / "other" / "fe", build=False)
    assert _detect_frontends(tmp_path, ()) == []


def test_detect_frontends_configured_preferred_over_auto(tmp_path: Path) -> None:
    """同根目录命中两条路径时按根目录去重，保留配置来源（产物目录精确）."""
    write_frontend_pkg(tmp_path / "frontend")
    fps = _detect_frontends(tmp_path, ("frontend/deploy",))
    assert len(fps) == 1
    assert fps[0].output_dirs == ((tmp_path / "frontend" / "deploy").resolve(),)


def test_build_frontend_skips_when_output_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """产物目录非空时跳过构建（增量语义，不执行任何命令）."""
    fe = write_frontend_pkg(tmp_path / "frontend")
    deploy = fe / "deploy"
    deploy.mkdir()
    (deploy / "index.html").write_text("<html/>", encoding="utf-8")

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._run_cmd", lambda *a: calls.append(a))
    detail = _build_frontend(_detect_frontends(tmp_path, ()))
    assert calls == []
    assert "跳过" in detail


def test_build_frontend_installs_and_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """产物缺失时先 install（node_modules 不存在）再 build，产物就绪."""
    fe = write_frontend_pkg(tmp_path / "frontend")
    calls: list[tuple[str, ...]] = []

    def fake_run_cmd(exe: str, args: Sequence[str], cwd: Path) -> None:
        calls.append(tuple(args))
        if list(args) == ["run", "build"]:
            deploy = fe / "deploy"
            deploy.mkdir(parents=True, exist_ok=True)
            (deploy / "index.html").write_text("<html/>", encoding="utf-8")

    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._run_cmd", fake_run_cmd)
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._resolve_pm", lambda: ("pnpm", "C:/fake/pnpm.cmd"))
    detail = _build_frontend(_detect_frontends(tmp_path, ()))
    assert ("install",) in calls
    assert ("run", "build") in calls
    assert "pnpm" in detail and "frontend" in detail


def test_build_frontend_existing_node_modules_skips_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """node_modules 已存在时直接 build（不重复 install）."""
    fe = write_frontend_pkg(tmp_path / "frontend")
    (fe / "node_modules").mkdir()
    calls: list[tuple[str, ...]] = []

    def fake_run_cmd(exe: str, args: Sequence[str], cwd: Path) -> None:
        calls.append(tuple(args))
        if list(args) == ["run", "build"]:
            (fe / "dist").mkdir()
            (fe / "dist" / "index.html").write_text("<html/>", encoding="utf-8")

    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._run_cmd", fake_run_cmd)
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._resolve_pm", lambda: ("npm", "npm"))
    _build_frontend(_detect_frontends(tmp_path, ()))
    assert ("install",) not in calls
    assert calls == [("run", "build")]


def test_build_frontend_no_pm_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """产物缺失且无包管理器时报错并给出指引."""
    write_frontend_pkg(tmp_path / "frontend")
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._resolve_pm", lambda: None)
    with pytest.raises(FspackError, match="Node"):
        _build_frontend(_detect_frontends(tmp_path, ()))


def test_build_frontend_empty_output_after_build_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构建命令成功但产物目录仍为空时报错（fail-fast，防打包出坏应用）."""
    write_frontend_pkg(tmp_path / "frontend")
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._run_cmd", lambda *a: None)
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._resolve_pm", lambda: ("npm", "npm"))
    with pytest.raises(FspackError, match="产物目录仍为空"):
        _build_frontend(_detect_frontends(tmp_path, ()))


def test_is_wsl_windows_mount() -> None:
    """``/mnt/<盘符>/...`` 命中 WSL Windows 挂载，其余路径不命中."""
    assert _is_wsl_windows_mount("/mnt/c/Users/foo/nodejs/pnpm")
    assert _is_wsl_windows_mount("/mnt/d/env/node/pnpm.cmd")
    # 非 Windows 盘符挂载：多字母卷名、普通 Linux 路径、相对路径均不命中
    assert not _is_wsl_windows_mount("/mnt/usb/bin/pnpm")
    assert not _is_wsl_windows_mount("/usr/bin/pnpm")
    assert not _is_wsl_windows_mount("C:/fake/pnpm.cmd")
    assert not _is_wsl_windows_mount("pnpm")


def test_resolve_pm_skips_wsl_windows_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """``which`` 命中 WSL Windows 盘符路径时跳过，改用后续 Linux 候选."""
    from fspack.packaging.pipeline import frontend_stage

    def fake_which(name: str) -> str | None:
        return {
            "pnpm": "/mnt/c/Users/foo/AppData/pnpm",
            "npm": "/usr/bin/npm",
        }.get(name)

    monkeypatch.setattr(frontend_stage.shutil, "which", fake_which)
    assert frontend_stage._resolve_pm() == ("npm", "/usr/bin/npm")


def test_resolve_pm_all_wsl_mounts_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有候选均只存在于 Windows 盘符挂载时返回 None（明确的未找到报错）."""
    from fspack.packaging.pipeline import frontend_stage

    monkeypatch.setattr(frontend_stage.shutil, "which", lambda name: f"/mnt/c/tools/{name}")
    assert frontend_stage._resolve_pm() is None


class _FakePipeStream:
    """os.pipe 读端包装：drain 线程经 ``fileno`` + ``os.read`` 消费，可预置内容."""

    def __init__(self, content: bytes = b"") -> None:
        self._r, w = os.pipe()
        if content:
            os.write(w, content)
        os.close(w)

    def fileno(self) -> int:
        return self._r


class _FakeProc:
    """``_run_cmd`` 的 Popen 替身：管道流 + 可编程 wait/kill 行为."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stderr: bytes = b"",
        stdout: bytes = b"",
        first_wait_exc: Exception | None = None,
    ) -> None:
        self.pid = 4242
        self.stdout = _FakePipeStream(stdout)
        self.stderr = _FakePipeStream(stderr)
        self._returncode = returncode
        self._first_wait_exc = first_wait_exc
        self.kill_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        if self._first_wait_exc is not None:
            exc, self._first_wait_exc = self._first_wait_exc, None
            raise exc
        return self._returncode

    def kill(self) -> None:
        self.kill_calls += 1


def test_run_cmd_success_passes_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """退出码 0 时正常返回（无异常），命令与工作目录透传 Popen."""
    seen: dict[str, object] = {}

    def fake_popen(cmd: list[str], cwd: str, **kwargs: object) -> _FakeProc:
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        return _FakeProc()

    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage.subprocess.Popen", fake_popen)
    _run_cmd("C:/fake/npm", ["run", "build"], tmp_path)
    assert seen["cmd"] == ["C:/fake/npm", "run", "build"]
    assert seen["cwd"] == str(tmp_path)


def test_run_cmd_failure_raises_with_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非零退出码抛 FspackError，含 stderr 尾部（截断到 800 字符）."""
    proc = _FakeProc(returncode=1, stderr=b"E" * 1000)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.frontend_stage.subprocess.Popen",
        lambda *a, **k: proc,
    )
    with pytest.raises(FspackError, match="前端命令失败") as exc_info:
        _run_cmd("npm", ["run", "build"], tmp_path)
    assert "E" * 800 in str(exc_info.value)
    assert "E" * 801 not in str(exc_info.value)


def test_run_cmd_timeout_kills_process_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时抛 FspackError 并终止进程树：Windows taskkill /T /F，POSIX kill."""
    import sys as _sys

    proc = _FakeProc(
        returncode=1,
        first_wait_exc=subprocess.TimeoutExpired(cmd=["fake"], timeout=600),
    )
    kill_cmds: list[list[str]] = []
    monkeypatch.setattr(
        "fspack.packaging.pipeline.frontend_stage.subprocess.Popen",
        lambda *a, **k: proc,
    )
    monkeypatch.setattr(
        "fspack.packaging.pipeline.frontend_stage.subprocess.run",
        lambda cmd, **k: kill_cmds.append(list(cmd)),
    )
    with pytest.raises(FspackError, match="前端命令超时"):
        _run_cmd("npm", ["run", "build"], tmp_path)
    if _sys.platform == "win32":
        assert kill_cmds and kill_cmds[0][:1] == ["taskkill"]
        assert "/T" in kill_cmds[0] and "/F" in kill_cmds[0]
    else:
        assert proc.kill_calls == 1


# ---- copy_source 前端裁剪（dist 只发布产物，前端源码不进入） ----


def test_frontend_prune_map_assembly(tmp_path: Path) -> None:
    """_frontend_prune_map 组装：FrontendProject 集 → root 到产物映射."""
    fe = write_frontend_pkg(tmp_path / "frontend")
    fps = _detect_frontends(tmp_path, ())
    assert _frontend_prune_map(fps) == {fe.resolve(): (fe / "deploy", fe / "dist")}
