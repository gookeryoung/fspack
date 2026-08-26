"""stamp 缓存体系测试：compile_with_stamp、stamp_key 六要素、hash 索引与失败文件列表（indexes.py）."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fspack.config import (
    get_mirror,
    nuitka_version_for,
)
from fspack.exceptions import NuitkaError
from fspack.packaging.nuitka import NuitkaCompiler
from fspack.packaging.nuitka.compile import (
    _HASH_INDEX_MAX,
    _hash_index_path,
    _load_hash_index,
    _update_hash_index,
)
from fspack.platform import Platform
from fspack.progress import StageRecorder

# ---- compile_with_stamp stamp 缓存测试 ----


def test_compile_with_stamp_cache_hit_skips_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 命中时跳过 ensure_env 与 compile_src."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写匹配的 stamp
    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    NuitkaCompiler._stamp_path(dist).write_text(expected_key, encoding="utf-8")

    ensure_called = {"n": 0}
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: ensure_called.__setitem__("n", ensure_called["n"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    assert ensure_called["n"] == 0
    assert compile_called["n"] == 0
    assert st._hits == 1
    assert "stamp 命中" in st._detail


def test_compile_with_stamp_writes_stamp_after_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 未命中时调用 ensure_env + ensure_build_python + compile_src 并写入 stamp."""
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


def test_compile_with_stamp_invalidates_on_src_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """源码变化使 stamp 失效，重新调用 ensure_env + ensure_build_python + compile_src."""
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
    """compiler 非 auto 时拼入 stamp key：切换编译器强制重编；auto 不拼接保持兼容."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")

    key_auto = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    key_auto_default = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", compiler="auto")
    key_mingw = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", compiler="mingw")
    key_msvc = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", compiler="msvc")

    # auto 不拼接：与既有六段式格式一致，存量 stamp 不失效
    assert key_auto == key_auto_default
    assert key_auto.count("|") == 5
    # 非 auto 拼接第七段，且 mingw/msvc 互异
    assert key_mingw == f"{key_auto}|mingw"
    assert key_msvc == f"{key_auto}|msvc"
    assert key_mingw != key_msvc


def test_stamp_key_includes_entry_rels(tmp_path: Path) -> None:
    """entry_rels 纳入 stamp key：入口集合变化时 stamp 失效，强制重编.

    避免上次编译删除了 .py、本次新增入口跳过但 .py 已不在导致 run_path 失败。
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
    """data_dirs 纳入 stamp key：配置变化时编译范围变化，须强制重编.

    data-dirs 增删会改变哪些 .py 被跳过编译；stamp 仍命中会导致新纳入编译的
    文件永不编译（其 .py 已被上次构建剥离的场景）。
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
    """data_dirs 内 .py 内容变化不改变 stamp key（不参与编译，无需重编）.

    fspack 自构建 dev 循环：assets/templates/ 模板示例项目 .py 频繁编辑，
    若纳入指纹会导致 Nuitka 全量重编。data-dirs 树从指纹排除后仅非数据
    源码变化触发失效。指纹有构建级缓存，写文件后须手动失效再取 key。
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

    # 非数据源码变化仍须失效
    (src / "app.py").write_text("x = 222")
    clear_fingerprint_cache()
    key_src_changed = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, (), (tpl,))
    assert key_src_changed != key_after


def test_stamp_path_under_dist(tmp_path: Path) -> None:
    """stamp 文件位于 dist/.nuitka_compile_stamp."""
    dist = tmp_path / "dist"
    assert NuitkaCompiler._stamp_path(dist) == dist / ".nuitka_compile_stamp"


def test_compile_with_stamp_read_oserror_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 读取 OSError（如磁盘错误）时容错继续编译流程，不崩溃."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写 stamp 文件使 is_file() 为 True，随后 read_text 抛 OSError
    stamp = NuitkaCompiler._stamp_path(dist)
    stamp.write_text("stale", encoding="utf-8")

    orig_read_text = Path.read_text

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == stamp:
            raise OSError("disk error")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    calls = {"compile": 0}
    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: Path()))
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("compile", calls["compile"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # OSError 被容错：继续执行编译流程
    assert calls["compile"] == 1


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


# ---- compile_with_stamp hash 索引回退测试（iter-129） ----


def test_hash_index_path_under_dist(tmp_path: Path) -> None:
    """hash 索引文件位于 dist/.nuitka_hash_index.json，与 stamp 同目录."""
    dist = tmp_path / "dist"
    assert _hash_index_path(dist) == dist / ".nuitka_hash_index.json"


def test_load_hash_index_missing_file_returns_empty(tmp_path: Path) -> None:
    """索引文件不存在时返回空 dict，不抛异常."""
    dist = tmp_path / "dist"
    dist.mkdir()
    assert _load_hash_index(dist) == {}


def test_load_hash_index_corrupt_json_deletes_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """索引文件 JSON 非法时删除并返回空 dict（与 _load_deps_cache 策略一致）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    index_file.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        result = _load_hash_index(dist)

    assert result == {}
    assert not index_file.is_file()
    assert any("hash 索引损坏" in r.message for r in caplog.records)


def test_load_hash_index_non_dict_deletes_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """索引文件顶层非 dict 时删除并返回空 dict."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    index_file.write_text('["not", "a", "dict"]', encoding="utf-8")

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        result = _load_hash_index(dist)

    assert result == {}
    assert not index_file.is_file()
    assert any("非 dict" in r.message for r in caplog.records)


def test_load_hash_index_strips_non_str_entries(tmp_path: Path) -> None:
    """索引含非 str 键/值时剔除异常条目，保留有效条目并回写."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    # 混合有效与无效条目：123 是 int 键，"valid" 是有效条目，None 是无效值
    raw = json.dumps({"valid": "2026-01-01T00:00:00", "123": "2026-01-01", "bad_val": None})
    index_file.write_text(raw, encoding="utf-8")

    result = _load_hash_index(dist)

    # 仅保留 valid 条目（int 键 JSON 转为 str，但值 None 被剔除）
    # 注意：json.loads 把数字键转为 str，所以 "123" 实际是 str 键 + str 值，会被保留
    # 真正被剔除的是 "bad_val": None（值非 str）
    assert result["valid"] == "2026-01-01T00:00:00"
    assert "bad_val" not in result
    # 索引文件被回写（剔除后）
    rewritten = json.loads(index_file.read_text(encoding="utf-8"))
    assert "bad_val" not in rewritten


def test_load_hash_index_read_oserror_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """索引读取 OSError（如权限错误）时返回空 dict，不删除文件（瞬时错误）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    index_file.write_text('{"k": "v"}', encoding="utf-8")

    orig_read_text = Path.read_text

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == index_file:
            raise OSError("permission denied")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        result = _load_hash_index(dist)

    assert result == {}
    # OSError 不删除文件（瞬时错误，下次重试）
    assert index_file.is_file()
    assert any("读取 hash 索引失败" in r.message for r in caplog.records)


def test_load_hash_index_corrupt_json_unlink_oserror_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """索引损坏但删除文件失败时仅告警，仍返回空 dict（不因删除失败中断）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    index_file.write_text("{corrupt", encoding="utf-8")

    def raise_oserror(self: Path, *args: Any, **kwargs: Any) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", raise_oserror)

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        result = _load_hash_index(dist)

    assert result == {}
    # 删除失败告警
    assert any("删除文件失败" in r.message for r in caplog.records)
    # 文件仍在（删除失败）
    assert index_file.is_file()


def test_update_hash_index_writes_new_entry(tmp_path: Path) -> None:
    """更新索引：新条目写入，含当前 ISO 时间戳."""
    dist = tmp_path / "dist"
    dist.mkdir()
    stamp_key = "4.1.3|3.11.9|fingerprint||"

    _update_hash_index(dist, stamp_key)

    index = json.loads(_hash_index_path(dist).read_text(encoding="utf-8"))
    assert stamp_key in index
    assert isinstance(index[stamp_key], str)
    assert len(index[stamp_key]) > 0


def test_update_hash_index_merges_existing(tmp_path: Path) -> None:
    """更新索引：保留已有条目，合并新条目."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    index_file.write_text('{"old_key": "2026-01-01T00:00:00"}', encoding="utf-8")

    _update_hash_index(dist, "new_key")

    index = json.loads(index_file.read_text(encoding="utf-8"))
    assert "old_key" in index
    assert "new_key" in index


def test_update_hash_index_lru_eviction(tmp_path: Path) -> None:
    """索引超过 _HASH_INDEX_MAX 时按时间戳淘汰最旧条目."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    # 预写 _HASH_INDEX_MAX 条旧条目（同一天内秒数递增，字符串排序与数值一致）
    old_index = {f"old_{i:02d}": f"2026-01-01T00:00:{i:02d}" for i in range(_HASH_INDEX_MAX)}
    index_file.write_text(json.dumps(old_index), encoding="utf-8")

    # 更新一条新条目（now_iso 比所有旧条目都新）
    _update_hash_index(dist, "new_key")

    index = json.loads(index_file.read_text(encoding="utf-8"))
    # 总数不超过 _HASH_INDEX_MAX
    assert len(index) == _HASH_INDEX_MAX
    # 新条目保留
    assert "new_key" in index
    # 最旧条目被淘汰（old_00 时间戳最早）
    assert "old_00" not in index
    # 次新条目保留
    assert f"old_{_HASH_INDEX_MAX - 1:02d}" in index


def test_update_hash_index_write_oserror_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """索引原子写入失败时仅告警不中断（索引是回退优化，不影响主流程）."""
    dist = tmp_path / "dist"
    dist.mkdir()

    def raise_oserror(*a: Any, **kw: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("fspack.packaging.nuitka.compile._atomic_write_text", raise_oserror)

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        # 不抛异常即通过
        _update_hash_index(dist, "some_key")

    assert any("写入 hash 索引失败" in r.message for r in caplog.records)


def test_compile_with_stamp_hash_index_hit_skips_compile_and_restamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stamp 未命中但 hash 索引命中时跳过编译，重建 stamp（iter-129 核心场景）.

    场景：dist 完整保留（.pyd 产物在）但 stamp 文件单独丢失/损坏。
    索引与 stamp 同在 dist/，删除 dist 时一并清理，故索引命中安全。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写 hash 索引含当前 stamp_key，但不写 stamp 文件
    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    _hash_index_path(dist).write_text(json.dumps({expected_key: "2026-01-01T00:00:00"}), encoding="utf-8")

    ensure_called = {"n": 0}
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: ensure_called.__setitem__("n", ensure_called["n"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # 索引命中：跳过 ensure_env 与 compile_src
    assert ensure_called["n"] == 0
    assert compile_called["n"] == 0
    assert st._hits == 1
    assert "hash 索引命中" in st._detail
    # stamp 被重建
    stamp = NuitkaCompiler._stamp_path(dist)
    assert stamp.is_file()
    assert stamp.read_text(encoding="utf-8") == expected_key


def test_compile_with_stamp_hash_index_miss_proceeds_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 未命中且 hash 索引未命中时走完整编译，编译后更新索引."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 不写 stamp，不写索引（索引文件不存在）

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # 编译后 stamp 与索引均写入
    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    assert NuitkaCompiler._stamp_path(dist).read_text(encoding="utf-8") == expected_key
    index = json.loads(_hash_index_path(dist).read_text(encoding="utf-8"))
    assert expected_key in index


def test_compile_with_stamp_hash_index_corrupt_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """hash 索引文件损坏时删除并走完整编译（不因损坏中断）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写损坏的索引文件
    _hash_index_path(dist).write_text("{corrupt json", encoding="utf-8")

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
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

    # 损坏索引被删除，走完整编译
    assert compile_called["n"] == 1
    assert any("hash 索引损坏" in r.message for r in caplog.records)
    # 编译后索引重建
    assert _hash_index_path(dist).is_file()


def test_compile_with_stamp_hash_index_hit_restamp_oserror_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """hash 索引命中但重建 stamp 失败时仅告警，仍跳过编译（索引命中即视为已编译）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    _hash_index_path(dist).write_text(json.dumps({expected_key: "2026-01-01T00:00:00"}), encoding="utf-8")

    # patch _atomic_write_text 抛 OSError（仅影响 stamp 重建）
    def raise_oserror(*a: Any, **kw: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("fspack.packaging.nuitka.compile._atomic_write_text", raise_oserror)

    ensure_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: ensure_called.__setitem__("n", ensure_called["n"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    # 索引命中仍跳过编译（ensure_env 未调用）
    assert ensure_called["n"] == 0
    assert st._hits == 1
    # 重建 stamp 失败告警
    assert any("重建 Nuitka stamp 失败" in r.message for r in caplog.records)
    # stamp 未写入（_atomic_write_text 抛 OSError）
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
    """nuitka_packages 纳入 stamp key：包列表变化时 stamp 失效."""
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
    """_load_failed_files 内容损坏（非法 JSON）删除文件并返回空（与 _load_hash_index 策略一致）."""
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


def test_compile_with_stamp_stamp_miss_retries_failed_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 未命中（源码已变化）时全量重试：不读取失败文件列表，skip_files 恒 None.

    旧 BUG：stamp 未命中仍传 skip_files 跳过上次失败文件，且编译后用不含该文件的
    新列表覆盖写入 + 照写 stamp，用户修复后的文件永远不被编译。新语义：编译路径
    恒全量重试，失败列表仅作诊断记录写入。
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
    assert captured_skip[0] is None, "stamp 未命中时应全量重试（skip_files=None）"
    # 失败文件列表仍被写入（诊断记录），内容为本次 compile_src 返回值
    assert _load_failed_files(dist) == frozenset()


def test_compile_with_stamp_cache_hit_does_not_read_failed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stamp 命中时跳过整个 Nuitka，不读取失败文件列表（无意义）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预置 stamp 命中
    nuitka_ver = nuitka_version_for("3.11.9")
    stamp_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    NuitkaCompiler._stamp_path(dist).write_text(stamp_key, encoding="utf-8")

    compile_called = {"yes": False}

    def fake_compile_src(cls: Any, *a: Any, **kw: Any) -> list[str]:
        compile_called["yes"] = True
        return []

    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(fake_compile_src))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    assert not compile_called["yes"], "stamp 命中时不应调用 compile_src"
