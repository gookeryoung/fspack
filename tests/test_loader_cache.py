from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.loader.cache import cache_lock, cached_copy, publish_cache


def test_cache_publication_preserves_content_and_mtime(tmp_path: Path) -> None:
    source = tmp_path / "source.o"
    source.write_bytes(b"compiled object")
    target = tmp_path / "cache" / "object.o"
    out = tmp_path / "restored.o"

    with cache_lock(target):
        publish_cache(source, target)
        assert cached_copy(target, out)

    assert out.read_bytes() == source.read_bytes()
    assert out.stat().st_mtime_ns == source.stat().st_mtime_ns
    assert (
        target.with_name(target.name + ".sha256").read_text(encoding="ascii").strip()
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path aliases")
def test_reentrant_cache_lock_normalizes_extended_path_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fspack.packaging.loader import cache

    target = tmp_path / "cache" / "object.o"
    spellings = iter((str(target), "\\\\?\\" + str(target)))
    monkeypatch.setattr(cache.os.path, "realpath", lambda _: next(spellings))
    monkeypatch.setattr(cache, "_LOCK_TIMEOUT", 0.1)
    with cache_lock(target), cache_lock(target):
        assert len(cache._lock_entries) == 1
    assert not cache._lock_entries


@pytest.mark.parametrize("damage", ["artifact", "digest", "missing_digest"])
def test_damaged_cache_does_not_overwrite_output(tmp_path: Path, damage: str) -> None:
    source = tmp_path / "source.o"
    source.write_bytes(b"compiled object")
    target = tmp_path / "cache" / "object.o"
    out = tmp_path / "restored.o"
    out.write_bytes(b"existing output")

    with cache_lock(target):
        publish_cache(source, target)
        digest = target.with_name(target.name + ".sha256")
        if damage == "artifact":
            target.write_bytes(b"damaged object")
        elif damage == "digest":
            digest.write_text("invalid digest", encoding="ascii")
        else:
            digest.unlink()
        assert not cached_copy(target, out)

    assert out.read_bytes() == b"existing output"


def test_cache_hit_output_failure_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.o"
    source.write_bytes(b"compiled object")
    target = tmp_path / "cache" / "object.o"
    with cache_lock(target):
        publish_cache(source, target)

    def deny_copy(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("output denied")

    monkeypatch.setattr("fspack.packaging.loader.cache.shutil.copy2", deny_copy)
    with cache_lock(target), pytest.raises(PermissionError, match="output denied"):
        cached_copy(target, tmp_path / "restored.o")


def test_cache_publication_failure_keeps_build_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "source.o"
    source.write_bytes(b"compiled object")
    target = tmp_path / "cache" / "object.o"

    def deny_copy(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("cache denied")

    monkeypatch.setattr("fspack.packaging.loader.cache.shutil.copy2", deny_copy)
    with cache_lock(target):
        publish_cache(source, target)

    assert source.read_bytes() == b"compiled object"
    assert not target.exists()
    assert not target.with_name(target.name + ".sha256").exists()
    assert "cache denied" in caplog.text
    assert not list(target.parent.glob("*.tmp"))


@pytest.mark.slow
def test_cache_first_miss_is_shared_across_processes(tmp_path: Path) -> None:
    source = tmp_path / "source.o"
    source.write_bytes(b"compiled object")
    target = tmp_path / "cache" / "object.o"
    events = tmp_path / "compilations.txt"
    script = """
import sys
from pathlib import Path
from fspack.packaging.loader.cache import cache_lock, cached_copy, publish_cache
source, target, events, output = map(Path, sys.argv[1:])
print("ready", flush=True)
with cache_lock(target):
    if not cached_copy(target, output):
        with events.open("a", encoding="ascii") as stream:
            stream.write("compile\\n")
        publish_cache(source, target)
        assert cached_copy(target, output)
"""
    env = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, env.get("PYTHONPATH", ""))))
    processes: list[subprocess.Popen[str]] = []
    try:
        with cache_lock(target):
            for index in range(4):
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            script,
                            str(source),
                            str(target),
                            str(events),
                            str(tmp_path / f"out{index}.o"),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env,
                    )
                )
            for process in processes:
                assert process.stdout is not None
                assert process.stdout.readline().strip() == "ready"
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    assert events.read_text(encoding="ascii").splitlines() == ["compile"]
    for index in range(4):
        assert (tmp_path / f"out{index}.o").read_bytes() == source.read_bytes()


def test_toolchain_identity_reuses_probes_and_detects_binary_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fspack.packaging.loader.toolchain import toolchain_fingerprint

    compiler = tmp_path / "compiler.exe"
    compiler.write_bytes(b"compiler")
    calls: list[list[str]] = []

    def fake_probe(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "fake toolchain", "")

    monkeypatch.setattr("fspack.packaging.loader.toolchain.subprocess.run", fake_probe)
    first = toolchain_fingerprint(str(compiler))
    probe_count = len(calls)
    assert probe_count > 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(lambda _: toolchain_fingerprint(str(compiler)), range(12)))
    assert identities == [first] * 12
    assert len(calls) == probe_count

    compiler.write_bytes(b"updated compiler")
    assert toolchain_fingerprint(str(compiler)) != first


def test_toolchain_identity_detects_header_search_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fspack.packaging.loader.toolchain import toolchain_fingerprint

    compiler = tmp_path / "compiler.exe"
    compiler.write_bytes(b"compiler")

    def fake_probe(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, "fake toolchain", "")

    monkeypatch.setattr("fspack.packaging.loader.toolchain.subprocess.run", fake_probe)
    monkeypatch.setenv("CPATH", str(tmp_path / "headers-before"))
    first = toolchain_fingerprint(str(compiler))
    monkeypatch.setenv("CPATH", str(tmp_path / "headers-after"))
    assert toolchain_fingerprint(str(compiler)) != first


@pytest.mark.parametrize("component", ["ld", "cc1", "as"])
def test_toolchain_identity_detects_component_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    from fspack.packaging.loader.toolchain import toolchain_fingerprint

    compiler = tmp_path / "compiler.exe"
    compiler.write_bytes(b"compiler")
    tool = tmp_path / f"{component}.exe"
    tool.write_bytes(b"component")

    def fake_probe(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output = str(tool) if cmd[1] == f"-print-prog-name={component}" else ""
        return subprocess.CompletedProcess(cmd, 0, output, "")

    monkeypatch.setattr("fspack.packaging.loader.toolchain.subprocess.run", fake_probe)
    first = toolchain_fingerprint(str(compiler))
    assert toolchain_fingerprint(str(compiler)) == first
    tool.write_bytes(b"updated component")
    assert toolchain_fingerprint(str(compiler)) != first


@pytest.mark.slow
@pytest.mark.e2e
@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows loader execution")
def test_windows_loader_cached_entries_execute_after_relocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pefile

    from fspack.config import AppType
    from fspack.packaging.loader import LoaderVersionInfo, compile_loader, generate_loader_source, mingw_available
    from fspack.packaging.loader.toolchain import _find_windres
    from fspack.platform import Platform

    if not mingw_available() or not shutil.which(_find_windres()):
        pytest.skip("requires MinGW gcc and windres")
    base = Path(sys.base_prefix)
    py_xy = f"python{sys.version_info.major}{sys.version_info.minor}"
    if not (base / f"{py_xy}.dll").is_file():
        pytest.skip("requires a local CPython DLL")
    original = tmp_path / "original"
    runtime = original / "runtime"
    runtime.mkdir(parents=True)
    for pattern in ("python*.dll", "vcruntime*.dll"):
        for library in base.glob(pattern):
            shutil.copy2(library, runtime / library.name)
    (runtime / f"{py_xy}._pth").write_text(f"{base / 'Lib'}\n{base / 'DLLs'}\n..\n", encoding="utf-8")
    source = generate_loader_source(py_xy, platform=Platform.WINDOWS)
    cache = tmp_path / "cache"
    entries = (("cli", AppType.CLI), ("gui", AppType.GUI), ("web", AppType.WEB))
    for name, app_type in entries:
        script_name = f"入口_{name}.py"
        (original / script_name).write_text(
            "import json, sys\nfrom pathlib import Path\n"
            f"Path(sys.argv[1]).write_text(json.dumps({{'entry': {name!r}, 'args': sys.argv[2:]}}, "
            "ensure_ascii=False), encoding='utf-8')\n"
            f"raise SystemExit({7 if app_type is AppType.CLI else 0})\n",
            encoding="utf-8",
        )
        (original / f"{name}.entry").write_text(script_name, encoding="utf-8")
        compile_loader(
            source,
            original / f"{name}.exe",
            app_type,
            tmp_path / f"work-{name}",
            Platform.WINDOWS,
            version_info=LoaderVersionInfo("smoke", "1.0.0", "smoke", "author", f"{name}.exe"),
            cache_dir=cache,
        )
        with pefile.PE(str(original / f"{name}.exe")) as parsed:
            pe: Any = parsed
            assert pe.OPTIONAL_HEADER.Subsystem == (3 if app_type is AppType.CLI else 2)
            strings = {
                key: value
                for group in pe.FileInfo
                for item in group
                for table in getattr(item, "StringTable", [])
                for key, value in table.entries.items()
            }
            assert strings[b"OriginalFilename"] == f"{name}.exe".encode("ascii")
            assert any(entry.id == 24 for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries)

    relocated = tmp_path / "迁移 directory"
    shutil.copytree(original, relocated)

    def deny_compile(cmd: list[str], **kwargs: Any) -> None:
        raise AssertionError(f"warm build invoked a tool: {cmd}")

    with monkeypatch.context() as patch:
        patch.setattr("fspack.packaging.loader.subprocess.run", deny_compile)
        for name, app_type in entries:
            compile_loader(
                source,
                relocated / f"{name}.exe",
                app_type,
                tmp_path / f"warm-{name}",
                Platform.WINDOWS,
                version_info=LoaderVersionInfo("smoke", "1.0.0", "smoke", "author", f"{name}.exe"),
                cache_dir=cache,
            )
            assert not (tmp_path / f"warm-{name}").exists()
            assert (relocated / f"{name}.exe").read_bytes() == (original / f"{name}.exe").read_bytes()

    for directory in (original, relocated):
        for name, app_type in entries:
            output = directory / f"result-{name}.json"
            result = subprocess.run(
                [str(directory / f"{name}.exe"), str(output), "中文参数", "with space"],
                cwd=tmp_path,
                capture_output=True,
                check=False,
                timeout=20,
            )
            assert result.returncode == (7 if app_type is AppType.CLI else 0), result.stderr
            assert json.loads(output.read_text(encoding="utf-8")) == {
                "entry": name,
                "args": ["中文参数", "with space"],
            }
