"""runner_profile 模块测试：``fsp r --profile`` 打点采集与汇总.

覆盖 importtime 行解析（建树/分段/畸形行容错）、run_with_profile 真实子进程
流式采集（标记行收集/非标记行透传/汇总打印）与 runner.run 的 profile 分流
（环境变量注入与 debug 组合）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from fspack.runner import run as run_run
from fspack.runner_profile import (
    PROFILE_ENV,
    _pad,
    _parse_import_lines,
    _print_summary,
    _rpad,
    run_with_profile,
)

# ---- _parse_import_lines 单元测试 ----

# importtime 样本：表头 + 嵌套子模块 + depth-0 根导入 + 畸形行。
# CPython 行格式：import time: <self_us> | <cumulative_us> | <缩进><name>，
# 名字字段每级缩进 2 空格。
_IMPORTTIME_SAMPLE = [
    "import time: self [us] | cumulative | imported package",
    "import time:       100 |       150 |       _codecs",
    "import time:       200 |       350 |     codecs",
    "import time:       300 |       650 | encodings",
    "import time:       400 |      1050 | site",
    "import time:        60 |        60 | glob",
    "import time:       700 |       760 | runpy",
    "import time:       500 |      2000 | game",
    "import time:       150 |       400 |       numpy",
    "not an importtime line",
    "import time:  abc |  def | broken",
]


def test_parse_import_lines_segments() -> None:
    """glob 前的根导入计入解释器初始化段，glob 及其后为顶层导入段."""
    interp_ms, roots, self_top = _parse_import_lines(_IMPORTTIME_SAMPLE)
    # glob 之前仅 encodings(650us) + site(1050us) = 1.7ms
    assert interp_ms == pytest.approx(1.7)
    assert [name for name, _ in roots] == ["glob", "runpy", "game"]
    assert roots[2] == ("game", pytest.approx(2.0))
    # self 降序 top：runpy 0.7 / game 0.5 / site 0.4 / numpy 0.15 ...
    assert self_top[0] == ("runpy", pytest.approx(0.7))
    assert self_top[1] == ("game", pytest.approx(0.5))
    names_top = [name for name, _ in self_top]
    assert "site" in names_top
    assert "glob" in names_top
    # 畸形行（非 importtime / 非数字列）被跳过不进入任何列表
    assert all(name not in names_top for name in ("broken", "not an importtime line"))


def test_parse_import_lines_no_glob_fallback() -> None:
    """无 glob 根导入时（旧版 wrapper）全部根导入计入解释器初始化段."""
    lines = [
        "import time:       100 |       100 | encodings",
        "import time:       200 |       300 | site",
    ]
    interp_ms, roots, self_top = _parse_import_lines(lines)
    assert interp_ms == pytest.approx(0.4)
    assert roots == []
    assert len(self_top) == 2


def test_parse_import_lines_empty() -> None:
    """空输入返回零值与空列表."""
    interp_ms, roots, self_top = _parse_import_lines([])
    assert interp_ms == 0.0
    assert roots == []
    assert self_top == []


def test_parse_import_lines_nested_not_root() -> None:
    """缩进的子模块（深度 > 0）不计入根导入列表，只参与 self 排序."""
    lines = [
        "import time:       100 |       100 | glob",
        "import time:       999 |      1500 |   submodule",
    ]
    _, roots, self_top = _parse_import_lines(lines)
    assert [name for name, _ in roots] == ["glob"]
    assert self_top[0] == ("submodule", pytest.approx(0.999))


# ---- run_with_profile 真实子进程测试 ----

# 子进程脚本：输出 loader/timing 标记行与普通 stderr 行，退出码 0。
# \\n 在测试源码字符串中是字面反斜杠 n，传给子进程后是源码级换行转义。
_CHILD_CODE = (
    "import sys\n"
    "sys.stderr.write('[fspack loader] read_entry 耗时 1.5ms\\n')\n"
    "sys.stderr.write('[fspack loader] 加载 python313.dll 耗时 16.4ms\\n')\n"
    "sys.stderr.write('[fspack loader] loader 总耗时 18.9ms（进入 Python）\\n')\n"
    "sys.stderr.write('[fspack timing] env_ready @2.0ms\\n')\n"
    "sys.stderr.write('[fspack timing] entry_start @3.0ms\\n')\n"
    "sys.stderr.write('[fspack timing] entry_done @5.5ms\\n')\n"
    "sys.stderr.write('user-stderr-line\\n')\n"
    "sys.stderr.flush()\n"
    "print('user-stdout-line')\n"
)


def test_run_with_profile_collect_and_summarize(capsys: pytest.CaptureFixture[str]) -> None:
    """标记行被收集并由汇总替代；非标记 stderr 行原样透传；汇总各段齐全."""
    env = {**os.environ, **PROFILE_ENV}
    rc = run_with_profile([sys.executable, "-c", _CHILD_CODE], env=env)
    assert rc == 0
    captured = capsys.readouterr()
    # 汇总头、分隔线与总耗时
    assert "[fspack] 启动耗时剖析（总 wall time" in captured.out
    assert "退出码 0" in captured.out
    assert "─" * 30 in captured.out
    # loader 段：结构化展示（阶段名 + "耗时"词并入列展示，"loader 总"重组为
    # "loader 总（进入 Python）"）
    assert "read_entry" in captured.out
    assert "1.5ms" in captured.out
    assert "加载 python313.dll" in captured.out
    assert "16.4ms" in captured.out
    assert "loader 总（进入 Python）" in captured.out
    assert "18.9ms" in captured.out
    # wrapper 段：环境准备 = env_ready 累计值；用户入口执行 = done - start
    assert "环境准备" in captured.out
    assert "2.0ms" in captured.out
    assert "用户入口执行" in captured.out
    assert "2.5ms" in captured.out
    # import 段：真实解释器受 PYTHONPROFILEIMPORTTIME 激活输出 importtime
    assert "解释器初始化(约)" in captured.out
    # 占比列存在（"%"结尾的字段）
    assert "%" in captured.out
    # 短运行 gap 低于阈值，不显示未细分行
    assert "未细分" not in captured.out
    # 非标记行透传（stderr），标记行被汇总替代不透传
    assert "user-stderr-line" in captured.err
    assert "[fspack timing]" not in captured.err
    assert "[fspack loader]" not in captured.err


def test_run_with_profile_importtime_raw_lines_not_passed_through(capsys: pytest.CaptureFixture[str]) -> None:
    """importtime 原始行不透传（由汇总替代），顶层导入段来自真实 import."""
    env = {**os.environ, "PYTHONPROFILEIMPORTTIME": "1"}
    code = "import json\nprint('done')\n"
    rc = run_with_profile([sys.executable, "-c", code], env=env)
    assert rc == 0
    captured = capsys.readouterr()
    assert "解释器初始化(约)" in captured.out
    # import json 是真实顶层导入，应出现在汇总中
    assert "json" in captured.out
    # 原始 import time: 行不透传
    assert "import time:" not in captured.err


def test_run_with_profile_nonzero_exit(capsys: pytest.CaptureFixture[str]) -> None:
    """非零退出码：汇总标注退出码，返回值透传给调用方."""
    rc = run_with_profile([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert rc == 3
    captured = capsys.readouterr()
    assert "退出码 3" in captured.out


def test_run_with_profile_missing_entry_done(capsys: pytest.CaptureFixture[str]) -> None:
    """缺 entry_done 打点（os._exit/被杀）时显示未返回而非缺段."""
    code = (
        "import sys\n"
        "sys.stderr.write('[fspack timing] env_ready @1.0ms\\n')\n"
        "sys.stderr.write('[fspack timing] entry_start @2.0ms\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(0)\n"
    )
    rc = run_with_profile([sys.executable, "-c", code])
    assert rc == 0
    captured = capsys.readouterr()
    assert "环境准备" in captured.out
    assert "1.0ms" in captured.out
    assert "用户入口执行" in captured.out
    assert "未返回" in captured.out


def test_run_with_profile_no_markers(capsys: pytest.CaptureFixture[str]) -> None:
    """无任何标记行（旧 dist）时仅打印汇总头与 wall time，不报错."""
    rc = run_with_profile([sys.executable, "-c", "print('x')"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "[fspack] 启动耗时剖析" in captured.out
    assert "[loader]" not in captured.out
    assert "[wrapper]" not in captured.out


# ---- runner.run 的 profile 分流测试 ----


def _make_runnable_project(tmp_path: Path) -> Path:
    """构造可直接运行的 CLI 项目（dist/app.exe 存在）."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    return tmp_path


def test_run_profile_env_injected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """profile=True 时走 run_with_profile 且注入三个打点环境变量."""
    project = _make_runnable_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_profile(cmd: list[str], env: dict[str, str] | None = None) -> int:
        captured["cmd"] = cmd
        captured["env"] = env
        return 0

    monkeypatch.setattr("fspack.runner.run_with_profile", fake_profile)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    run_run(project, profile=True)
    assert captured["cmd"] == [str(project / "dist" / "app.exe")]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["FSPACK_LOADER_VERBOSE"] == "1"
    assert env["FSPACK_TIMING"] == "1"
    assert env["PYTHONPROFILEIMPORTTIME"] == "1"


def test_run_profile_debug_combined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """profile + debug 组合：embed python 命令 + PROFILE_ENV 与 PYTHONUNBUFFERED 并存."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    (dist / "runtime").mkdir(parents=True)
    (dist / "runtime" / "python.exe").write_bytes(b"")
    (dist / "_entry_app.py").write_text("pass\n")

    captured: dict[str, Any] = {}

    def fake_profile(cmd: list[str], env: dict[str, str] | None = None) -> int:
        captured["cmd"] = cmd
        captured["env"] = env
        return 0

    monkeypatch.setattr("fspack.runner.run_with_profile", fake_profile)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    run_run(tmp_path, debug=True, profile=True)
    assert captured["cmd"] == [str(dist / "runtime" / "python.exe"), str(dist / "_entry_app.py")]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["FSPACK_TIMING"] == "1"
    assert env["PYTHONPROFILEIMPORTTIME"] == "1"


def test_run_profile_nonzero_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """profile 模式非零退出码与普通模式一致抛 FspackError."""
    project = _make_runnable_project(tmp_path)

    monkeypatch.setattr("fspack.runner.run_with_profile", lambda cmd, env=None: 7)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    from fspack.exceptions import FspackError

    with pytest.raises(FspackError, match="程序退出码非零: 7"):
        run_run(project, profile=True)


def test_run_without_profile_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未启用 profile 时保持原 subprocess.run 路径（env=None、不注入变量）."""
    project = _make_runnable_project(tmp_path)
    captured: dict[str, Any] = {}

    class _Completed:
        returncode = 0

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return _Completed()

    monkeypatch.setattr("fspack.runner.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    run_run(project)
    assert captured["cmd"] == [str(project / "dist" / "app.exe")]
    assert captured["env"] is None


# ---- 汇总表排版与"未细分"归因测试 ----


def test_pad_cjk_display_width() -> None:
    """_pad/_rpad 按终端显示宽度对齐：CJK 全宽字符占 2 列，超宽不截断."""
    assert _pad("环境准备", 10) == "环境准备  "
    assert _pad("read_entry", 12) == "read_entry  "
    assert _rpad("1437.3ms", 10) == "  1437.3ms"
    assert _rpad("99.9%", 7) == "  99.9%"
    assert _pad("超长名称超过列宽时不截断", 6) == "超长名称超过列宽时不截断"


def test_print_summary_table_layout(capsys: pytest.CaptureFixture[str]) -> None:
    """汇总表逐行展示顶层导入/模块自身耗时子项，含耗时与占比两列.

    直接调用私有 _print_summary 注入构造数据（故障注入例外，避免为构造
    秒级 wall time 真实起子进程）。
    """
    timing = {"env_ready": 50.0, "entry_start": 51.0, "entry_done": 100.0}
    imports = [
        "import time:      2100 |      2100 | encodings",
        "import time:      3300 |      5400 | site",
        "import time:        60 |        60 | glob",
        "import time:   1437300 |  1437300 | app.controllers.app_controller",
        "import time:     66000 |     66000 | natsort.natsort",
    ]
    _print_summary(2000.0, 0, [("loader 总", 10.0, "（进入 Python）")], timing, imports)
    out = capsys.readouterr().out
    # 表头与分隔线
    assert "[fspack] 启动耗时剖析（总 wall time 2000ms，退出码 0）" in out
    assert "─" * 60 in out
    # loader 行结构化：阶段名 + 耗时 + 占比
    assert "loader 总（进入 Python）" in out
    assert "10.0ms" in out
    assert "0.5%" in out
    # 顶层导入子项行：名称 + cumulative + 占比（1437.3ms / 2000ms = 71.9%）
    assert "app.controllers.app_controller" in out
    assert "1437.3ms" in out
    assert "71.9%" in out
    # 模块自身耗段子项（glob 0.06ms 低于阈值被过滤，剩 4 条）
    assert "模块自身耗时 top4" in out
    assert "natsort.natsort" in out
    assert "66.0ms" in out
    # wrapper 段
    assert "环境准备" in out
    assert "50.0ms" in out
    assert "用户入口执行" in out
    assert "49.0ms" in out


def test_print_summary_gap_old_dist_hint(capsys: pytest.CaptureFixture[str]) -> None:
    """旧 dist（无 wrapper 打点）大 gap 显示未细分行与重新构建提示."""
    _print_summary(7000.0, 0, [("read_entry", 0.0, ""), ("loader 总", 0.0, "（进入 Python）")], {}, [])
    out = capsys.readouterr().out
    assert "未细分" in out
    assert "旧 dist 无 wrapper 打点" in out
    assert "重新 fsp b" in out
    assert "~" in out


def test_print_summary_gap_teardown_new_dist(capsys: pytest.CaptureFixture[str]) -> None:
    """新 dist 末端大 gap 归因为进程收尾，不提示重新构建."""
    timing = {"env_ready": 5.0, "entry_start": 6.0, "entry_done": 5000.0}
    _print_summary(7000.0, 0, [("loader 总", 10.0, "")], timing, [])
    out = capsys.readouterr().out
    assert "未细分" in out
    assert "进程收尾" in out
    assert "重新 fsp b" not in out
    # wrapper 各段正常展示：入口执行 = 5000 - 6 = 4994ms
    assert "环境准备" in out
    assert "用户入口执行" in out
    assert "4994.0ms" in out


def test_print_summary_gap_entry_missing(capsys: pytest.CaptureFixture[str]) -> None:
    """有 env_ready 但缺 entry_done 的大 gap 归因为入口未完成打点."""
    timing = {"env_ready": 5.0, "entry_start": 6.0}
    _print_summary(5000.0, 0, [], timing, [])
    out = capsys.readouterr().out
    assert "未细分" in out
    assert "入口执行未完成打点" in out
    assert "未返回" in out


def test_print_summary_small_gap_hidden(capsys: pytest.CaptureFixture[str]) -> None:
    """低于阈值的小 gap 不显示未细分行，避免噪音."""
    timing = {"env_ready": 5.0, "entry_start": 6.0, "entry_done": 5950.0}
    _print_summary(6000.0, 0, [("loader 总", 10.0, "")], timing, [])
    out = capsys.readouterr().out
    assert "未细分" not in out
