"""``NuitkaStrip``/``NuitkaVerify`` 剥离与验证测试：.pyd 可加载性验证后剥离源码、批量/单模块导入探测与构建残留清理."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.nuitka import NuitkaCompiler
from tests._stubs import VerifyResultStub


def test_strip_compiled_sources_preserves_py_when_pyd_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """_strip_compiled_sources 验证 .pyd/.so 存在才删 .py：产物缺失时保留源码.

    Nuitka 可能 returncode==0 但未生成 .pyd（如文件名含 ``-`` 触发静默失败），
    此时删除 .py 会导致运行时 ImportError/访问违例。验证产物缺失时保留 .py 并告警。
    """
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    # 模拟 Nuitka returncode==0 但未生成 .pyd 的场景（rich._unicode_data.unicode10-0-0.py）
    py_file = tmp_path / "unicode10-0-0.py"
    py_file.write_text("x = 1")
    # 不创建 .pyd 产物

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        stripped = NuitkaCompiler._strip_compiled_sources({py_file}, st)

    # 未删除任何 .py（.pyd 缺失）
    assert stripped == 0
    assert py_file.is_file(), "产物缺失时应保留 .py 避免运行时 ImportError"
    # 告警提示未找到产物
    assert any("未找到 .pyd/.so 产物" in r.message for r in caplog.records)
    # stage 不计剥离
    assert st._skipped == 0


def test_strip_compiled_sources_deletes_py_when_pyd_exists(tmp_path: Path) -> None:
    """_strip_compiled_sources 在 .pyd 存在时删除 .py（正常路径）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    py_file = tmp_path / "app.py"
    py_file.write_text("x = 1")
    # 模拟 Nuitka 生成 .pyd 产物
    (tmp_path / "app.cp311-win_amd64.pyd").write_bytes(b"fake-pyd")

    st = StageRecorder("Nuitka 编译")
    stripped = NuitkaCompiler._strip_compiled_sources({py_file}, st)

    assert stripped == 1
    assert not py_file.exists()
    assert (tmp_path / "app.cp311-win_amd64.pyd").is_file()
    assert st._skipped == 1


def test_strip_compiled_sources_deletes_py_when_so_exists(tmp_path: Path) -> None:
    """Linux 平台 .so 产物同样支持验证."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    py_file = tmp_path / "mod.py"
    py_file.write_text("x = 1")
    (tmp_path / "mod.cpython-311-x86_64-linux-gnu.so").write_bytes(b"fake-so")

    st = StageRecorder("Nuitka 编译")
    stripped = NuitkaCompiler._strip_compiled_sources({py_file}, st)

    assert stripped == 1
    assert not py_file.exists()


# ---- _strip_compiled_sources 可选 import 验证测试 ----


class _CrashResult:
    """subprocess.run 桩：模拟批量验证崩溃（returncode=-1073741819 访问违例）."""

    def __init__(self) -> None:
        self.returncode = -1073741819  # 0xC0000005
        self.stdout = ""
        self.stderr = ""


class _SubprocessResult:
    """subprocess.run 返回值桩."""

    def __init__(self, returncode: int = 0, stdout: str | bytes = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _IndividualRunner:
    """subprocess.run 可调用桩：模拟逐个验证，按调用解析模块名返回结果.

    ``ok_modules`` 为二进制有效模块集合：输出 FSPACK_ONE_RESULT:1 标记并 exit 0；
    其余模块模拟硬崩溃（访问违例 returncode），与真实损坏 .pyd 行为一致。
    每次调用返回新的 :class:`_SubprocessResult`，避免 returncode 在调用间被覆盖。
    """

    def __init__(self, ok_modules: set[str]) -> None:
        self._ok_modules = ok_modules

    def __call__(self, cmd: list[str], **kwargs: Any) -> _SubprocessResult:
        # 从 -c 参数中提取模块名（最后一行 importlib.import_module({mod!r})）
        script = cmd[cmd.index("-c") + 1]
        mod = ""
        for line in reversed(script.split("\n")):
            if "importlib.import_module(" in line:
                # 提取引号中的模块名（支持单引号和双引号）
                start = line.find("'") + 1
                if start > 0:
                    end = line.find("'", start)
                    mod = line[start:end]
                else:
                    start = line.find('"') + 1
                    end = line.find('"', start)
                    mod = line[start:end]
                break
        if mod in self._ok_modules:
            # individual 测试 subprocess 未指定 encoding，stdout 为 bytes
            return _SubprocessResult(returncode=0, stdout=b"FSPACK_ONE_RESULT:1\n")
        return _SubprocessResult(returncode=-1073741819)


def test_strip_compiled_sources_verify_preserves_py_when_pyd_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_strip_compiled_sources 验证模式：.pyd 损坏时保留 .py 并删除损坏 .pyd.

    Nuitka 4.x 在 Python 3.13+ Windows 上用 zig 编译可能生成损坏 .pyd，
    验证发现损坏时删除 .pyd（避免运行时优先加载损坏产物）并保留 .py 回退到 .pyc。
    """
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    # 模拟 site-packages/rich/errors.py + 损坏的 errors.cp313-win_amd64.pyd
    site_packages = tmp_path / "site-packages"
    rich_dir = site_packages / "rich"
    rich_dir.mkdir(parents=True)
    # __init__.py 让 _find_package_root 推导包根为 site-packages/，模块名 rich.errors
    (rich_dir / "__init__.py").write_text("")
    py_file = rich_dir / "errors.py"
    py_file.write_text("class ConsoleError(Exception): pass")
    pyd_file = rich_dir / "errors.cp313-win_amd64.pyd"
    pyd_file.write_bytes(b"corrupt-pyd")

    # 批量验证返回 errors 模块不可加载
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: VerifyResultStub({"rich.errors": False}),
    )

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        stripped = NuitkaCompiler._strip_compiled_sources(
            {py_file},
            st,
            verify_py_exe=tmp_path / "python.exe",
            verify_search_root=site_packages,
        )

    assert stripped == 0, "损坏 .pyd 对应的 .py 不应删除"
    assert py_file.is_file(), "损坏 .pyd 时应保留 .py 回退到 .pyc"
    assert not pyd_file.exists(), "损坏 .pyd 应删除避免运行时优先加载"
    assert any("损坏" in r.message for r in caplog.records)


def test_strip_compiled_sources_verify_deletes_py_when_pyd_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_strip_compiled_sources 验证模式：.pyd 可加载时正常删除 .py."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    site_packages = tmp_path / "site-packages"
    rich_dir = site_packages / "rich"
    rich_dir.mkdir(parents=True)
    # __init__.py 让 _find_package_root 推导包根为 site-packages/，模块名 rich.errors
    (rich_dir / "__init__.py").write_text("")
    py_file = rich_dir / "errors.py"
    py_file.write_text("class ConsoleError(Exception): pass")
    pyd_file = rich_dir / "errors.cp313-win_amd64.pyd"
    pyd_file.write_bytes(b"valid-pyd")

    # 批量验证返回 errors 模块可加载
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: VerifyResultStub({"rich.errors": True}),
    )

    st = StageRecorder("Nuitka 编译")
    stripped = NuitkaCompiler._strip_compiled_sources(
        {py_file},
        st,
        verify_py_exe=tmp_path / "python.exe",
        verify_search_root=site_packages,
    )

    assert stripped == 1
    assert not py_file.exists(), "可加载 .pyd 时应删除 .py"
    assert pyd_file.is_file(), "可加载 .pyd 应保留"


def test_strip_compiled_sources_verify_fallback_to_individual_on_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """批量验证崩溃时回退到逐个验证，定位损坏的 .pyd.

    批量 subprocess 因损坏 .pyd 触发访问违例（returncode != 0），
    回退到逐个模块测试，仅损坏模块保留 .py，可加载模块正常删除 .py。
    """
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    site_packages = tmp_path / "site-packages"
    rich_dir = site_packages / "rich"
    rich_dir.mkdir(parents=True)
    # __init__.py 让 _find_package_root 推导包根为 site-packages/，模块名 rich.errors/rich.console
    (rich_dir / "__init__.py").write_text("")
    # 两个模块：errors 可加载，console 损坏
    errors_py = rich_dir / "errors.py"
    errors_py.write_text("class ConsoleError(Exception): pass")
    (rich_dir / "errors.cp313-win_amd64.pyd").write_bytes(b"valid-pyd")
    console_py = rich_dir / "console.py"
    console_py.write_text("print('hello')")
    (rich_dir / "console.cp313-win_amd64.pyd").write_bytes(b"corrupt-pyd")

    # 第一次批量测试崩溃，后续逐个测试只有 rich.errors 成功
    call_count = [0]
    individual_runner = _IndividualRunner({"rich.errors"})

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        call_count[0] += 1
        if call_count[0] == 1:
            # 批量测试崩溃
            return _CrashResult()
        # 逐个测试：rich.errors 成功，rich.console 崩溃
        return individual_runner(cmd, **kwargs)

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", _fake_run)

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        stripped = NuitkaCompiler._strip_compiled_sources(
            {errors_py, console_py},
            st,
            verify_py_exe=tmp_path / "python.exe",
            verify_search_root=site_packages,
        )

    assert stripped == 1, "仅可加载的 errors.py 应删除"
    assert not errors_py.exists(), "可加载 .pyd 对应的 .py 应删除"
    assert console_py.is_file(), "损坏 .pyd 对应的 .py 应保留"
    assert not (rich_dir / "console.cp313-win_amd64.pyd").exists(), "损坏 .pyd 应删除"
    assert any("批量验证 .pyd 崩溃" in r.message for r in caplog.records)


def test_verify_compiled_modules_empty_input() -> None:
    """_verify_compiled_modules 空输入返回空集合."""
    from fspack.packaging.nuitka import NuitkaCompiler

    verified, artifacts = NuitkaCompiler._verify_compiled_modules(Path("python.exe"), set())
    assert verified == set()
    assert artifacts == []


def test_find_package_root_derives_package_root(tmp_path: Path) -> None:
    """_find_package_root 自动推导包根，兼容 flat/src layout.

    - site-packages/rich/errors.py → site-packages/（rich/ 有 __init__.py）
    - dist/src/src/fspack/builder.py → dist/src/src/（fspack/ 有 __init__.py，src/ 无）
    - dist/src/main.py → dist/src/（main.py 父目录无 __init__.py）
    """
    from fspack.packaging.nuitka import NuitkaCompiler

    # flat layout: site-packages/rich/errors.py
    sp = tmp_path / "site-packages"
    rich_dir = sp / "rich"
    rich_dir.mkdir(parents=True)
    (rich_dir / "__init__.py").write_text("")
    errors_py = rich_dir / "errors.py"
    errors_py.write_text("")
    assert NuitkaCompiler._find_package_root(errors_py) == sp

    # src layout: dist/src/src/fspack/builder.py
    src_root = tmp_path / "dist" / "src" / "src"
    fspack_dir = src_root / "fspack"
    fspack_dir.mkdir(parents=True)
    (fspack_dir / "__init__.py").write_text("")
    builder_py = fspack_dir / "builder.py"
    builder_py.write_text("")
    assert NuitkaCompiler._find_package_root(builder_py) == src_root

    # 顶层模块: dist/src/main.py
    dist_src = tmp_path / "dist2" / "src"
    dist_src.mkdir(parents=True)
    main_py = dist_src / "main.py"
    main_py.write_text("")
    assert NuitkaCompiler._find_package_root(main_py) == dist_src


def test_batch_import_test_returns_none_on_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_batch_import_test 在 subprocess 崩溃时返回 None（调用方回退到逐个测试）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _CrashResult(),
    )

    result = NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors"])
    assert result is None


def test_batch_import_test_returns_importable_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_batch_import_test 成功时返回可加载模块集合."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: VerifyResultStub({"rich.errors": True, "rich.console": False}),
    )

    result = NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors", "rich.console"])
    assert result == {"rich.errors"}


def test_individual_import_test_locates_corrupt_pyd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_individual_import_test 逐个测试定位损坏 .pyd，仅返回可加载模块."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # rich.errors 可加载，rich.console 崩溃
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        _IndividualRunner({"rich.errors"}),
    )

    result = NuitkaCompiler._individual_import_test(
        tmp_path / "python.exe", [tmp_path], ["rich.errors", "rich.console"]
    )
    assert result == {"rich.errors"}


def test_binary_load_failure_snippet_classification() -> None:
    """分类规则：依赖缺失/模块代码层异常有效，DLL 加载失败与模块自身缺失判损坏."""
    from fspack.packaging.nuitka.verify import _BINARY_LOAD_FAILURE_SNIPPET

    ns: dict[str, Any] = {}
    exec(_BINARY_LOAD_FAILURE_SNIPPET, ns)
    fn = ns["_fspack_binary_load_failure"]

    # 依赖缺失：ModuleNotFoundError.name 指向第三方依赖（如模板模块 import PySide2）
    assert fn("main", ModuleNotFoundError("No module named 'PySide2'", name="PySide2")) is False
    assert fn("modules.module_b", ModuleNotFoundError("No module named 'ordered_set'", name="ordered_set")) is False
    # 模块自身缺失（模块名推导错误或产物不存在）
    assert fn("main", ModuleNotFoundError("No module named 'main'", name="main")) is True
    # DLL 加载失败（.pyd 二进制损坏）：ImportError 无 name
    assert fn("main", ImportError("DLL load failed while importing 'main'")) is True
    # 模块顶层代码运行时异常：.pyd 已成功加载执行
    assert fn("main", ValueError("boom")) is False
    assert fn("main", ZeroDivisionError("division by zero")) is False


def _make_verify_fixture(tmp_path: Path) -> Path:
    """构造真实验证场景：依赖缺失模块 + 损坏扩展产物，返回包根.

    - ``dep_missing.py``：顶层 import 不存在的第三方依赖（模拟模板模块 import PySide2）
    - ``junk.py`` + 垃圾字节扩展产物：import 时二进制加载失败（模拟损坏 .pyd）。
      扩展后缀用 :data:`importlib.machinery.EXTENSION_SUFFIXES[0]` 保证跨平台命名正确
      （Windows 为 ``.cp311-win_amd64.pyd``，Linux 为 ``.cpython-311-...so``），
      且扩展模块优先级高于 .py，import 必走损坏产物。
    """
    import importlib.machinery

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "dep_missing.py").write_text("import fspack_nonexistent_dep\n")
    (pkg / "junk.py").write_text("x = 1\n")
    (pkg / f"junk{importlib.machinery.EXTENSION_SUFFIXES[0]}").write_bytes(b"garbage-not-a-valid-binary")
    return pkg


def test_batch_import_test_real_subclassifies_dependency_missing(tmp_path: Path) -> None:
    """真实 subprocess 集成：依赖缺失判有效、损坏扩展产物判损坏.

    回归：模板模块顶层 import PySide2/pygame 等非本项目依赖时抛 ModuleNotFoundError，
    旧实现误判为 .pyd 损坏并删除产物；修复后仅二进制自身加载失败才判损坏。
    """
    from fspack.packaging.nuitka import NuitkaCompiler

    pkg = _make_verify_fixture(tmp_path)
    result = NuitkaCompiler._batch_import_test(Path(sys.executable), [pkg], ["dep_missing", "junk"])
    assert result is not None
    assert "dep_missing" in result, "依赖缺失应视为二进制有效"
    assert "junk" not in result, "损坏扩展产物应判损坏"


def test_individual_import_test_real_subclassifies_dependency_missing(tmp_path: Path) -> None:
    """真实 subprocess 集成（逐个测试）：依赖缺失判有效、损坏扩展产物判损坏."""
    from fspack.packaging.nuitka import NuitkaCompiler

    pkg = _make_verify_fixture(tmp_path)
    result = NuitkaCompiler._individual_import_test(Path(sys.executable), [pkg], ["dep_missing", "junk"])
    assert result == {"dep_missing"}


def test_batch_import_test_skips_non_prefix_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_batch_import_test 跳过非 FSPACK_VERIFY_RESULT 前缀的输出行."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # reversed 迭代：结果行在前 → 非前缀行在后，确保非前缀行也被遍历到
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _SubprocessResult(
            returncode=0,
            stdout=VerifyResultStub({"rich.errors": True}).stdout + "trailing line\nanother trailing\n",
        ),
    )
    result = NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors"])
    assert result == {"rich.errors"}


def test_batch_import_test_returns_none_on_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_batch_import_test 遇到前缀行但 JSON 损坏时返回 None（回退到逐个测试）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _SubprocessResult(
            returncode=0,
            stdout="FSPACK_VERIFY_RESULT:not-valid-json\n",
        ),
    )
    result = NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors"])
    assert result is None


def test_batch_import_test_timeout_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_batch_import_test 超时（模块顶层 input()/死循环/GUI）按验证失败处理返回 None.

    无超时会使构建永久挂起；超时返回 None 让调用方回退逐个测试定位阻塞模块。
    """

    def raise_timeout(cmd: list[str], **kwargs: Any) -> Any:
        # 模块顶层含 input()/死循环/GUI 启动代码，subprocess 永不退出
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30.0)

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", raise_timeout)

    with caplog.at_level(logging.WARNING, logger="fspack.packaging.nuitka"):
        result = NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors"])

    assert result is None
    assert any("超时" in r.message for r in caplog.records)


def test_batch_import_test_timeout_passes_timeout_kwarg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_batch_import_test 调 subprocess.run 时传入模块级超时常量."""
    from fspack.packaging.nuitka.verify import _IMPORT_TEST_TIMEOUT

    captured_kwargs: list[float] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        captured_kwargs.append(float(kwargs["timeout"]))
        return _CrashResult()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)
    NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors"])

    assert captured_kwargs == [_IMPORT_TEST_TIMEOUT]


def test_individual_import_test_timeout_treated_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_individual_import_test 单模块超时按该模块验证失败处理（不进结果集合）.

    超时模块判损坏保留 .py 回退 .pyc；其余模块测试不受影响，构建不挂起。
    """

    class _MixedRunner:
        """hanging 模块抛 TimeoutExpired，其余模块正常返回标记."""

        def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
            script = cmd[cmd.index("-c") + 1]
            if "hanging" in script:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=30.0)
            return _SubprocessResult(returncode=0, stdout=b"FSPACK_ONE_RESULT:1\n")

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", _MixedRunner())

    with caplog.at_level(logging.WARNING, logger="fspack.packaging.nuitka"):
        result = NuitkaCompiler._individual_import_test(tmp_path / "python.exe", [tmp_path], ["hanging", "normal"])

    assert result == {"normal"}
    assert any("超时" in r.message for r in caplog.records)


def test_import_test_timeout_constant_value() -> None:
    """``_IMPORT_TEST_TIMEOUT`` 默认 30s：覆盖常规模块导入耗时并防永久挂起."""
    from fspack.packaging.nuitka.verify import _IMPORT_TEST_TIMEOUT

    assert _IMPORT_TEST_TIMEOUT == 30.0


def test_verify_compiled_modules_strips_init_module_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_verify_compiled_modules 对 __init__.py 推导的模块名剥离 .__init__ 后缀."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # 构造 site-packages/rich/__init__.py + __init__.cp311-win_amd64.pyd
    site_packages = tmp_path / "site-packages"
    rich_dir = site_packages / "rich"
    rich_dir.mkdir(parents=True)
    init_py = rich_dir / "__init__.py"
    init_py.write_text("")
    (rich_dir / "__init__.cp311-win_amd64.pyd").write_bytes(b"fake")

    captured: dict[str, list[str]] = {}

    def fake_batch(py_exe: Path, roots: list[Path], mods: list[str]) -> set[str] | None:
        captured["mods"] = mods
        return set(mods)

    monkeypatch.setattr(NuitkaCompiler, "_batch_import_test", fake_batch)

    verified, _unverified = NuitkaCompiler._verify_compiled_modules(tmp_path / "python.exe", {init_py})
    assert init_py in verified
    # 模块名应为 "rich" 而非 "rich.__init__"
    assert captured["mods"] == ["rich"]


def test_strip_compiled_sources_no_verify_preserves_original_behavior(tmp_path: Path) -> None:
    """_strip_compiled_sources 不传验证参数时保持原有行为（仅检查 .pyd 存在）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    py_file = tmp_path / "app.py"
    py_file.write_text("x = 1")
    (tmp_path / "app.cp311-win_amd64.pyd").write_bytes(b"fake-pyd")

    st = StageRecorder("Nuitka 编译")
    # 不传 verify_py_exe 和 verify_search_root
    stripped = NuitkaCompiler._strip_compiled_sources({py_file}, st)

    assert stripped == 1
    assert not py_file.exists()


def test_cleanup_build_dirs_removes_residual(tmp_path: Path) -> None:
    """_cleanup_build_dirs 清理 Nuitka 编译失败的 .build 残留目录."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # 模拟编译失败残留的 .build 目录
    build1 = tmp_path / "rich" / "_unicode_data" / "unicode10-0-0.build"
    build1.mkdir(parents=True)
    (build1 / "module.unicode10-0-0.c").write_text("// c source")
    (build1 / "__constants.o").write_bytes(b"object")

    build2 = tmp_path / "app.build"
    build2.mkdir()
    (build2 / "scons-debug.py").write_text("# scons")

    # 非 .build 目录不清理
    keep_dir = tmp_path / "rich" / "_unicode_data"
    (keep_dir / "__init__.py").write_text("")

    cleaned = NuitkaCompiler._cleanup_build_dirs(tmp_path)

    assert cleaned == 2
    assert not build1.exists()
    assert not build2.exists()
    # 非 .build 目录与文件保留
    assert keep_dir.is_dir()
    assert (keep_dir / "__init__.py").is_file()


def test_cleanup_build_dirs_no_match(tmp_path: Path) -> None:
    """_cleanup_build_dirs 无 .build 目录时返回 0."""
    from fspack.packaging.nuitka import NuitkaCompiler

    (tmp_path / "app.py").write_text("x = 1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "mod.py").write_text("y = 2")

    cleaned = NuitkaCompiler._cleanup_build_dirs(tmp_path)
    assert cleaned == 0


def test_cleanup_build_dirs_skips_files_named_build(tmp_path: Path) -> None:
    """_cleanup_build_dirs 跳过名为 *.build 的文件（仅清理目录）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # app.build 是文件而非目录，应跳过
    build_file = tmp_path / "app.build"
    build_file.write_text("not a directory")
    # real.build 是目录，应清理
    build_dir = tmp_path / "real.build"
    build_dir.mkdir()
    (build_dir / "scons.py").write_text("# scons")

    cleaned = NuitkaCompiler._cleanup_build_dirs(tmp_path)
    assert cleaned == 1
    assert build_file.is_file(), "名为 .build 的文件应保留"
    assert not build_dir.exists(), ".build 目录应清理"


def test_cleanup_build_dirs_handles_rmtree_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_cleanup_build_dirs 遇到 rmtree OSError 时 warning 不中断."""
    from fspack.packaging.nuitka import NuitkaCompiler

    build_dir = tmp_path / "fail.build"
    build_dir.mkdir()
    (build_dir / "module.c").write_text("// c")

    def fail_rmtree(path: Path, **kwargs: Any) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("fspack.packaging.nuitka.compile.shutil.rmtree", fail_rmtree)

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        cleaned = NuitkaCompiler._cleanup_build_dirs(tmp_path)
    assert cleaned == 0
    assert any("清理 .build 目录失败" in r.message for r in caplog.records)


# ---- iter-137: 并发验证 + 失败文件列表测试 ----


def test_individual_import_test_concurrent_handles_multiple_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_individual_import_test 并发处理多个模块，仍正确返回可加载集合（iter-137 并发化）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # mod0/mod1 可加载，mod2/mod3 崩溃
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        _IndividualRunner({"mod0", "mod1"}),
    )

    result = NuitkaCompiler._individual_import_test(
        tmp_path / "python.exe",
        [tmp_path],
        ["mod0", "mod1", "mod2", "mod3"],
    )
    assert result == {"mod0", "mod1"}


def test_individual_import_test_empty_modules_returns_empty(tmp_path: Path) -> None:
    """_individual_import_test 空模块列表直接返回空集合，不启动线程池."""
    from fspack.packaging.nuitka import NuitkaCompiler

    result = NuitkaCompiler._individual_import_test(tmp_path / "python.exe", [tmp_path], [])
    assert result == set()
