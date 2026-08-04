"""``--log-file`` 构建日志持久化单元测试.

覆盖 :mod:`fspack.packaging.log_file` 与 CLI 层 ``--log-file``/``--log-format``
标志：

- :class:`LogFormat` 枚举与 :meth:`LogFormat.parse`
- :class:`TextFormatter`/`JsonFormatter`：格式化输出
- :func:`setup_log_file`/`teardown_log_file`：handler 生命周期
- CLI ``--log-file``/``--log-format`` 标志透传
- :func:`fspack.packaging.pipeline.build` 集成：日志写入文件 + 异常清理
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from fspack import cli
from fspack.packaging.log_file import (
    JsonFormatter,
    LogFileHandler,
    LogFormat,
    TextFormatter,
    setup_log_file,
    teardown_log_file,
)

# ---- LogFormat 枚举 ----


def test_log_format_values() -> None:
    """LogFormat 枚举值正确."""
    assert LogFormat.TEXT.value == "text"
    assert LogFormat.JSON.value == "json"


def test_log_format_parse_text() -> None:
    """parse('text') 返回 TEXT."""
    assert LogFormat.parse("text") is LogFormat.TEXT


def test_log_format_parse_json() -> None:
    """parse('json') 返回 JSON."""
    assert LogFormat.parse("json") is LogFormat.JSON


def test_log_format_parse_case_insensitive() -> None:
    """parse 大小写不敏感."""
    assert LogFormat.parse("TEXT") is LogFormat.TEXT
    assert LogFormat.parse("Json") is LogFormat.JSON


def test_log_format_parse_none_returns_text() -> None:
    """parse(None) 返回默认 TEXT."""
    assert LogFormat.parse(None) is LogFormat.TEXT


def test_log_format_parse_empty_string_returns_text() -> None:
    """parse('') 返回默认 TEXT."""
    assert LogFormat.parse("") is LogFormat.TEXT


def test_log_format_parse_unknown_raises() -> None:
    """未知格式名抛 ValueError."""
    with pytest.raises(ValueError, match="未知日志格式"):
        LogFormat.parse("xml")


# ---- TextFormatter ----


def _make_record(  # noqa: PLR0913
    name: str = "test.module",
    level: int = logging.INFO,
    msg: str = "测试消息",
    args: tuple[Any, ...] = (),
    exc_info: tuple[Any, ...] | None = None,
    func: str = "_make_record",
) -> logging.LogRecord:
    """构造 LogRecord 用于 Formatter 测试."""
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=42,
        msg=msg,
        args=args,
        exc_info=exc_info,
        func=func,
    )


def test_text_formatter_basic_format() -> None:
    """TextFormatter 输出含时间戳/级别/logger 名/消息."""
    formatter = TextFormatter()
    record = _make_record(msg="构建开始")
    output = formatter.format(record)
    assert "构建开始" in output
    assert "[INFO    ]" in output
    assert "test.module" in output
    # 时间戳格式 YYYY-MM-DD HH:MM:SS
    assert len(output.split(" ", 1)[0]) == 10  # 日期部分


def test_text_formatter_includes_exception() -> None:
    """TextFormatter 异常栈附加在消息后."""
    formatter = TextFormatter()
    try:
        raise ValueError("测试异常")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    record = _make_record(msg="操作失败", exc_info=exc_info)
    output = formatter.format(record)
    assert "操作失败" in output
    assert "ValueError" in output
    assert "测试异常" in output
    assert "Traceback" in output


def test_text_formatter_uses_args() -> None:
    """TextFormatter 支持 % 占位符延迟格式化."""
    formatter = TextFormatter()
    record = _make_record(msg="处理 %d 个文件，耗时 %.2fs", args=(10, 1.5))
    output = formatter.format(record)
    assert "处理 10 个文件，耗时 1.50s" in output


# ---- JsonFormatter ----


def test_json_formatter_outputs_valid_json() -> None:
    """JsonFormatter 输出可解析的 JSON 字符串."""
    formatter = JsonFormatter()
    record = _make_record(msg="构建开始")
    output = formatter.format(record)
    parsed = json.loads(output)
    assert isinstance(parsed, dict)
    assert parsed["message"] == "构建开始"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.module"
    assert parsed["module"] == "test_log_file"
    assert parsed["function"] == "_make_record"
    assert parsed["line"] == 42
    assert "timestamp" in parsed


def test_json_formatter_includes_exception() -> None:
    """JsonFormatter 异常时附加 exception 字段."""
    formatter = JsonFormatter()
    try:
        raise RuntimeError("测试错误")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()
    record = _make_record(msg="操作失败", exc_info=exc_info)
    parsed = json.loads(formatter.format(record))
    assert "exception" in parsed
    assert "RuntimeError" in parsed["exception"]
    assert "测试错误" in parsed["exception"]


def test_json_formatter_preserves_extra_fields() -> None:
    """JsonFormatter 保留 extra= 传入的自定义字段."""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test.extra",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="含 extra 的日志",
        args=(),
        exc_info=None,
    )
    record.__dict__["request_id"] = "req-abc-123"
    record.__dict__["build_phase"] = "compile"
    parsed = json.loads(formatter.format(record))
    assert parsed["request_id"] == "req-abc-123"
    assert parsed["build_phase"] == "compile"


def test_json_formatter_safe_serialize_non_serializable() -> None:
    """JsonFormatter 不可序列化对象转 repr."""
    formatter = JsonFormatter()
    record = _make_record(msg="测试")
    # set 不可 JSON 序列化（需通过 extra 传入）
    record.__dict__["custom_set"] = {1, 2, 3}
    parsed = json.loads(formatter.format(record))
    # repr({1, 2, 3}) 形如 "{1, 2, 3}"（顺序可能不同）
    assert isinstance(parsed["custom_set"], str)
    assert "1" in parsed["custom_set"]


def test_json_formatter_ensure_ascii_false() -> None:
    """JsonFormatter 中文消息原样输出（不转义）."""
    formatter = JsonFormatter()
    record = _make_record(msg="中文日志测试")
    output = formatter.format(record)
    assert "中文日志测试" in output
    # 不应出现 \uXXXX 转义
    assert "\\u" not in output


# ---- setup_log_file / teardown_log_file ----


def test_setup_log_file_creates_parent_dirs(tmp_path: Path) -> None:
    """setup_log_file 自动创建父目录."""
    log_path = tmp_path / "logs" / "sub" / "build.log"
    wrapper = setup_log_file(log_path, LogFormat.TEXT)
    try:
        assert log_path.parent.is_dir()
        assert wrapper.path == log_path.resolve()
    finally:
        teardown_log_file(wrapper)


def test_setup_log_file_attaches_handler_to_root(tmp_path: Path) -> None:
    """setup_log_file 将 handler 附加到 root logger."""
    root = logging.getLogger()
    initial_count = len(root.handlers)
    wrapper = setup_log_file(tmp_path / "build.log", LogFormat.TEXT)
    try:
        assert len(root.handlers) == initial_count + 1
        assert wrapper.handler in root.handlers
    finally:
        teardown_log_file(wrapper)
        assert len(root.handlers) == initial_count


def test_setup_log_file_writes_logs(tmp_path: Path) -> None:
    """setup_log_file 后日志写入文件."""
    root = logging.getLogger()
    original_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        log_path = tmp_path / "build.log"
        wrapper = setup_log_file(log_path, LogFormat.TEXT)
        try:
            logger = logging.getLogger("test.setup.write")
            logger.info("测试日志写入")
            wrapper.handler.flush()
        finally:
            teardown_log_file(wrapper)
        content = log_path.read_text(encoding="utf-8")
        assert "测试日志写入" in content
        assert "test.setup.write" in content
    finally:
        root.setLevel(original_level)


def test_setup_log_file_text_format(tmp_path: Path) -> None:
    """TEXT 格式写入纯文本日志."""
    root = logging.getLogger()
    original_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        log_path = tmp_path / "build.log"
        wrapper = setup_log_file(log_path, LogFormat.TEXT)
        try:
            logging.getLogger("test.fmt.text").info("文本格式测试")
            wrapper.handler.flush()
        finally:
            teardown_log_file(wrapper)
        content = log_path.read_text(encoding="utf-8")
        assert "[INFO" in content
        assert "文本格式测试" in content
    finally:
        root.setLevel(original_level)


def test_setup_log_file_json_format(tmp_path: Path) -> None:
    """JSON 格式写入结构化日志（每行一条 JSON）."""
    root = logging.getLogger()
    original_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        log_path = tmp_path / "build.log"
        wrapper = setup_log_file(log_path, LogFormat.JSON)
        try:
            logging.getLogger("test.fmt.json").info("JSON 格式测试")
            wrapper.handler.flush()
        finally:
            teardown_log_file(wrapper)
        content = log_path.read_text(encoding="utf-8").strip()
        # 每行应为合法 JSON，至少有一条日志
        lines = [ln for ln in content.splitlines() if ln.strip()]
        assert len(lines) >= 1
        target = next((json.loads(ln) for ln in lines if "JSON 格式测试" in ln), None)
        assert target is not None
        assert target["level"] == "INFO"
        assert target["logger"] == "test.fmt.json"
    finally:
        root.setLevel(original_level)


def test_setup_log_file_utf8_encoding(tmp_path: Path) -> None:
    """日志文件以 UTF-8 编码写入中文."""
    root = logging.getLogger()
    original_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        log_path = tmp_path / "build.log"
        wrapper = setup_log_file(log_path, LogFormat.TEXT)
        try:
            logging.getLogger("test.utf8").info("中文日志内容测试")
            wrapper.handler.flush()
        finally:
            teardown_log_file(wrapper)
        # 显式以 UTF-8 读取，验证无乱码
        content = log_path.read_text(encoding="utf-8")
        assert "中文日志内容测试" in content
    finally:
        root.setLevel(original_level)


def test_setup_log_file_append_mode(tmp_path: Path) -> None:
    """日志文件以追加模式写入，不覆盖既有内容."""
    root = logging.getLogger()
    original_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        log_path = tmp_path / "build.log"
        log_path.write_text("既有内容\n", encoding="utf-8")
        wrapper = setup_log_file(log_path, LogFormat.TEXT)
        try:
            logging.getLogger("test.append").info("新增内容")
            wrapper.handler.flush()
        finally:
            teardown_log_file(wrapper)
        content = log_path.read_text(encoding="utf-8")
        assert "既有内容" in content
        assert "新增内容" in content
    finally:
        root.setLevel(original_level)


def test_teardown_log_file_removes_handler(tmp_path: Path) -> None:
    """teardown_log_file 从 root logger 移除 handler."""
    root = logging.getLogger()
    initial_count = len(root.handlers)
    wrapper = setup_log_file(tmp_path / "build.log", LogFormat.TEXT)
    assert len(root.handlers) == initial_count + 1
    teardown_log_file(wrapper)
    assert len(root.handlers) == initial_count
    assert wrapper.handler not in root.handlers


def test_teardown_log_file_closes_handler(tmp_path: Path) -> None:
    """teardown_log_file 关闭文件 handler（释放文件锁）."""
    log_path = tmp_path / "build.log"
    wrapper = setup_log_file(log_path, LogFormat.TEXT)
    teardown_log_file(wrapper)
    # 关闭后 stream 应已关闭（FileHandler.close() 置 stream 为 None）
    assert wrapper.handler.stream is None or wrapper.handler.stream.closed


def test_teardown_log_file_none_is_noop() -> None:
    """teardown_log_file(None) 无操作不抛异常."""
    root = logging.getLogger()
    initial_count = len(root.handlers)
    teardown_log_file(None)
    assert len(root.handlers) == initial_count


def test_setup_teardown_round_trip_handler_count(tmp_path: Path) -> None:
    """多次 setup/teardown 不积累 handler."""
    root = logging.getLogger()
    initial_count = len(root.handlers)
    for i in range(3):
        wrapper = setup_log_file(tmp_path / f"build_{i}.log", LogFormat.TEXT)
        try:
            assert len(root.handlers) == initial_count + 1
        finally:
            teardown_log_file(wrapper)
        assert len(root.handlers) == initial_count


def test_log_file_handler_dataclass() -> None:
    """LogFileHandler 是 frozen dataclass，含 handler 与 path 字段."""
    from dataclasses import fields

    field_names = {f.name for f in fields(LogFileHandler)}
    assert field_names == {"handler", "path"}


# ---- CLI 层 --log-file/--log-format 透传 ----


def _make_minimal_project(tmp_path: Path) -> Path:
    """创建最小可解析项目."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return tmp_path


def test_cli_build_log_file_passed_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp b --log-file <path>`` 透传 log_file 路径给 build()."""
    _make_minimal_project(tmp_path)
    log_path = tmp_path / "build.log"
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: bool = False,
        auto_clean: bool = False,
    ) -> None:
        captured["log_file"] = log_file
        captured["log_format"] = log_format

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--log-file", str(log_path)])
    assert captured["log_file"] == log_path.resolve()
    assert captured["log_format"] is LogFormat.TEXT


def test_cli_build_log_format_json_passed_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp b --log-file <path> --log-format json`` 透传 JSON 格式."""
    _make_minimal_project(tmp_path)
    log_path = tmp_path / "build.log"
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: bool = False,
        auto_clean: bool = False,
    ) -> None:
        captured["log_file"] = log_file
        captured["log_format"] = log_format

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--log-file", str(log_path), "--log-format", "json"])
    assert captured["log_file"] == log_path.resolve()
    assert captured["log_format"] is LogFormat.JSON


def test_cli_build_without_log_file_passes_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --log-file 时 log_file=None."""
    _make_minimal_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: bool = False,
        auto_clean: bool = False,
    ) -> None:
        captured["log_file"] = log_file
        captured["log_format"] = log_format

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert captured["log_file"] is None
    assert captured["log_format"] is LogFormat.TEXT


def test_cli_build_log_format_invalid_rejected(tmp_path: Path) -> None:
    """``--log-format xml`` 被 argparse choices 拒绝."""
    _make_minimal_project(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["b", str(tmp_path), "--log-format", "xml"])


# ---- build() 集成：日志写入文件 + 异常清理 ----


def test_build_writes_log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build(log_file=...) 将构建日志写入文件."""
    from fspack.config import get_mirror
    from fspack.console import console
    from fspack.packaging.pipeline import build
    from fspack.platform import Platform

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")
    log_path = tmp_path / "build.log"

    # mock 写操作避免实际下载
    monkeypatch.setattr(
        "fspack.packaging.pipeline._prepare_runtime",
        lambda ctx: ctx.cfg.dist_dir / "runtime" / "Lib" / "site-packages",
    )
    monkeypatch.setattr("fspack.packaging.pipeline._analyze_dependencies", lambda ctx, **kw: _empty_report())
    monkeypatch.setattr("fspack.packaging.pipeline._download_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr("fspack.packaging.pipeline.write_pth", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.copy_source", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline._compile_user_sources", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline._build_entry_loaders", lambda *a, **kw: [])

    with console.rich.capture():
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, log_file=log_path)

    assert log_path.is_file()
    content = log_path.read_text(encoding="utf-8")
    # 构建过程会记录项目信息日志
    assert "app" in content
    assert "[INFO" in content


def test_build_cleans_up_log_file_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build() 异常时也正确清理日志 handler（try/finally）."""
    from fspack.config import get_mirror
    from fspack.packaging.pipeline import build
    from fspack.platform import Platform

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")
    log_path = tmp_path / "build.log"

    # 让 _execute_build 内部抛异常
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("构建失败模拟")

    monkeypatch.setattr("fspack.packaging.pipeline.resolve_project_info", boom)

    root = logging.getLogger()
    initial_count = len(root.handlers)
    with pytest.raises(RuntimeError, match="构建失败模拟"):
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, log_file=log_path)
    # handler 已被清理
    assert len(root.handlers) == initial_count


def test_build_log_format_json_writes_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build(log_format=JSON) 写入 JSON 结构化日志."""
    from fspack.config import get_mirror
    from fspack.console import console
    from fspack.packaging.pipeline import build
    from fspack.platform import Platform

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")
    log_path = tmp_path / "build.log"

    monkeypatch.setattr(
        "fspack.packaging.pipeline._prepare_runtime",
        lambda ctx: ctx.cfg.dist_dir / "runtime" / "Lib" / "site-packages",
    )
    monkeypatch.setattr("fspack.packaging.pipeline._analyze_dependencies", lambda ctx, **kw: _empty_report())
    monkeypatch.setattr("fspack.packaging.pipeline._download_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr("fspack.packaging.pipeline.write_pth", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.copy_source", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline._compile_user_sources", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline._build_entry_loaders", lambda *a, **kw: [])

    with console.rich.capture():
        build(
            proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, log_file=log_path, log_format=LogFormat.JSON
        )

    content = log_path.read_text(encoding="utf-8").strip()
    # 每行应为合法 JSON
    lines = [ln for ln in content.splitlines() if ln.strip()]
    for line in lines:
        parsed = json.loads(line)
        assert "level" in parsed
        assert "message" in parsed
        assert "timestamp" in parsed


def test_build_without_log_file_does_not_create_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 log_file 时不创建日志文件."""
    from fspack.config import get_mirror
    from fspack.console import console
    from fspack.packaging.pipeline import build
    from fspack.platform import Platform

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")
    log_path = tmp_path / "build.log"

    monkeypatch.setattr(
        "fspack.packaging.pipeline._prepare_runtime",
        lambda ctx: ctx.cfg.dist_dir / "runtime" / "Lib" / "site-packages",
    )
    monkeypatch.setattr("fspack.packaging.pipeline._analyze_dependencies", lambda ctx, **kw: _empty_report())
    monkeypatch.setattr("fspack.packaging.pipeline._download_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr("fspack.packaging.pipeline.write_pth", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.copy_source", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline._compile_user_sources", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline._build_entry_loaders", lambda *a, **kw: [])

    with console.rich.capture():
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)

    assert not log_path.exists()


def _empty_report() -> Any:
    """构造空 DependencyReport 用于 mock."""
    from fspack.config import DependencyReport

    return DependencyReport(
        declared=(),
        ast_third_party=(),
        ast_stdlib=(),
        ast_local=(),
        ast_submodules={},
    )
