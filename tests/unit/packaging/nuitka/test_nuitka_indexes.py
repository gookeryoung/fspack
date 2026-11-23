"""Nuitka 编译阶段测试：compile_with_stamp 每次都重编、stamp_key 六要素、失败文件列表（indexes.py）."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack.config import (
    get_mirror,
    nuitka_version_for,
)
from fspack.exceptions import NuitkaError
from fspack.packaging.nuitka import NuitkaCompiler
from fspack.platform import Platform
from fspack.progress import StageRecorder

# ---- compile_with_stamp 编译流程测试 ----


def test_compile_with_stamp_matching_stamp_still_recompiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：stamp 内容匹配但产物已被源码同步还原时，仍必须走 ensure_env + compile_src.

    旧实现把"stamp 命中"当作跨构建跳过依据，而 ``copy_source`` 在编译阶段之前
    把上次生成的 ``.pyd``/``.so`` 当陌生文件删除并还原 ``.py``，跳过即静默产出
    只有 ``.pyc`` 的发行包（审计复现：generated_artifact_survived_sync=false、
    python_source_restored=true、compilation_environment_calls=0）。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写与本次 stamp_key 完全一致的 stamp（模拟"上次已编译"）
    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    NuitkaCompiler._stamp_path(dist).write_text(expected_key, encoding="utf-8")

    calls = {"ensure": 0, "build_python": 0, "compile": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("ensure", calls["ensure"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("build_python", calls["build_python"] + 1) or Path()),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("compile", calls["compile"] + 1) or []),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    assert calls == {"ensure": 1, "build_python": 1, "compile": 1}
    # 不再产生"命中跳过"记录
    assert st._hits == 0
    assert "命中" not in st._detail
    assert st._items >= 1
    # 重编后 stamp 仍写入（内容不变）
    assert NuitkaCompiler._stamp_path(dist).read_text(encoding="utf-8") == expected_key


def test_compile_with_stamp_writes_stamp_after_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 stamp 时调用 ensure_env + ensure_build_python + compile_src 并写入 stamp."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    # mock standalone python 下载：返回占位路径（compile_src 也被 mock 不会真用到）
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: fake_py),
    )
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

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


def test_compile_with_stamp_stale_stamp_is_rewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """源码变化后旧 stamp 被覆盖为本次 stamp_key，且照常重编."""
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

    calls = {"ensure": 0, "build_python": 0, "compile": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("ensure", calls["ensure"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("build_python", calls["build_python"] + 1) or Path()),
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

    # stamp 不匹配，调用 ensure_env、_ensure_build_python 与 compile_src
    assert calls["ensure"] == 1
    assert calls["build_python"] == 1
    assert calls["compile"] == 1
    # 旧 stamp 内容被本次 stamp_key 覆盖
    stamp_text = NuitkaCompiler._stamp_path(dist).read_text(encoding="utf-8")
    assert stamp_text != old_key
    assert stamp_text == NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")


def test_compile_with_stamp_passes_build_python_to_compile_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_with_stamp 将 _ensure_build_python 返回的路径传给 compile_src.

    验证 standalone python 接入闭环：之前该步骤被遗漏导致 _ensure_build_python
    成死代码，编译回退到 embed runtime python 触发 Nuitka reExecute fork bomb
    （Windows 下反复衍生 python.exe 进程导致 CPU 卡死）。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    # standalone python 路径：mock 返回真实存在的文件路径
    fake_py = tmp_path / "fake_standalone_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: fake_py),
    )

    captured: dict[str, object] = {}

    def _capture_compile(cls: Any, *a: Any, **kw: Any) -> None:
        captured["build_python_exe"] = kw.get("build_python_exe")

    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(_capture_compile))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # 关键断言：compile_src 收到的 build_python_exe 正是 _ensure_build_python 的返回值
    assert captured["build_python_exe"] == fake_py


def test_compile_with_stamp_passes_data_dirs_to_compile_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_with_stamp 透传 data_dirs 到 compile_src（数据资源目录不编译）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "assets" / "templates").mkdir(parents=True)
    (src / "assets" / "templates" / "demo.py").write_text("x = 1")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_standalone_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: fake_py),
    )

    captured: dict[str, object] = {}

    def _capture_compile(cls: Any, *a: Any, **kw: Any) -> list[str]:
        captured["data_dirs"] = kw.get("data_dirs")
        return []

    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(_capture_compile))

    st = StageRecorder("Nuitka 编译")
    data_dirs = (src / "assets" / "templates",)
    NuitkaCompiler.compile_with_stamp(
        src,
        dist,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        get_mirror("aliyun"),
        cache_root,
        stage=st,
        data_dirs=data_dirs,
    )

    # 关键断言：compile_src 收到的 data_dirs 原样透传
    assert captured["data_dirs"] == data_dirs


def test_stamp_key_includes_nuitka_version_py_version_src_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stamp 键含 nuitka_version + py_version + src_fingerprint + entry_rels."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    key = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    assert "4.1.3" in key
    assert "3.11.9" in key
    # 六段式：version|py_version|src_fp|entry_part|pkg_part|data_part
    # （entry_rels=None 时 entry_part 为空）
    assert key.count("|") == 5
    # 末尾三段为空（entry_rels=None + nuitka_packages=() + data_dirs=()）
    assert key.endswith("|||")


def test_stamp_key_compiler_suffix(tmp_path: Path) -> None:
    """compiler 非 auto 时作为第七段拼入 stamp key；auto 不拼接保持既有格式."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")

    key_auto = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    key_auto_default = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", compiler="auto")
    key_mingw = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", compiler="mingw")
    key_msvc = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", compiler="msvc")

    # auto 不拼接：与既有六段式格式一致
    assert key_auto == key_auto_default
    assert key_auto.count("|") == 5
    # 非 auto 拼接第七段，且 mingw/msvc 互异
    assert key_mingw == f"{key_auto}|mingw"
    assert key_msvc == f"{key_auto}|msvc"
    assert key_mingw != key_msvc


def test_stamp_key_includes_entry_rels(tmp_path: Path) -> None:
    """entry_rels 纳入 stamp key：入口集合不同则 stamp 内容不同.

    stamp 记录"这次编译覆盖到哪些源码与入口"，供 dist 半成品检测与诊断使用，
    不再作为跳过编译的依据（跳过会让入口的 .py 缺失导致 run_path 失败）。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "snake.py").write_text("print('entry')")
    (src / "util.py").write_text("x = 1")

    key_no_entry = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    key_with_entry = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"snake.py"}))
    key_different_entry = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"util.py"}))

    # entry_rels 不同则 stamp key 不同
    assert key_no_entry != key_with_entry
    assert key_with_entry != key_different_entry
    # entry_rels 出现在 key 中（排序后拼接）
    assert "snake.py" in key_with_entry
    assert "util.py" in key_different_entry


def test_stamp_key_entry_rels_order_independent(tmp_path: Path) -> None:
    """entry_rels 集合迭代顺序不影响 stamp key（排序后拼接）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("")
    key1 = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"snake.py", "util.py"}))
    key2 = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"util.py", "snake.py"}))
    assert key1 == key2


def test_stamp_key_includes_data_dirs(tmp_path: Path) -> None:
    """data_dirs 纳入 stamp key：数据资源目录配置不同则 stamp 内容不同.

    data-dirs 增删改变哪些 .py 参与编译，stamp 记录本次编译范围便于诊断；
    跳过与否由"每次构建都重编"保证，新纳入编译的文件必然被编译。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "assets").mkdir()
    (src / "assets" / "tpl.py").write_text("x = 1")

    key_none = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    key_a = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, (), (src / "assets",))
    key_b = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, (), (src / "other",))

    # data_dirs 不同则 stamp key 不同（含指纹段差异与 data_part 差异）
    assert key_none != key_a
    assert key_a != key_b
    # data_dir 相对路径出现在 key 中（排序后拼接）
    assert "assets" in key_a


def test_stamp_key_data_dirs_order_independent(tmp_path: Path) -> None:
    """data_dirs 顺序无关：排序后拼接，集合相同则 key 相同."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1")
    (src / "a").mkdir()
    (src / "b").mkdir()

    key1 = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, (), (src / "a", src / "b"))
    key2 = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, (), (src / "b", src / "a"))
    assert key1 == key2


def test_stamp_key_data_dirs_content_changes_do_not_invalidate(tmp_path: Path) -> None:
    """data_dirs 内 .py 内容变化不改变 stamp key（其下 .py 不参与编译）.

    fspack 自构建 dev 循环：assets/templates/ 模板示例项目 .py 频繁编辑，
    若纳入指纹会让 stamp 内容随无关文件抖动，失去"标记本次编译范围"的诊断
    价值。data-dirs 树从指纹排除后仅非数据源码变化改变 key。
    指纹有构建级缓存，写文件后须手动失效再取 key。
    """
    from fspack.analyzer.fingerprint import clear_fingerprint_cache

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1")
    tpl = src / "assets" / "templates"
    tpl.mkdir(parents=True)
    (tpl / "demo.py").write_text("v = 1")

    key_before = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, (), (tpl,))
    (tpl / "demo.py").write_text("v = 222")
    clear_fingerprint_cache()
    key_after = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, (), (tpl,))
    assert key_before == key_after

    # 非数据源码变化仍须改变 key
    (src / "app.py").write_text("x = 222")
    clear_fingerprint_cache()
    key_src_changed = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, (), (tpl,))
    assert key_src_changed != key_after


def test_stamp_path_under_dist(tmp_path: Path) -> None:
    """stamp 文件位于 dist/.nuitka_compile_stamp."""
    dist = tmp_path / "dist"
    assert NuitkaCompiler._stamp_path(dist) == dist / ".nuitka_compile_stamp"


def test_compile_with_stamp_write_oserror_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """stamp 原子写入失败（os.replace OSError）时仅告警不中断.

    iter-128 改用 ``_atomic_write_text``（tempfile + os.replace）写 stamp，
    patch ``_atomic_write_text`` 抛 OSError 模拟只读文件系统/跨设备 rename 失败。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    def raise_oserror(*a: Any, **kw: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("fspack.packaging.nuitka.compile._atomic_write_text", raise_oserror)

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: Path()))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        # 不抛异常即通过（写入失败仅告警）
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    assert any("写入 Nuitka stamp 失败" in r.message for r in caplog.records)
    # stamp 未写入（原子化保证：要么完整写入要么不存在）
    assert not NuitkaCompiler._stamp_path(dist).is_file()


# ---- compile_with_stamp 环境就绪失败回退测试 ----


def test_compile_with_stamp_ensure_env_failure_falls_back_to_pyc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ensure_env 抛 NuitkaError（如 pip install 失败、C 编译器缺失）时回退到 .pyc 模式.

    Nuitka 是可选优化，环境就绪失败不应中断构建。回退后不写 stamp（下次构建仍会尝试）。
    """

    def _fail_ensure_env(cls: Any, *a: Any, **kw: Any) -> str:
        raise NuitkaError("pip install nuitka==4.1.3 失败（退出码 1）")

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(_fail_ensure_env))
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        # 不抛异常即通过（回退到 .pyc 模式）
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    assert any("回退到 .pyc 模式" in r.message for r in caplog.records)
    assert "回退到 .pyc 模式" in st._detail
    # 回退后不调用 compile_src
    assert compile_called["n"] == 0
    # 回退后不写 stamp（下次构建仍会尝试，避免永久跳过 Nuitka）
    assert not NuitkaCompiler._stamp_path(dist).is_file()


def test_compile_with_stamp_ensure_build_python_failure_falls_back_to_pyc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_ensure_build_python 抛 NuitkaError（如 standalone python 下载失败）时回退到 .pyc 模式."""

    def _fail_build_python(cls: Any, *a: Any, **kw: Any) -> Path:
        raise NuitkaError("下载 standalone python 失败: network unreachable")

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(_fail_build_python))
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    assert any("回退到 .pyc 模式" in r.message for r in caplog.records)
    assert compile_called["n"] == 0
    assert not NuitkaCompiler._stamp_path(dist).is_file()


def test_compile_with_stamp_compile_src_failure_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile_src 内部单文件编译失败已有 warning 继续，不触发回退机制.

    回退仅捕获环境就绪阶段（ensure_env + _ensure_build_python）的 NuitkaError，
    不捕获 compile_src 的编译失败（那是用户代码问题，非环境问题）。
    此处验证 compile_src 被 mock 为正常返回时，stamp 正常写入（不回退）。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # compile_src 正常调用 → stamp 写入（非回退路径）
    assert NuitkaCompiler._stamp_path(dist).is_file()
    assert "回退" not in st._detail


def test_compile_with_stamp_passes_nuitka_packages_to_compile_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile_with_stamp 透传 nuitka_packages 到 compile_packages."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    # 创建 site-packages 目录使 compile_with_stamp 进入 compile_packages 分支
    (dist / "site-packages").mkdir(parents=True)

    captured_pkgs: list[tuple[str, ...]] = []

    def fake_compile_packages(cls: Any, *args: Any, **kwargs: Any) -> None:
        captured_pkgs.append(args[1] if len(args) > 1 else kwargs.get("packages", ()))

    monkeypatch.setattr(NuitkaCompiler, "compile_packages", classmethod(fake_compile_packages))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src,
        dist,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        get_mirror("aliyun"),
        cache_root,
        stage=st,
        nuitka_packages=("rich", "click"),
    )

    assert captured_pkgs == [("rich", "click")]
    # stamp 写入（含 pkg_part）
    assert NuitkaCompiler._stamp_path(dist).is_file()


def test_compile_with_stamp_warns_when_site_packages_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """compile_with_stamp 在 site-packages 不存在时 warning 跳过包编译."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    # 不创建 site-packages 目录

    compile_packages_called: list[bool] = []

    def fake_compile_packages(cls: Any, *args: Any, **kwargs: Any) -> None:
        compile_packages_called.append(True)

    monkeypatch.setattr(NuitkaCompiler, "compile_packages", classmethod(fake_compile_packages))

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_with_stamp(
            src,
            dist,
            runtime,
            "3.11.9",
            Platform.WINDOWS,
            get_mirror("aliyun"),
            cache_root,
            stage=st,
            nuitka_packages=("rich",),
        )

    # compile_packages 未被调用（site-packages 不存在）
    assert not compile_packages_called
    assert any("site-packages 不存在" in r.message for r in caplog.records)


def test_stamp_key_includes_nuitka_packages(tmp_path: Path) -> None:
    """nuitka_packages 纳入 stamp key：包列表不同则 stamp 内容不同."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x")
    key_empty = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, ())
    key_with_pkgs = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, ("rich", "click"))
    assert key_empty != key_with_pkgs
    assert "rich,click" in key_with_pkgs


def test_load_failed_files_missing_returns_empty(tmp_path: Path) -> None:
    """_load_failed_files 文件不存在返回空 frozenset."""
    from fspack.packaging.nuitka.compile import _load_failed_files

    assert _load_failed_files(tmp_path) == frozenset()


def test_load_failed_files_valid_list(tmp_path: Path) -> None:
    """_load_failed_files 读取合法 JSON 列表."""
    from fspack.packaging.nuitka.compile import _load_failed_files

    (tmp_path / ".nuitka_failed_files.json").write_text('["a.py", "sub/b.py"]', encoding="utf-8")
    result = _load_failed_files(tmp_path)
    assert result == frozenset({"a.py", "sub/b.py"})


def test_load_failed_files_corrupt_json_deletes_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """_load_failed_files 内容损坏（非法 JSON）删除文件并返回空（与 wheels 依赖缓存策略一致）."""
    from fspack.packaging.nuitka.compile import _failed_files_path, _load_failed_files

    path = _failed_files_path(tmp_path)
    path.write_text("not a json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        result = _load_failed_files(tmp_path)
    assert result == frozenset()
    assert not path.exists(), "损坏的失败文件列表应被删除"
    assert any("失败文件列表损坏" in r.message for r in caplog.records)


def test_load_failed_files_non_list_deletes_file(tmp_path: Path) -> None:
    """_load_failed_files 顶层非 list 删除文件并返回空."""
    from fspack.packaging.nuitka.compile import _load_failed_files

    (tmp_path / ".nuitka_failed_files.json").write_text('{"key": "val"}', encoding="utf-8")
    result = _load_failed_files(tmp_path)
    assert result == frozenset()


def test_load_failed_files_strips_non_str_entries(tmp_path: Path) -> None:
    """_load_failed_files 剔除非 str 条目（保留 str 条目）."""
    from fspack.packaging.nuitka.compile import _load_failed_files

    (tmp_path / ".nuitka_failed_files.json").write_text('["a.py", 123, null, "b.py"]', encoding="utf-8")
    result = _load_failed_files(tmp_path)
    assert result == frozenset({"a.py", "b.py"})


def test_save_failed_files_writes_json(tmp_path: Path) -> None:
    """_save_failed_files 写入 JSON 列表."""
    from fspack.packaging.nuitka.compile import _failed_files_path, _load_failed_files, _save_failed_files

    _save_failed_files(tmp_path, ["a.py", "sub/b.py"])
    path = _failed_files_path(tmp_path)
    assert path.is_file()
    # 回读校验
    assert _load_failed_files(tmp_path) == frozenset({"a.py", "sub/b.py"})


def test_save_failed_files_empty_list_overwrites(tmp_path: Path) -> None:
    """_save_failed_files 空列表也写入，覆盖上次失败记录."""
    from fspack.packaging.nuitka.compile import _failed_files_path, _save_failed_files

    path = _failed_files_path(tmp_path)
    path.write_text('["old.py"]', encoding="utf-8")
    _save_failed_files(tmp_path, [])
    assert path.read_text(encoding="utf-8") == "[]"


def test_compile_with_stamp_writes_failed_files_after_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_with_stamp 编译后将失败文件列表写入 .nuitka_failed_files.json（iter-137）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "broken.py").write_text("syntax error")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    # compile_src 返回失败文件列表
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: ["broken.py"]))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    failed_file = dist / ".nuitka_failed_files.json"
    assert failed_file.is_file(), "失败文件列表应被写入"
    assert "broken.py" in failed_file.read_text(encoding="utf-8")


def test_compile_with_stamp_retries_previous_failed_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """编译路径恒全量重试：不读取上次失败文件列表，skip_files 恒 None.

    旧 BUG：跳过上次失败文件，且编译后用不含该文件的新列表覆盖写入 + 照写
    stamp，用户修复后的文件永远不被编译。新语义：每次构建全量重编，失败列表
    仅作诊断记录写入。
    """
    from fspack.packaging.nuitka.compile import _failed_files_path, _load_failed_files

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "broken.py").write_text("fixed now")
    dist = tmp_path / "dist"
    dist.mkdir()
    # 预置上次失败文件列表（含本次已修复的 broken.py）
    _failed_files_path(dist).write_text('["broken.py"]', encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))

    captured_skip: list[frozenset[str] | None] = []

    def fake_compile_src(cls: Any, *a: Any, **kw: Any) -> list[str]:
        captured_skip.append(kw.get("skip_files"))
        return []

    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(fake_compile_src))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    assert captured_skip, "compile_src 应被调用"
    # skip_files 恒 None：上次失败的 broken.py（已修复）参与全量重试
    assert captured_skip[0] is None, "每次构建都应全量重试（skip_files=None）"
    # 失败文件列表仍被写入（诊断记录），内容为本次 compile_src 返回值
    assert _load_failed_files(dist) == frozenset()
