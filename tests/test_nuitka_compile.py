"""``NuitkaCompile`` 编译测试：compile_src/compile_packages、损坏自愈、失败文件收集与 stamp 原子写入薄封装."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.nuitka import NuitkaCompiler
from fspack.platform import Platform
from fspack.progress import StageRecorder
from tests._stubs import (
    VerifyResultStub,
    make_nuitka_cache,
)

# ---- compile_src 测试 ----


def test_compile_src_skips_when_python_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """runtime python 未就绪时告警并跳过编译."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    cache = make_nuitka_cache(tmp_path / "cache")
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
    cache = make_nuitka_cache(tmp_path / "cache")

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
    cache = make_nuitka_cache(tmp_path / "cache")

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
        # --mode=module：4.x 中旧 --module 已废弃，模块模式专属选项检查只认 --mode=module，
        # 否则 --no-pyi-file 触发 "has no effect" WARNING
        assert "--mode=module" in cmd
        # --nofollow-imports：显式不跟随导入（模块模式默认行为），消除
        # "did not specify to follow or include anything" 警告
        assert "--nofollow-imports" in cmd
        assert "--module" not in cmd
        assert "--no-pyi-file" in cmd
        assert "--remove-output" in cmd
        # --assume-yes-for-downloads：Nuitka 4.x 内置 zig 作为可选 C 编译器，自动接受下载
        # 避免交互式询问阻塞构建（实际已通过 CC 环境变量指定 gcc/mingw 避免 zig，此为兜底）
        assert "--assume-yes-for-downloads" in cmd
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
    cache = make_nuitka_cache(tmp_path / "cache")

    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才删 .py）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
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
    cache = make_nuitka_cache(tmp_path / "cache")

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
    cache = make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # cmd 最后一个元素是 py_file 路径
        if "bad.py" in cmd[-1]:
            return (1, "", "syntax error")
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才删 .py）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
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


def test_compile_src_failure_cleans_build_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单文件编译失败时 _cleanup_build_dirs 仍清理 .build 残留（iter-130）.

    Nuitka --remove-output 仅在编译成功时清理 .build/，失败时残留。
    compile_src 在 finally 块调 _cleanup_build_dirs 确保失败时也清理。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1")
    cache = make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # 模拟编译失败：Nuitka 残留 .build 目录（--remove-output 仅成功时清理）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.build").mkdir(exist_ok=True)
        (py_file.parent / f"{py_file.stem}.build" / "module.c").write_text("// c")
        return (1, "", "compile error")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 编译失败但 .build 残留应被清理
    assert not (src / "app.build").exists(), "编译失败的 .build 残留应被 _cleanup_build_dirs 清理"
    # 失败的 .py 保留（运行时回退 .pyc）
    assert (src / "app.py").is_file()


def test_compile_src_compile_files_exception_cleans_build_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """worker OSError 按失败处理后，compile_src 仍清理 .build 残留（iter-130）.

    _stream_compile 抛 FileNotFoundError（py_exe 不存在）时 _compile_one 捕获
    OSError 按失败文件处理（不中断构建），compile_src 的 finally 调
    _cleanup_build_dirs 确保编译结束（含失败）后清理 .build 残留。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1")
    # 预存在的 .build 目录（模拟上次编译残留）
    build_dir = src / "app.build"
    build_dir.mkdir()
    (build_dir / "module.c").write_text("// c")
    cache = make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        raise FileNotFoundError("python exe not found")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    # OSError 按失败处理不抛异常，返回失败文件列表
    failed = NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert failed == ["app.py"]

    # .build 残留也应被清理（finally 块）
    assert not build_dir.exists(), ".build 残留也应被 finally 块清理"


def test_compile_src_linux_uses_python3_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 平台用 runtime/python/bin/python{ver} 调 nuitka."""
    runtime = tmp_path / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    (runtime / "python" / "bin" / "python3.11").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    cache = make_nuitka_cache(tmp_path / "cache")

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
    cache = make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才删 .py）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

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
    cache = make_nuitka_cache(tmp_path / "cache")

    captured_files: list[str] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # cmd 最后一个元素是 py_file 路径
        captured_files.append(cmd[-1])
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才删 .py）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
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
    cache = make_nuitka_cache(tmp_path / "cache")

    captured_files: list[str] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured_files.append(cmd[-1])
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才删 .py）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
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
    cache = make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才尝试 unlink）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

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


# ---- compile_src 损坏自愈测试（编译缓存污染 → 清缓存重试一轮） ----


def _setup_corrupt_retry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, py_count: int) -> Path:
    """搭 compile_src 自愈测试环境：runtime/python.exe + nuitka 缓存 + py 文件.

    返回 src 目录。mock 保留真实 `_collect_py_files`/`_create_bootstrap_script`/
    `_cleanup_build_dirs`（自愈轮重新收集依赖真实文件系统状态）。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    for i in range(py_count):
        (src / f"f{i}.py").write_text("x = 1")
    make_nuitka_cache(tmp_path / "cache")
    return src


def test_compile_src_corrupt_majority_purges_cache_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """产物异常 ≥3 且过半时清 Nuitka 编译缓存并重试一轮（编译缓存污染自愈）."""
    src = _setup_corrupt_retry_env(tmp_path, monkeypatch, py_count=4)

    compile_rounds: list[list[Path]] = []
    purge_calls: list[Path] = []

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], list[Path]]:
        compile_rounds.append(list(py_files))
        return (set(py_files), [])

    # 首轮仅 1 个剥离（4 个中 3 个异常 → 3≥3 且 3*2>=4 触发自愈），
    # 重试轮全部剥离（异常 0 → 循环终止）
    strip_returns: list[int] = [1, 4]
    strip_rounds: list[int] = []

    def fake_strip(cls: Any, compiled_files: set[Path], stage: Any, **kw: Any) -> int:
        n = strip_rounds[0] if strip_rounds else 0
        strip_rounds.append(n)
        return strip_returns[n] if n < len(strip_returns) else 0

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))
    monkeypatch.setattr(NuitkaCompiler, "_strip_compiled_sources", classmethod(fake_strip))
    monkeypatch.setattr(
        "fspack.packaging.nuitka.compile._purge_nuitka_compile_cache",
        lambda: purge_calls.append(Path("purged")),
    )

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, tmp_path / "runtime", "3.11.9", Platform.WINDOWS, tmp_path / "cache", stage=st)

    # 编译两轮（首轮 + 清缓存重试轮），缓存清理恰一次
    assert len(compile_rounds) == 2
    assert len(purge_calls) == 1
    # 首轮编译全部 4 个文件；重试轮重新收集仍 4 个（mock 不删 .py）
    assert len(compile_rounds[0]) == 4
    assert len(compile_rounds[1]) == 4
    assert any("清理 Nuitka 编译缓存" in r.message or "疑为编译缓存污染" in r.message for r in caplog.records)


def test_compile_src_low_corruption_no_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """产物异常数 <3（单文件边界问题）时不触发清缓存重试."""
    src = _setup_corrupt_retry_env(tmp_path, monkeypatch, py_count=4)

    compile_rounds: list[list[Path]] = []

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], list[Path]]:
        compile_rounds.append(list(py_files))
        return (set(py_files), [])

    # 4 个中 2 个异常：2 < 3 不触发
    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))
    monkeypatch.setattr(NuitkaCompiler, "_strip_compiled_sources", classmethod(lambda cls, cf, st, **kw: 2))
    monkeypatch.setattr(
        "fspack.packaging.nuitka.compile._purge_nuitka_compile_cache",
        lambda: (_ for _ in ()).throw(AssertionError("低损坏率不应清缓存")),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, tmp_path / "runtime", "3.11.9", Platform.WINDOWS, tmp_path / "cache", stage=st)
    assert len(compile_rounds) == 1


def test_compile_src_corrupt_minority_ratio_no_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """异常数 ≥3 但未过半（非缓存级系统性损坏）时不触发重试."""
    src = _setup_corrupt_retry_env(tmp_path, monkeypatch, py_count=7)

    compile_rounds: list[list[Path]] = []

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], list[Path]]:
        compile_rounds.append(list(py_files))
        return (set(py_files), [])

    # 7 个中 3 个异常：3 ≥3 但 3*2=6 < 7 未过半，不触发
    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))
    monkeypatch.setattr(NuitkaCompiler, "_strip_compiled_sources", classmethod(lambda cls, cf, st, **kw: 4))
    monkeypatch.setattr(
        "fspack.packaging.nuitka.compile._purge_nuitka_compile_cache",
        lambda: (_ for _ in ()).throw(AssertionError("未过半不应清缓存")),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, tmp_path / "runtime", "3.11.9", Platform.WINDOWS, tmp_path / "cache", stage=st)
    assert len(compile_rounds) == 1


def test_compile_src_corrupt_retry_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重试轮仍高损坏率时不再清缓存重试（防死循环，仅重试一轮）."""
    src = _setup_corrupt_retry_env(tmp_path, monkeypatch, py_count=4)

    compile_rounds: list[list[Path]] = []
    purge_calls: list[int] = []

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], list[Path]]:
        compile_rounds.append(list(py_files))
        return (set(py_files), [])

    # 两轮均 0 剥离（异常 4/4 过半）：首轮触发自愈，重试轮不再触发
    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))
    monkeypatch.setattr(NuitkaCompiler, "_strip_compiled_sources", classmethod(lambda cls, cf, st, **kw: 0))
    monkeypatch.setattr(
        "fspack.packaging.nuitka.compile._purge_nuitka_compile_cache",
        lambda: purge_calls.append(1),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, tmp_path / "runtime", "3.11.9", Platform.WINDOWS, tmp_path / "cache", stage=st)
    # 恰两轮编译、恰一次清缓存（重试轮不再清）
    assert len(compile_rounds) == 2
    assert len(purge_calls) == 1


def test_purge_nuitka_compile_cache_isolates_winlibs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_purge_nuitka_compile_cache 删 nuitka-work 但不动 winlibs 工具链目录."""
    from fspack.packaging.nuitka.compile import _purge_nuitka_compile_cache

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    work_dir = tmp_path / "cache" / "nuitka-work"
    (work_dir / "clcache" / "sub").mkdir(parents=True)
    (work_dir / "clcache" / "sub" / "entry.txt").write_text("stale")
    winlibs_gcc = tmp_path / "cache" / "nuitka-winlibs-mingw" / "gcc" / "x86_64" / "rel" / "mingw64" / "bin"
    winlibs_gcc.mkdir(parents=True)
    (winlibs_gcc / "gcc.exe").write_bytes(b"")

    _purge_nuitka_compile_cache()

    assert not work_dir.exists(), "nuitka-work 编译缓存应被清空"
    assert (winlibs_gcc / "gcc.exe").is_file(), "winlibs 工具链目录不应被清理（避免重下 200MB）"


def test_purge_nuitka_compile_cache_missing_dir_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_purge_nuitka_compile_cache 在目录不存在时静默 no-op（不抛异常）."""
    from fspack.packaging.nuitka.compile import _purge_nuitka_compile_cache

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    # 不创建 nuitka-work 目录
    _purge_nuitka_compile_cache()  # 不抛即通过
    assert not (tmp_path / "cache" / "nuitka-work").exists()


# ---- compile_packages 边缘场景测试 ----


def test_compile_packages_skips_when_py_exe_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_compile_python 返回 None 时 compile_packages 直接返回."""
    from fspack.packaging.nuitka import NuitkaCompiler

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    sp.mkdir()
    (sp / "rich").mkdir()
    (sp / "rich" / "__init__.py").write_text("")

    monkeypatch.setattr(NuitkaCompiler, "_resolve_compile_python", lambda *a, **kw: None)

    st = StageRecorder("Nuitka 包编译")
    # 不应抛异常，不应调用 _compile_files
    NuitkaCompiler.compile_packages(sp, ("rich",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)


def test_compile_packages_skips_when_nuitka_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_is_nuitka_cached 返回 False 时 compile_packages 跳过编译."""
    from fspack.packaging.nuitka import NuitkaCompiler

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    # 缓存目录无 nuitka 包
    cache = tmp_path / "cache"
    cache.mkdir()
    sp = tmp_path / "site-packages"
    sp.mkdir()
    (sp / "rich").mkdir()
    (sp / "rich" / "__init__.py").write_text("")

    st = StageRecorder("Nuitka 包编译")
    # 不应抛异常，不应调用 _compile_files
    NuitkaCompiler.compile_packages(sp, ("rich",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)


def test_compile_packages_warns_when_failed_gt_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """compile_packages 编译有失败时 warning 记录失败数."""
    from fspack.packaging.nuitka import NuitkaCompiler

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    pkg = sp / "rich"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("x = 1")

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], list[Path]]:
        # 返回 1 个失败
        return (set(), [Path("fake.py")])

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))

    st = StageRecorder("Nuitka 包编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_packages(sp, ("rich",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert any("失败 1 个" in r.message for r in caplog.records)


def test_compile_packages_mixed_existing_and_missing_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """compile_packages 同时传入存在与不存在的包：不存在包跳过，存在包正常编译."""
    from fspack.packaging.nuitka import NuitkaCompiler

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    pkg = sp / "rich"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("x = 1")

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], int]:
        for py in py_files:
            (py.parent / f"{py.stem}.cp311-win_amd64.pyd").write_bytes(b"fake")
        return (set(py_files), 0)

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: VerifyResultStub({"rich.mod": True}),
    )

    st = StageRecorder("Nuitka 包编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        # rich 存在，nonexistent 不存在
        NuitkaCompiler.compile_packages(
            sp, ("rich", "nonexistent"), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st
        )
    # nonexistent 包警告
    assert any("未找到包目录" in r.message for r in caplog.records)
    # rich 包正常编译
    assert not (pkg / "mod.py").exists()
    assert (pkg / "mod.cp311-win_amd64.pyd").is_file()


def test_compile_packages_with_ccache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_packages 传 ccache=True + cache_root 时调用 _ensure_ccache."""
    from fspack.packaging.nuitka import NuitkaCompiler

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    pkg = sp / "rich"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("x = 1")

    ccache_called: dict[str, bool] = {}

    def fake_ensure_ccache(cache_root: Path, target: Platform, stage: Any) -> Path:
        ccache_called["yes"] = True
        return Path("/usr/bin/ccache")

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], int]:
        for py in py_files:
            (py.parent / f"{py.stem}.cp311-win_amd64.pyd").write_bytes(b"fake")
        return (set(py_files), 0)

    monkeypatch.setattr(
        NuitkaCompiler, "_ensure_ccache", classmethod(lambda cls, *a, **kw: fake_ensure_ccache(*a, **kw))
    )
    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: VerifyResultStub({"rich.mod": True}),
    )

    st = StageRecorder("Nuitka 包编译")
    NuitkaCompiler.compile_packages(
        sp,
        ("rich",),
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        cache,
        stage=st,
        ccache=True,
        cache_root=tmp_path / "ccache_root",
    )
    assert ccache_called.get("yes") is True


# ---- compile_packages 测试 ----


def test_compile_packages_empty_packages_noop(tmp_path: Path) -> None:
    """packages 为空时 compile_packages 直接返回，不调用任何编译逻辑."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = make_nuitka_cache(tmp_path / "cache")
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
    cache = make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    sp.mkdir()
    # nonexistent_pkg 不存在
    st = StageRecorder("Nuitka 包编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_packages(sp, ("nonexistent_pkg",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert any("未找到包目录" in r.message for r in caplog.records)


def test_compile_packages_compiles_specified_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_packages 编译指定包下的 .py 文件（跳过 __init__.py）.

    fake_compile_files 同时创建 .pyd 产物，验证 _strip_compiled_sources 删除 .py 前检查 .pyd 存在。
    新增 import 验证：compile_packages 用 runtime python 批量验证 .pyd 可加载才删除 .py，
    mock subprocess.run 返回所有模块可加载（fake_pyd 是占位字节，无法真实 import）。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = make_nuitka_cache(tmp_path / "cache")
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
        # 模拟 Nuitka --module 生成 .pyd 产物（{stem}.cp{ver}-{platform}.pyd）
        for py in py_files:
            (py.parent / f"{py.stem}.cp311-win_amd64.pyd").write_bytes(b"fake-pyd")
        return (set(py_files), 0)

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))

    # mock 批量验证：返回所有模块可加载（fake_pyd 是占位字节，无法真实 import）
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: VerifyResultStub({"rich._extension": True, "rich.console": True}),
    )

    st = StageRecorder("Nuitka 包编译")
    NuitkaCompiler.compile_packages(sp, ("rich",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 收集了 _extension.py 与 console.py（跳过 __init__.py）
    assert len(captured) == 1
    names = {p.name for p in captured[0]}
    assert names == {"_extension.py", "console.py"}
    # 编译成功的 .py 被删除（.pyd 已生成且验证可加载）
    assert not (pkg / "_extension.py").exists()
    assert not (pkg / "console.py").exists()
    # .pyd 产物保留
    assert (pkg / "_extension.cp311-win_amd64.pyd").is_file()
    assert (pkg / "console.cp311-win_amd64.pyd").is_file()
    # __init__.py 保留
    assert (pkg / "__init__.py").is_file()


def test_collect_py_files_skips_failed_files(tmp_path: Path) -> None:
    """_collect_py_files 带 skip_files 跳过上次失败的文件（iter-137）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    src = tmp_path / "src"
    src.mkdir()
    (src / "good.py").write_text("x = 1")
    (src / "bad.py").write_text("y = 2")
    (src / "sub").mkdir()
    (src / "sub" / "nested.py").write_text("z = 3")

    # skip_files 用相对 src_dir 的 POSIX 路径
    skip = frozenset({"bad.py", "sub/nested.py"})
    collected = NuitkaCompiler._collect_py_files(src, entry_rels=None, skip_files=skip)
    collected_names = {p.relative_to(src).as_posix() for p in collected}
    assert collected_names == {"good.py"}


def test_collect_py_files_skip_files_none_preserves_all(tmp_path: Path) -> None:
    """skip_files=None 时不跳过任何文件（向后兼容）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("")
    (src / "b.py").write_text("")

    collected = NuitkaCompiler._collect_py_files(src, entry_rels=None, skip_files=None)
    assert len(collected) == 2


def test_collect_py_files_skips_data_dirs(tmp_path: Path) -> None:
    """_collect_py_files 跳过 data_dirs 目录树内的 .py（数据资源不编译）.

    fspack 自构建场景：assets/templates/ 含完整示例项目模板，逐一 Nuitka
    编译既拖慢构建也无运行收益（模板 .py 是数据资源，原样保留供下游使用）。
    """
    from fspack.packaging.nuitka import NuitkaCompiler

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1")
    demo = src / "assets" / "templates" / "demo"
    demo.mkdir(parents=True)
    (demo / "main.py").write_text("x = 2")
    (demo / "sub").mkdir()
    (demo / "sub" / "mod.py").write_text("x = 3")

    collected = NuitkaCompiler._collect_py_files(src, entry_rels=None, data_dirs=(src / "assets" / "templates",))
    assert [p.relative_to(src).as_posix() for p in collected] == ["app.py"]


def test_collect_py_files_data_dirs_empty_compiles_all(tmp_path: Path) -> None:
    """data_dirs=() 时不排除任何文件（默认行为，向后兼容）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("")
    (src / "tpl").mkdir()
    (src / "tpl" / "demo.py").write_text("")

    collected = NuitkaCompiler._collect_py_files(src, entry_rels=None, data_dirs=())
    assert len(collected) == 2


# ---- _atomic_write_text 原子化写入测试（iter-128） ----


def test_atomic_write_text_creates_file_with_content(tmp_path: Path) -> None:
    """``_atomic_write_text`` 成功写入创建目标文件且内容正确."""
    from fspack.packaging.nuitka.compile import _atomic_write_text

    target = tmp_path / "stamp.txt"
    _atomic_write_text(target, "hello-stamp\n")
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "hello-stamp\n"


def test_atomic_write_text_overwrites_existing(tmp_path: Path) -> None:
    """``_atomic_write_text`` 覆盖已有文件且内容完整替换（无残留旧内容）."""
    from fspack.packaging.nuitka.compile import _atomic_write_text

    target = tmp_path / "stamp.txt"
    target.write_text("old-content", encoding="utf-8")
    _atomic_write_text(target, "new-content-longer")
    assert target.read_text(encoding="utf-8") == "new-content-longer"


def test_atomic_write_text_no_tmp_residue(tmp_path: Path) -> None:
    """``_atomic_write_text`` 成功后不残留 ``.tmp_`` 临时文件."""
    from fspack.packaging.nuitka.compile import _atomic_write_text

    target = tmp_path / "stamp.txt"
    _atomic_write_text(target, "x")
    residues = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp_")]
    assert residues == []


def test_atomic_write_text_replace_failure_cleans_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``Path.replace`` 失败时清理临时文件并重抛 OSError，目标文件保持原样."""
    from fspack.packaging.nuitka import compile as nuitka_compile

    target = tmp_path / "stamp.txt"
    target.write_text("original", encoding="utf-8")

    orig_replace = Path.replace

    def fail_replace(self: Path, dst: Path, *args: Any, **kwargs: Any) -> Path:
        raise OSError("cross-device link not permitted")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="cross-device"):
        nuitka_compile._atomic_write_text(target, "new-content")

    # 临时文件被清理
    residues = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp_")]
    assert residues == []
    # 目标文件保持原内容（未被替换）
    assert target.read_text(encoding="utf-8") == "original"
    # 确认 Path.replace 被调用过（restore 后可正常使用）
    monkeypatch.setattr(Path, "replace", orig_replace)


def test_atomic_write_text_creates_parent_dir(tmp_path: Path) -> None:
    """``_atomic_write_text`` 自动创建父目录（与原 ``stamp.parent.mkdir`` 行为一致）."""
    from fspack.packaging.nuitka.compile import _atomic_write_text

    target = tmp_path / "nested" / "deep" / "stamp.txt"
    _atomic_write_text(target, "key")
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "key"
