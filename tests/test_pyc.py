"""``fspack.packaging.pyc`` 预编译字节码测试：compileall 超时/失败不写 stamp."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.pyc import _precompile_pyc
from fspack.platform import Platform
from fspack.progress import StageRecorder
from tests._stubs import CompletedStub, fake_compileall_runner

# ---- _precompile_pyc compileall 超时防护测试（iter-127） ----


def test_precompile_pyc_timeout_skips_stamp_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """compileall 超时不写 stamp（下次重试），记录 warning 并 set_detail."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hi')", encoding="utf-8")

    # patch subprocess.run 抛 TimeoutExpired
    def raise_timeout(*args: Any, **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=args[0] if args else [], timeout=0.5)

    monkeypatch.setattr("subprocess.run", raise_timeout)

    from fspack.packaging.pyc import _COMPILEALL_TIMEOUT, _precompile_pyc

    st = StageRecorder("预编译字节码")
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.pyc"):
        _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # 不写 stamp（下次重试）
    stamp = dist / ".pyc_stamp"
    assert not stamp.is_file()
    # warning 日志
    timeout_logs = [r for r in caplog.records if "超时" in r.message]
    assert len(timeout_logs) == 1
    assert "compileall" in timeout_logs[0].message
    assert str(int(_COMPILEALL_TIMEOUT)) in timeout_logs[0].message


def test_precompile_pyc_timeout_constant_default() -> None:
    """``_COMPILEALL_TIMEOUT`` 默认 300s."""
    from fspack.packaging.pyc import _COMPILEALL_TIMEOUT

    assert _COMPILEALL_TIMEOUT == 300.0


def test_precompile_pyc_normal_no_timeout_writes_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常完成（无超时）的 compileall 仍写 stamp，验证超时分支不影响正常路径."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hi')", encoding="utf-8")

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())

    from fspack.packaging.pyc import _precompile_pyc

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # 正常路径写 stamp
    stamp = dist / ".pyc_stamp"
    assert stamp.is_file()


# ---- _precompile_pyc returncode != 0 不写 stamp 测试（iter-128） ----


def test_precompile_pyc_returncode_nonzero_skips_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """compileall 非零退出码时不写 stamp（与超时分支一致的"失败不缓存"策略）.

    iter-128 扩展 iter-127 的超时不写 stamp 策略到 returncode != 0 场景，
    避免失败的编译被 stamp 跳过导致用户长期运行未编译的 .py。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hi')", encoding="utf-8")

    class _CompileFail:
        returncode = 2
        stderr = "SyntaxError: invalid syntax"
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileFail())

    from fspack.packaging.pyc import _precompile_pyc

    st = StageRecorder("预编译字节码")
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.pyc"):
        _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    stamp = dist / ".pyc_stamp"
    assert not stamp.is_file()
    fail_logs = [r for r in caplog.records if "compileall 失败" in r.message]
    assert len(fail_logs) == 1
    assert "SyntaxError" in fail_logs[0].message


# ---- _precompile_pyc 测试 ----


def test_precompile_pyc_windows_calls_compileall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标用 runtime/python.exe 拆分两次调 compileall 分别编译 src 与 site-packages.

    src 与 site-packages 用 ``ThreadPoolExecutor`` 并行编译，完成顺序不保证，
    断言两个目录都出现且都使用 runtime python.exe。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or CompletedStub())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # 拆分为两次 compileall 调用：src 与 site-packages 分别编译
    # （src 用 optimize，site-packages 用 min(optimize,1) 保留 docstring）
    # 并行执行，完成顺序不保证，用集合断言两个目标都出现
    assert len(captured) == 2
    target_dirs = {cmd[3] for cmd in captured}
    assert str(dist / "src") in target_dirs
    assert str(tmp_path / "dist" / "site-packages") in target_dirs
    for cmd in captured:
        assert "compileall" in cmd
        assert str(runtime / "python.exe") in cmd[0]


def test_precompile_pyc_parallel_executes_in_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """双目录场景下两个 compileall 在不同线程并行执行（验证 ThreadPoolExecutor 启用）.

    使用 :class:`threading.Barrier` 强制两个 compileall 调用同时活跃：若两者
    在同一线程串行执行，Barrier 永远等不到 2 个 parties，超时抛
    :class:`BrokenBarrierError` 让测试失败。
    """
    import threading

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")
    (dist / "site-packages").mkdir(parents=True)
    (dist / "site-packages" / "pkg.py").write_text("x = 1")

    thread_ids: set[int] = set()
    # Barrier(2) 强制两个 compileall 同时活跃：只有两个任务都在不同线程执行时才能通过
    barrier: threading.Barrier = threading.Barrier(2)

    def capture_thread(cmd: list[str], **kw: object) -> object:
        """记录调用线程 ID，等待另一个 compileall 也到达后返回."""
        thread_ids.add(threading.get_ident())
        barrier.wait(timeout=2.0)
        return CompletedStub()

    monkeypatch.setattr("subprocess.run", capture_thread)

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # 两个 compileall 在不同线程执行（ThreadPoolExecutor worker 线程）
    assert len(thread_ids) >= 2


def test_precompile_pyc_parallel_one_failure_skips_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """并行编译时任一目录失败则不写 stamp，且记录一条 compileall 失败 warning."""
    import logging

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")
    (dist / "site-packages").mkdir(parents=True)
    (dist / "site-packages" / "pkg.py").write_text("x = 1")

    class _CompileFail:
        returncode = 2
        stderr = "SyntaxError: invalid syntax"
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileFail())

    from fspack.packaging.pyc import _precompile_pyc

    st = StageRecorder("预编译字节码")
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.pyc.compile"):
        _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    stamp = dist / ".pyc_stamp"
    assert not stamp.is_file()
    fail_logs = [r for r in caplog.records if "compileall 失败" in r.message]
    # 至少一条失败日志（并行下两个都失败，as_completed 顺序不保证）
    assert len(fail_logs) >= 1
    assert "SyntaxError" in fail_logs[0].message


def test_precompile_pyc_cleans_data_dirs_pycache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_precompile_pyc`` 编译后删除 data_dirs 下的 __pycache__（数据资源不留 .pyc）.

    compileall 会为 data_dirs 内 .py 生成 __pycache__/*.pyc，但 data_dirs 视为完整
    资源原样保留，这些字节码是污染（尤其 fspack 模板目录内 $entry_module.py 编译出
    的 .pyc 会被 fsp init 模板加载器误读）。本测试模拟 compileall 已生成 __pycache__，
    验证 _precompile_pyc 调用后 data_dirs 下的 __pycache__ 被清理，src 其余 .pyc 保留。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "app.py").write_text("print('app')")
    # 模拟 data_dir：assets/init_templates 下含占位符 .py 的模板目录
    tpl_dir = src / "fspack" / "assets" / "init_templates" / "cli" / "helloworld"
    tpl_dir.mkdir(parents=True)
    (tpl_dir / "$entry_module.py").write_text("def main():\n    pass\n")
    # 模拟 compileall 已为 data_dir 与普通 src 生成 __pycache__/*.pyc
    tpl_pycache = tpl_dir / "__pycache__"
    tpl_pycache.mkdir()
    (tpl_pycache / "$entry_module.cpython-311.opt-2.pyc").write_bytes(b"\xa7\x00\x01")
    src_pycache = src / "__pycache__"
    src_pycache.mkdir()
    (src_pycache / "app.cpython-311.pyc").write_bytes(b"\x00\x01\x02")

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(
        dist,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        strip_py=False,
        stage=st,
        data_dirs=(tpl_dir.resolve(),),
    )

    # data_dir 下的 __pycache__ 被清理，占位符 .py 保留
    assert not tpl_pycache.exists()
    assert (tpl_dir / "$entry_module.py").is_file()
    # src 其余（非 data_dir）__pycache__ 不受影响
    assert src_pycache.is_dir()
    assert (src_pycache / "app.cpython-311.pyc").is_file()


def test_precompile_pyc_linux_uses_python3_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标用 runtime/python/bin/python{ver} 调 compileall."""
    runtime = tmp_path / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    (runtime / "python" / "bin" / "python3.11").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or CompletedStub())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.LINUX, strip_py=False, stage=st)

    # pyrefly: ignore [unnecessary-type-conversion]
    assert "python3.11" in str(captured[0][0])


def test_precompile_pyc_strip_deletes_non_init_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip_py=True 删除非 __init__.py 的 .py，保留 __init__.py 维持包结构.

    PEP 3147 迁移：删除 .py 前将 __pycache__/{stem}.cpython-{ver}.pyc 移到 {stem}.pyc。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('hi')")
    (src / "sub").mkdir()
    (src / "sub" / "__init__.py").write_text("")
    (src / "sub" / "mod.py").write_text("x")

    monkeypatch.setattr("subprocess.run", fake_compileall_runner)

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=True, stage=st)

    # __init__.py 保留（包标识）
    assert (src / "__init__.py").is_file()
    assert (src / "sub" / "__init__.py").is_file()
    # 非 __init__.py 被删
    assert not (src / "app.py").exists()
    assert not (src / "sub" / "mod.py").exists()
    # .pyc 已迁移到 legacy 布局
    assert (src / "app.pyc").is_file()
    assert (src / "sub" / "mod.pyc").is_file()


def test_precompile_pyc_strip_keeps_init_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip_py=True 时不删 __init__.py（避免 PEP 420 命名空间包导致 .pyc 不加载）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("PKG = 1")
    (src / "main.py").write_text("print('main')")

    monkeypatch.setattr("subprocess.run", fake_compileall_runner)

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=True, stage=st)

    assert (src / "__init__.py").is_file()
    assert not (src / "main.py").exists()
    # main.py 的 .pyc 已迁移到 legacy 布局
    assert (src / "main.pyc").is_file()


def test_precompile_pyc_strip_keeps_entry_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip_py=True 时保留 entry_rels 中的入口文件（runpy.run_module 需 .py 定位模块）.

    入口文件跳过 Nuitka 编译（保留 .py），若 pyc_strip 再删除入口 .py，
    ``runpy.run_module`` 会因 ``find_spec`` 找不到模块而 ``ImportError``：
    ``__pycache__`` 下的 ``.pyc`` 不在 ``FileFinder`` 搜索范围。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    # 模拟 fspack 包结构：src/fspack/__init__.py + cli.py（入口）+ utils.py（非入口）
    pkg = src / "fspack"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "cli.py").write_text("def main(): pass")  # 入口文件
    (pkg / "utils.py").write_text("x = 1")  # 非入口文件

    monkeypatch.setattr("subprocess.run", fake_compileall_runner)

    st = StageRecorder("预编译字节码")
    _precompile_pyc(
        dist,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        strip_py=True,
        stage=st,
        entry_rels=frozenset({"fspack/cli.py"}),
    )

    # __init__.py 保留（包标识）
    assert (pkg / "__init__.py").is_file()
    # 入口文件保留（runpy.run_module 需 .py 定位）
    assert (pkg / "cli.py").is_file()
    # 非入口 .py 被删除
    assert not (pkg / "utils.py").exists()
    # utils.py 的 .pyc 已迁移到 legacy 布局
    assert (pkg / "utils.pyc").is_file()


def test_strip_py_sources_skips_entry_rels(tmp_path: Path) -> None:
    """``_strip_py_sources`` 单元测试：entry_rels 中的文件跳过剥离.

    新增 PEP 3147 迁移：删除 .py 前需有对应 .pyc 才会剥离，否则保留 .py。
    """
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    pkg = src / "app"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text("m")  # 入口
    (pkg / "helper.py").write_text("h")  # 非入口
    # 为 helper.py 预生成 .pyc（模拟 compileall 输出），否则新逻辑保留 .py
    _make_pyc_file(pkg / "helper.py", "3.11", optimize=0)

    stripped = _strip_py_sources([src], frozenset({"app/main.py"}), optimize=0, py_version="3.11.9")

    assert stripped == 1  # 仅 helper.py 被删
    assert (pkg / "main.py").is_file()  # 入口保留
    assert not (pkg / "helper.py").exists()  # 非入口删除
    assert (pkg / "__init__.py").is_file()  # __init__.py 保留
    # .pyc 已迁移到 legacy 布局（helper.pyc）
    assert (pkg / "helper.pyc").is_file()


def _make_pyc_file(py_file: Path, py_version: str = "3.11", optimize: int = 0) -> Path:
    """生成 ``__pycache__/{stem}.cpython-{ver}{opt}.pyc`` 文件，返回路径.

    用 :func:`py_compile.compile` 生成真实的 .pyc 字节码（非空文件），
    供 ``_strip_py_sources`` 的 PEP 3147 迁移逻辑测试使用。
    """
    import py_compile

    major, minor = py_version.split(".")[:2]
    ver_tag = f"cpython-{major}{minor}"
    opt_suffix = "" if optimize == 0 else f".opt-{optimize}"
    pycache = py_file.parent / "__pycache__"
    pycache.mkdir(exist_ok=True)
    pyc_file = pycache / f"{py_file.stem}.{ver_tag}{opt_suffix}.pyc"
    py_compile.compile(str(py_file), cfile=str(pyc_file), optimize=optimize)
    return pyc_file


def test_strip_py_sources_migrates_pyc_to_legacy_layout(tmp_path: Path) -> None:
    """``_strip_py_sources`` 删除 .py 前将 __pycache__ 中的 .pyc 迁移到 legacy 布局.

    PEP 3147 规定 __pycache__ 中的 .pyc 仅在源码 .py 存在时才被加载，
    删除 .py 后必须迁移到 {stem}.pyc 才能被 SourcelessFileLoader 加载。
    """
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "mod.py").write_text("VALUE = 42")
    # 预生成 .pyc（optimize=2，对应 .opt-2.pyc）
    _make_pyc_file(src / "mod.py", "3.11", optimize=2)

    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=2)

    assert stripped == 1
    assert not (src / "mod.py").exists()  # .py 已删
    assert (src / "mod.pyc").is_file()  # .pyc 迁移到 legacy 布局
    # __pycache__ 中的 .pyc 已被移走
    pycache_dir = src / "__pycache__"
    pycache_files: list[Path] = list(pycache_dir.glob("mod.*.pyc")) if pycache_dir.exists() else []
    assert not pycache_files


def test_strip_py_sources_keeps_py_when_pyc_missing(tmp_path: Path) -> None:
    """``.pyc`` 不存在（编译失败）时保留 ``.py``，避免模块完全丢失."""
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "broken.py").write_text("syntax error!!!")
    # 不生成 .pyc（模拟 compileall 失败）

    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=0)

    assert stripped == 0  # 无 .pyc 不剥离
    assert (src / "broken.py").is_file()  # .py 保留


def test_strip_py_sources_optimize_level_matches_pyc(tmp_path: Path) -> None:
    """optimize 级别必须匹配 .pyc 文件名后缀（.opt-N），否则不剥离."""
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "mod.py").write_text("x = 1")
    # 生成 optimize=2 的 .pyc，但调用时 optimize=0 → 文件名不匹配
    _make_pyc_file(src / "mod.py", "3.11", optimize=2)

    # optimize=0 查找 mod.cpython-311.pyc，但实际是 mod.cpython-311.opt-2.pyc
    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=0)

    assert stripped == 0  # 文件名不匹配，不剥离
    assert (src / "mod.py").is_file()  # .py 保留


def test_strip_py_sources_skips_data_dirs(tmp_path: Path) -> None:
    """``data_dirs`` 内的 .py 不剥离（数据资源目录原样保留）.

    模拟 fspack 自身打包：``src/fspack/assets/templates/<each>/tk_app.py``
    是项目模板源码，``fsp doctor --test`` 复制后需 .py 存在才能 build。
    """
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('app')")
    # 模拟 assets/templates/gui/tk_app/tk_app.py
    tpl_dir = src / "fspack" / "assets" / "templates" / "gui" / "tk_app"
    tpl_dir.mkdir(parents=True)
    (tpl_dir / "tk_app.py").write_text("def main():\n    print('hi')\n")
    # 为两个 .py 都生成 .pyc（确保 PEP 3147 迁移条件满足，区别仅在 data_dirs 跳过）
    _make_pyc_file(src / "app.py", "3.11", optimize=0)
    _make_pyc_file(tpl_dir / "tk_app.py", "3.11", optimize=0)

    data_dirs = (tpl_dir.resolve(),)
    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=0, data_dirs=data_dirs)

    # 仅 app.py 被剥离，tk_app.py 保留
    assert stripped == 1
    assert not (src / "app.py").exists()
    assert (src / "app.pyc").is_file()  # app.pyc 迁移到 legacy 布局
    assert (tpl_dir / "tk_app.py").is_file()  # data_dirs 内保留 .py
    # data_dirs 内的 __pycache__/.pyc 不迁移（.py 未删除）
    pycache_files: list[Path] = list((tpl_dir / "__pycache__").glob("*.pyc"))
    assert pycache_files  # __pycache__ 下 .pyc 仍在


def test_strip_py_sources_data_dirs_empty_default_behavior(tmp_path: Path) -> None:
    """``data_dirs`` 为空时与不传一致：所有非 __init__.py/.pyc 缺失的 .py 都剥离."""
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "mod.py").write_text("x = 1")
    _make_pyc_file(src / "mod.py", "3.11", optimize=0)

    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=0, data_dirs=())

    assert stripped == 1
    assert not (src / "mod.py").exists()


def test_precompile_pyc_python_missing_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime python 未就绪时跳过 compileall，不调 subprocess."""
    runtime = tmp_path / "runtime"
    # 不创建 python.exe
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("")

    called: list[object] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: called.append(cmd))

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    assert not called  # 未调 subprocess


def test_precompile_pyc_compileall_failure_warns_not_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """compileall 非零退出码时仅 warning 不抛异常，且不写 stamp（下次构建重试）.

    iter-128 扩展"失败不缓存"策略：returncode != 0 与超时一致都不写 stamp，
    避免失败的编译被 stamp 跳过导致用户长期运行未编译的 .py。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("")

    class _Failed:
        returncode = 1
        stderr = "syntax error"
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _Failed())

    st = StageRecorder("预编译字节码")
    with caplog.at_level("WARNING", logger="fspack.builder"):
        _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    assert any("compileall 失败" in r.message for r in caplog.records)
    # 编译失败不写 stamp（iter-128）：下次构建重试，避免失败的编译被缓存跳过
    assert not (dist / ".pyc_stamp").is_file()


def test_precompile_pyc_optimize_passes_o_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """optimize 参数透传为解释器 ``-O``/``-OO`` 标志，控制字节码优化级别.

    用解释器标志而非 ``compileall -o N``：后者仅 Python 3.9+ CLI 支持，
    embed Python 3.8 会报 "unrecognized arguments: -o"。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or CompletedStub())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st, optimize=2)

    # 拆分两次调用：src 用 optimize=2（-OO），site-packages 降级到 1（-O，保留 docstring）
    assert len(captured) == 2
    src_cmd = next(cmd for cmd in captured if str(dist / "src") in cmd)
    assert "-OO" in src_cmd
    sp_cmd = next(cmd for cmd in captured if str(tmp_path / "dist" / "site-packages") in cmd)
    assert "-O" in sp_cmd
    assert "-OO" not in sp_cmd


def test_pyc_stamp_key_includes_sp_optimize(tmp_path: Path) -> None:
    """_pyc_stamp_key 纳入 sp_optimize，切换 site-packages 优化级别时强制重编译.

    site-packages 用 ``min(optimize, 1)`` 降级（保留 docstring），老 stamp（无
    sp_optimize 字段）自然失效，避免旧的剥离 docstring 的 .pyc 被加载触发
    第三方库 C 扩展兼容问题（numpy ``add_docstring`` 等）。
    """
    from fspack.builder import _pyc_stamp_key

    sp = tmp_path / "sp"
    sp.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("")
    # 同 optimize 不同 sp_optimize 产生不同 key
    key_sp0 = _pyc_stamp_key(src, sp, strip_py=False, optimize=2, sp_optimize=0)
    key_sp1 = _pyc_stamp_key(src, sp, strip_py=False, optimize=2, sp_optimize=1)
    assert key_sp0 != key_sp1
    # 默认 sp_optimize=0
    assert _pyc_stamp_key(src, sp, strip_py=False, optimize=2) == key_sp0


def test_precompile_pyc_optimize_default_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """optimize 默认 0，compileall 命令不含优化标志（生成无 opt 后缀的 .pyc）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or CompletedStub())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    for cmd in captured:
        assert "-O" not in cmd
        assert "-OO" not in cmd


def test_pyc_stamp_key_includes_optimize(tmp_path: Path) -> None:
    """_pyc_stamp_key 纳入 optimize，切换级别时强制重编译."""
    from fspack.builder import _pyc_stamp_key

    sp = tmp_path / "sp"
    sp.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("")
    key0 = _pyc_stamp_key(src, sp, strip_py=False, optimize=0)
    key1 = _pyc_stamp_key(src, sp, strip_py=False, optimize=1)
    key2 = _pyc_stamp_key(src, sp, strip_py=False, optimize=2)
    assert key0 != key1
    assert key0 != key2
    assert key1 != key2
    # 同级别稳定
    assert _pyc_stamp_key(src, sp, strip_py=False, optimize=0) == key0


def test_precompile_pyc_optimize_invalidates_old_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """切换 optimize 时旧 stamp 不命中，触发重编译."""
    from fspack.builder import _pyc_stamp_path

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    # 先用 optimize=0 编译，写 stamp
    captured_first: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured_first.append(cmd) or CompletedStub())
    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st, optimize=0)
    assert captured_first  # 实际调用了 compileall
    assert _pyc_stamp_path(dist).is_file()

    # 切换 optimize=2，应触发重编译
    captured_second: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured_second.append(cmd) or CompletedStub())
    st2 = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st2, optimize=2)
    assert captured_second  # 重新调用 compileall，stamp 未命中


# ---- Nuitka 编译模式与 stamp 缓存命中测试 ----


def test_precompile_pyc_stamp_cache_hit_skips_compileall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 命中时跳过 compileall 调用，stage 标注缓存命中."""
    from fspack.builder import _pyc_stamp_key, _pyc_stamp_path

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    # 预先写入匹配的 stamp
    stamp_key = _pyc_stamp_key(dist / "src", tmp_path / "dist" / "site-packages", strip_py=False, optimize=0)
    _pyc_stamp_path(dist).parent.mkdir(parents=True, exist_ok=True)
    _pyc_stamp_path(dist).write_text(stamp_key, encoding="utf-8")

    call_count = {"n": 0}
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: call_count.__setitem__("n", call_count["n"] + 1) or CompletedStub(),
    )

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # stamp 命中，不调用 compileall
    assert call_count["n"] == 0
    assert st._hits == 1
    assert "缓存命中" in st._detail


def test_strip_py_sources_skips_web_static_dirs(tmp_path: Path) -> None:
    """``web_static_dirs`` 内的 .py 不剥离（前端产物目录原样保留）."""
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('app')")
    # 模拟前端构建产物目录 dist/ 下的 .py 文件（如 JS 工具脚本）
    web_dir = src / "dist"
    web_dir.mkdir()
    (web_dir / "tool.py").write_text("def run():\n    pass\n")
    # 为两个 .py 都生成 .pyc（确保 PEP 3147 迁移条件满足，区别仅在 web_static_dirs 跳过）
    _make_pyc_file(src / "app.py", "3.11", optimize=0)
    _make_pyc_file(web_dir / "tool.py", "3.11", optimize=0)

    web_static_dirs = (web_dir.resolve(),)
    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=0, web_static_dirs=web_static_dirs)

    # 仅 app.py 被剥离，tool.py 保留
    assert stripped == 1
    assert not (src / "app.py").exists()
    assert (web_dir / "tool.py").is_file()
