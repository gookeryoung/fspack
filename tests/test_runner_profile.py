"""runner_profile 模块测试：``fsp r --profile`` 打点采集与汇总.

覆盖 importtime 行解析（建树/分段/畸形行容错）、run_with_profile 真实子进程
流式采集（标记行收集/非标记行透传/汇总打印）与 runner.run 的 profile 分流
（环境变量注入与 debug 组合）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.profile_log import ProfileOptions
from fspack.runner import RunOptions
from fspack.runner import run as run_run
from fspack.runner_profile import (
    PROFILE_ENV,
    ProfileSample,
    _fmt_bar,
    _pad,
    _parse_import_lines,
    _print_summary,
    _rpad,
    run_with_profile,
)


def _opts(
    debug: bool = False,
    entry: str | None = None,
    profile: bool = False,
    profile_out: Path | None = None,
    profile_compare: str | None = None,
) -> RunOptions:
    """构造 RunOptions（测试便捷封装，收敛散参数）."""
    return RunOptions(
        debug=debug,
        entry=entry,
        profile=ProfileOptions(enabled=profile, out=profile_out, compare=profile_compare),
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
    """runpy 前的根导入计入解释器初始化段，runpy 及其后为顶层导入段."""
    interp_ms, roots, self_top = _parse_import_lines(_IMPORTTIME_SAMPLE)
    # runpy 之前：encodings(650us) + site(1050us) + glob(60us) = 1.76ms
    assert interp_ms == pytest.approx(1.76)
    assert [name for name, _ in roots] == ["runpy", "game"]
    assert roots[1] == ("game", pytest.approx(2.0))
    # self 降序 top：runpy 0.7 / game 0.5 / site 0.4 / numpy 0.15 ...
    assert self_top[0] == ("runpy", pytest.approx(0.7))
    assert self_top[1] == ("game", pytest.approx(0.5))
    names_top = [name for name, _ in self_top]
    assert "site" in names_top
    assert "glob" in names_top
    # 畸形行（非 importtime / 非数字列）被跳过不进入任何列表
    assert all(name not in names_top for name in ("broken", "not an importtime line"))


def test_parse_import_lines_no_runpy_fallback() -> None:
    """无 runpy 根导入时（极旧版 wrapper）全部根导入计入解释器初始化段."""
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
        "import time:       100 |       100 | runpy",
        "import time:       999 |      1500 |   submodule",
    ]
    _, roots, self_top = _parse_import_lines(lines)
    assert [name for name, _ in roots] == ["runpy"]
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
    assert "[fspack] 启动耗时剖析（总 " in captured.out
    assert "退出码 0" in captured.out
    assert "─" * 30 in captured.out
    # 条形图列存在（空槽 ░ 或实心 █）
    assert "░" in captured.out
    # loader 段：结构化展示（阶段名 + "耗时"词并入列展示，"loader 总"重组为
    # "loader 总（进入 Python）"）
    assert "read_entry" in captured.out
    assert "1.5ms" in captured.out
    assert "加载 python313.dll" in captured.out
    assert "16.4ms" in captured.out
    assert "loader 总（进入 Python）" in captured.out
    assert "18.9ms" in captured.out
    # wrapper 段：环境准备 = env_ready 累计值；用户入口执行 = done - start；
    # 无 entry_start 后 importtime 行 → 入口执行细分只剩"其余执行"行
    assert "环境准备" in captured.out
    assert "2.0ms" in captured.out
    assert "用户入口执行" in captured.out
    assert "2.5ms" in captured.out
    assert "其余执行" in captured.out
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
    # -S 跳过 site 使导入链干净（venv 的 site 链会挤满模块自身 top10，
    # 且 json/csv 偶被 site 预载不产生 importtime 行，见历史会话）。
    # pickle 的 self 耗时（~0.8ms）稳定位居前三，不受运行波动挤出 top10
    # （csv 排名第 9 与第 10 名接近，曾被挤出导致断言偶发失败）
    code = "import pickle\nprint('done')\n"
    rc = run_with_profile([sys.executable, "-S", "-c", code], env=env)
    assert rc == 0
    captured = capsys.readouterr()
    assert "解释器初始化(约)" in captured.out
    # import pickle 是真实顶层导入，应出现在汇总中（self 耗时稳定进 top10）
    assert "pickle" in captured.out
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


def test_run_with_profile_parent_side_tail_measurement(capsys: pytest.CaptureFixture[str]) -> None:
    """真实子进程：末行 stderr 后 sleep 拉大收尾 gap，父进程侧实测拆分生效.

    子进程打完 wrapper 打点行后 sleep 0.3s 再退出：已归因仅 entry_done
    累计（毫秒级）→ gap 超 100ms 阈值展示拆分；末行到达后的 ~300ms 落入
    tail（进程收尾实测），python 启动到首行到达落入 head。验证 readline
    逐行时间戳在真实管道下工作（迭代器批量预读会使首/末行时刻失真）。
    """
    code = (
        "import sys, time\n"
        "sys.stderr.write('[fspack timing] env_ready @1.0ms\\n')\n"
        "sys.stderr.write('[fspack timing] entry_start @2.0ms\\n')\n"
        "sys.stderr.write('[fspack timing] entry_done @3.0ms\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(0.3)\n"
    )
    data: dict[str, Any] = {}
    rc = run_with_profile([sys.executable, "-c", code], on_summary=data.update)
    assert rc == 0
    out = capsys.readouterr().out
    assert "进程创建与映像加载(约)" in out
    assert "进程收尾(约)" in out
    # 末行（entry_done 打点）到达后 sleep ~300ms 全部计入 tail；head 覆盖
    # python 解释器启动到首行写出（>0）
    assert data["gap_breakdown"]["tail_ms"] > 200.0
    assert data["gap_breakdown"]["head_ms"] > 0.0
    assert data["gap_breakdown"]["blind_ms"] >= 0.0


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

    def fake_profile(cmd: list[str], env: dict[str, str] | None = None, on_summary: object = None) -> int:
        captured["cmd"] = cmd
        captured["env"] = env
        return 0

    monkeypatch.setattr("fspack.runner.run_with_profile", fake_profile)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    run_run(project, options=_opts(profile=True))
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

    def fake_profile(cmd: list[str], env: dict[str, str] | None = None, on_summary: object = None) -> int:
        captured["cmd"] = cmd
        captured["env"] = env
        return 0

    monkeypatch.setattr("fspack.runner.run_with_profile", fake_profile)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    run_run(tmp_path, options=_opts(debug=True, profile=True))
    assert captured["cmd"] == [str(dist / "runtime" / "python.exe"), str(dist / "_entry_app.py")]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["FSPACK_TIMING"] == "1"
    assert env["PYTHONPROFILEIMPORTTIME"] == "1"


def test_run_profile_nonzero_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """profile 模式非零退出码与普通模式一致抛 FspackError."""
    project = _make_runnable_project(tmp_path)

    monkeypatch.setattr("fspack.runner.run_with_profile", lambda cmd, env=None, on_summary=None: 7)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    from fspack.exceptions import FspackError

    with pytest.raises(FspackError, match="程序退出码非零: 7"):
        run_run(project, options=_opts(profile=True))


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


def test_fmt_bar() -> None:
    """_fmt_bar 按 12 格映射占比：整格 █ + 半格 ▓ + 空槽 ░，超界 clamp."""
    # 零耗时 → 全空槽
    assert _fmt_bar(0.0, 100.0) == "░" * 12
    # 50% → 恰 6.0 整格，无余数不补 ▓
    assert _fmt_bar(50.0, 100.0) == "██████░░░░░░"
    # 54% → 6.48 格，余 0.48 < 0.5 无 ▓
    assert _fmt_bar(54.0, 100.0) == "██████░░░░░░"
    # 58% → 6.96 格，余 0.96 ≥ 0.5 补 ▓
    assert _fmt_bar(58.0, 100.0) == "██████▓░░░░░"
    # 满格与超界 clamp
    assert _fmt_bar(100.0, 100.0) == "█" * 12
    assert _fmt_bar(150.0, 100.0) == "█" * 12
    # wall 非正（防御）→ 全空槽
    assert _fmt_bar(10.0, 0.0) == "░" * 12


def test_print_summary_entry_breakdown(capsys: pytest.CaptureFixture[str]) -> None:
    """入口执行段细分：entry_start 后根导入归"导入合计"，差额归"其余执行".

    直接调用私有 _print_summary 注入构造数据（故障注入例外，避免真实
    秒级 wall time 起子进程）。
    """
    timing = {"env_ready": 5.0, "entry_start": 10.0, "entry_done": 110.0}
    post = [
        "import time:     5000 |     5000 | argparse",
        "import time:     3000 |     3000 | pathlib",
        "import time:     1000 |     1000 | shutil",
    ]
    _print_summary(ProfileSample(wall_ms=150.0, timing_stages=timing, post_entry_lines=post))
    out = capsys.readouterr().out
    # 入口执行 = 110 - 10 = 100ms，导入合计 = 5+3+1 = 9ms，其余执行 = 91ms
    assert "用户入口执行" in out
    assert "100.0ms" in out
    assert "导入合计 top3" in out
    assert "9.0ms" in out
    assert "其余执行" in out
    assert "91.0ms" in out
    # 导入子项逐条展示且缩进 2 列展示层级（名称列 12 空格前缀 + 2；
    # argparse 同时出现在模块自身段（12 前缀），须匹配入口细分段行）
    argparse_line = next(ln for ln in out.splitlines() if ln.startswith(" " * 14) and "argparse" in ln)
    assert "5.0ms" in argparse_line
    # entry_start 前无 importtime 行 → 无解释器初始化/顶层导入段
    assert "解释器初始化" not in out
    # gap = 150 - max(110, 5) = 40ms < 100ms 阈值，无未细分行
    assert "未细分" not in out


def test_print_summary_post_lines_without_entry_start_merged(capsys: pytest.CaptureFixture[str]) -> None:
    """防御：无 entry_start 打点却有分界数据（理论不可达）时并回主列表."""
    timing = {"env_ready": 5.0}
    post = ["import time:     3000 |     3000 | json"]
    _print_summary(
        ProfileSample(
            wall_ms=100.0,
            timing_stages=timing,
            import_lines=["import time: 1000 | 1000 | encodings"],
            post_entry_lines=post,
        )
    )
    out = capsys.readouterr().out
    # 并回后无 runpy 锚点 → 全部根导入归解释器初始化段；json 经模块自身段可见
    assert "解释器初始化(约)" in out
    assert "json" in out
    # 无入口执行细分段
    assert "导入合计" not in out
    assert "其余执行" not in out


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
        "import time:       100 |       100 | runpy",
        "import time:   1437300 |  1437300 | app.controllers.app_controller",
        "import time:     66000 |     66000 | natsort.natsort",
    ]
    _print_summary(
        ProfileSample(
            wall_ms=2000.0,
            loader_stages=[("loader 总", 10.0, "（进入 Python）")],
            timing_stages=timing,
            import_lines=imports,
        )
    )
    out = capsys.readouterr().out
    # 表头与分隔线（分隔线长度与五列表格总宽一致）
    assert "[fspack] 启动耗时剖析（总 2000ms，退出码 0）" in out
    assert "─" * 78 in out
    # 条形图：1437.3/2000 = 71.9% → 8.6 格 → 8 实心 + 半格 ▓
    assert "████████▓" in out
    # loader 行结构化：阶段名 + 耗时 + 占比
    assert "loader 总（进入 Python）" in out
    assert "10.0ms" in out
    assert "0.5%" in out
    # 顶层导入子项行：名称 + cumulative + 占比（1437.3ms / 2000ms = 71.9%）
    assert "app.controllers.app_controller" in out
    assert "1437.3ms" in out
    assert "71.9%" in out
    # 模块自身耗段子项（glob 0.06ms 低于阈值被过滤，剩 5 条含 runpy 0.1ms）
    assert "模块自身耗时 top5" in out
    assert "natsort.natsort" in out
    assert "66.0ms" in out
    # wrapper 段
    assert "环境准备" in out
    assert "50.0ms" in out
    assert "用户入口执行" in out
    assert "49.0ms" in out


def test_print_summary_gap_old_dist_hint(capsys: pytest.CaptureFixture[str]) -> None:
    """旧 dist（无 wrapper 打点）大 gap 显示未细分行与重新构建提示."""
    _print_summary(
        ProfileSample(
            wall_ms=7000.0,
            loader_stages=[("read_entry", 0.0, ""), ("loader 总", 0.0, "（进入 Python）")],
        )
    )
    out = capsys.readouterr().out
    assert "未细分" in out
    assert "旧 dist 无 wrapper 打点" in out
    assert "重新 fsp b" in out
    assert "~" in out


def test_print_summary_gap_teardown_new_dist(capsys: pytest.CaptureFixture[str]) -> None:
    """新 dist 末端大 gap 归因为进程收尾，不提示重新构建."""
    timing = {"env_ready": 5.0, "entry_start": 6.0, "entry_done": 5000.0}
    _print_summary(ProfileSample(wall_ms=7000.0, loader_stages=[("loader 总", 10.0, "")], timing_stages=timing))
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
    _print_summary(ProfileSample(wall_ms=5000.0, timing_stages=timing))
    out = capsys.readouterr().out
    assert "未细分" in out
    assert "入口执行未完成打点" in out
    assert "未返回" in out


def test_print_summary_small_gap_hidden(capsys: pytest.CaptureFixture[str]) -> None:
    """低于阈值的小 gap 不显示未细分行，避免噪音."""
    timing = {"env_ready": 5.0, "entry_start": 6.0, "entry_done": 5950.0}
    _print_summary(ProfileSample(wall_ms=6000.0, loader_stages=[("loader 总", 10.0, "")], timing_stages=timing))
    out = capsys.readouterr().out
    assert "未细分" not in out


def test_print_summary_fast_program_high_ratio_gap_shown(capsys: pytest.CaptureFixture[str]) -> None:
    """快程序小绝对值大占比 gap：显示并归因为进程创建与收尾等外部开销.

    wall 64ms、已归因 26.4ms（entry_done 累计）→ gap ~38ms 占比 ~59%
    超 30% 阈值：展示"进程创建与收尾(约)"（子进程创建/杀毒扫描/解释器
    退出不在任何打点段内），回答快程序的"时间去哪了"。
    """
    timing = {"env_ready": 0.1, "entry_start": 0.2, "entry_done": 26.4}
    _print_summary(ProfileSample(wall_ms=64.0, timing_stages=timing))
    out = capsys.readouterr().out
    assert "未细分" in out
    assert "进程创建与收尾(约)" in out
    assert "外部开销" in out
    assert "~38ms" in out


def test_print_summary_fast_program_low_ratio_gap_hidden(capsys: pytest.CaptureFixture[str]) -> None:
    """快程序小绝对值小占比 gap：仍隐藏（控制噪声）."""
    timing = {"env_ready": 0.1, "entry_start": 0.2, "entry_done": 60.0}
    _print_summary(ProfileSample(wall_ms=64.0, timing_stages=timing))
    out = capsys.readouterr().out
    assert "未细分" not in out


# ---- _print_summary 返回值：结构化剖析数据（落盘用） ----


def test_print_summary_returns_structured_data(capsys: pytest.CaptureFixture[str]) -> None:
    """返回结构化数据：主阶段/loader 原文阶段名/顶层与入口导入列表（毫秒）."""
    timing = {"env_ready": 5.0, "entry_start": 10.0, "entry_done": 110.0}
    imports = [
        "import time:      100 |      150 | encodings",
        "import time:       700 |       760 | runpy",
        "import time:       500 |      2000 | app.controllers",
    ]
    post = ["import time:     5000 |     5000 | argparse"]
    data = _print_summary(
        ProfileSample(
            wall_ms=150.0,
            loader_stages=[("loader 总", 8.0, "（进入 Python）")],
            timing_stages=timing,
            import_lines=imports,
            post_entry_lines=post,
        )
    )

    assert data["wall_ms"] == 150.0
    assert data["returncode"] == 0
    # 主阶段：loader 原文阶段名（不含 suffix）→ 环境准备 → 解释器初始化 → 用户入口执行
    assert ("loader 总", 8.0) in data["stages"]
    assert ("环境准备", 5.0) in data["stages"]
    assert ("解释器初始化(约)", 0.15) in data["stages"]
    assert ("用户入口执行", 100.0) in data["stages"]
    # 顶层导入（runpy 锚点后）与入口执行期间导入分开存放
    assert [n for n, _ in data["top_imports"]] == ["runpy", "app.controllers"]
    assert data["top_imports"][1] == ("app.controllers", 2.0)
    assert data["entry_imports"] == [("argparse", 5.0)]
    # 模块自身耗时列表存在（self 降序）
    assert ("argparse", 5.0) in data["top_self"]


def test_print_summary_returns_empty_stages_without_marks(capsys: pytest.CaptureFixture[str]) -> None:
    """无任何打点时返回空阶段与空导入列表（结构完整，值全空）."""
    data = _print_summary(ProfileSample(wall_ms=10.0))
    assert data == {
        "wall_ms": 10.0,
        "returncode": 0,
        "stages": [],
        "top_imports": [],
        "entry_imports": [],
        "top_self": [],
    }


def test_print_summary_gap_parent_side_breakdown(capsys: pytest.CaptureFixture[str]) -> None:
    """有首/末行实测时未细分拆为三行：进程创建与映像加载/进程收尾/其余盲区.

    复刻用户实测场景：wall 91ms、entry_done 累计 41ms → gap ~50ms 占比
    55% 超 30% 阈值展示；首行 25ms（含 read_entry 2ms，扣除后 head 23ms）、
    末行 42ms（tail 49ms）、blind = 50 - 23 - 49 < 0 归零不展示。
    """
    timing = {"env_ready": 0.1, "entry_start": 0.2, "entry_done": 41.0}
    data = _print_summary(
        ProfileSample(
            wall_ms=91.0,
            loader_stages=[("read_entry", 2.0, ""), ("loader 总", 3.0, "（进入 Python）")],
            timing_stages=timing,
            first_line_ms=25.0,
            last_line_ms=42.0,
        )
    )
    out = capsys.readouterr().out
    # 头部 = 首行 25ms - 首 loader 阶段 2ms（read_entry 已单独归因）
    assert "进程创建与映像加载(约)" in out
    assert "~23ms" in out
    assert "子进程创建/杀软扫描/映像加载/管道延迟" in out
    # 尾部 = wall 91 - 末行 42
    assert "进程收尾(约)" in out
    assert "~49ms" in out
    assert "解释器退出/管道 EOF/父进程调度" in out
    # blind = gap(50) - head(23) - tail(49) < 0 → clamp 0，不展示该行
    assert "其余盲区" not in out
    # 落盘拆分数据（毫秒）
    assert data["gap_breakdown"] == {"head_ms": 23.0, "tail_ms": 49.0, "blind_ms": 0.0}


def test_print_summary_gap_parent_side_breakdown_with_blind(capsys: pytest.CaptureFixture[str]) -> None:
    """首尾之和小于 gap 时展示其余盲区行（残余无打点段与测量噪声）."""
    timing = {"env_ready": 1.0, "entry_start": 2.0, "entry_done": 10.0}
    data = _print_summary(
        ProfileSample(
            wall_ms=200.0,
            loader_stages=[("loader 总", 5.0, "")],
            timing_stages=timing,
            first_line_ms=30.0,
            last_line_ms=50.0,
        )
    )
    out = capsys.readouterr().out
    # gap = 200 - (loader 5 + 0 + max(10, 1)) = 185；head = 30 - 首 loader 阶段
    # 5（已归因）= 25；tail = 200 - 50 = 150；blind = 185 - 25 - 150 = 10
    assert "其余盲区(约)" in out
    assert "~10ms" in out
    assert "残余无打点段" in out
    assert data["gap_breakdown"] == {"head_ms": 25.0, "tail_ms": 150.0, "blind_ms": 10.0}


def test_run_with_profile_collects_timing_gap_lines(capsys: pytest.CaptureFixture[str]) -> None:
    """timing-gap 行（py_init 实测）被收集不透传，汇总展示并从 gap 扣除."""
    code = (
        "import sys\n"
        "sys.stderr.write('[fspack loader] loader 总耗时 3.2ms（进入 Python）\\n')\n"
        "sys.stderr.write('[fspack timing-gap] py_init 8.5ms\\n')\n"
        "sys.stderr.write('[fspack timing] env_ready @0.1ms\\n')\n"
        "sys.stderr.write('[fspack timing] entry_start @0.2ms\\n')\n"
        "sys.stderr.write('[fspack timing] entry_done @40.0ms\\n')\n"
        "sys.stderr.flush()\n"
    )
    data: dict[str, Any] = {}
    rc = run_with_profile([sys.executable, "-c", code], on_summary=data.update)
    assert rc == 0
    captured = capsys.readouterr()
    out = captured.out
    # py_init 实测段展示在 loader 段后
    assert "C 层初始化(实测)" in out
    assert "8.5ms" in out
    # 标记行不透传
    assert "[fspack timing-gap]" not in captured.err
    # 落盘 stages 含实测段
    assert ("C 层初始化(实测)", 8.5) in data["stages"]


def test_print_summary_py_init_narrows_gap(capsys: pytest.CaptureFixture[str]) -> None:
    """py_init 实测参与 gap 扣除：其余盲区相应收窄."""
    timing = {"env_ready": 1.0, "entry_start": 2.0, "entry_done": 10.0}
    sample_no_gap = ProfileSample(
        wall_ms=200.0,
        loader_stages=[("loader 总", 5.0, "")],
        timing_stages=timing,
        first_line_ms=30.0,
        last_line_ms=50.0,
    )
    sample_with_gap = ProfileSample(
        wall_ms=200.0,
        loader_stages=[("loader 总", 5.0, "")],
        timing_stages=timing,
        gap_stages={"py_init": 8.0},
        first_line_ms=30.0,
        last_line_ms=50.0,
    )
    data_no = _print_summary(sample_no_gap)
    out_no = capsys.readouterr().out
    data_with = _print_summary(sample_with_gap)
    out_with = capsys.readouterr().out
    # 无 py_init：blind = 185 - 25 - 150 = 10；有 py_init(8)：gap = 177，
    # blind = 177 - 25 - 150 = 2
    assert "~10ms" in out_no
    assert "~2ms" in out_with
    assert data_with["gap_breakdown"]["blind_ms"] == 2.0
    assert data_no["gap_breakdown"]["blind_ms"] == 10.0


def test_print_summary_gap_below_threshold_no_breakdown_data(capsys: pytest.CaptureFixture[str]) -> None:
    """gap 低于展示阈值时不拆分也不落盘 gap_breakdown（控制噪声）."""
    timing = {"env_ready": 5.0, "entry_start": 6.0, "entry_done": 5950.0}
    data = _print_summary(
        ProfileSample(
            wall_ms=6000.0,
            loader_stages=[("loader 总", 10.0, "")],
            timing_stages=timing,
            first_line_ms=80.0,
            last_line_ms=5960.0,
        )
    )
    out = capsys.readouterr().out
    assert "未细分" not in out
    assert "gap_breakdown" not in data


# ---- runner.run 落盘与对比集成（mock run_with_profile） ----


def test_run_saves_profile_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """profile=True 时剖析数据落盘 .benchmarks/fsp-r-*.json（run schema）."""
    from fspack.packaging.profile_log import RUN_PROFILE_LOG_SCHEMA, load_profile_log

    project = _make_runnable_project(tmp_path)

    def fake_profile(
        cmd: list[str],
        env: dict[str, str] | None = None,
        on_summary: Callable[[dict[str, Any]], None] | None = None,
    ) -> int:
        assert callable(on_summary)
        on_summary(
            {
                "wall_ms": 52.0,
                "returncode": 0,
                "stages": [("loader 总", 8.0), ("环境准备", 5.0), ("用户入口执行", 25.0)],
                "top_imports": [("runpy", 0.7)],
                "entry_imports": [("argparse", 9.0)],
                "top_self": [("argparse", 3.2)],
                "gap_breakdown": {"head_ms": 12.0, "tail_ms": 6.5, "blind_ms": 0.5},
            }
        )
        return 0

    monkeypatch.setattr("fspack.runner.run_with_profile", fake_profile)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    run_run(project, options=_opts(profile=True))

    logs = sorted((project / ".benchmarks").glob("fsp-r-*.json"))
    assert len(logs) == 1
    data = load_profile_log(logs[0])
    assert data["schema"] == RUN_PROFILE_LOG_SCHEMA
    assert data["project"] == {"name": "app", "version": "0.0.0"}
    assert data["entry"] == "app"
    assert data["debug"] is False
    assert data["wall_time"] == 0.052
    assert data["returncode"] == 0
    assert {"name": "环境准备", "elapsed": 0.005} in data["stages"]
    assert {"name": "runpy", "elapsed": 0.0007} in data["top_imports"]
    assert {"name": "argparse", "elapsed": 0.009} in data["entry_imports"]
    assert {"name": "argparse", "elapsed": 0.0032} in data["top_self"]
    # gap_breakdown 落盘同步转秒（4 位小数）
    assert data["gap_breakdown"] == {"head_ms": 0.012, "tail_ms": 0.0065, "blind_ms": 0.0005}


def test_run_profile_compare_last_renders_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """profile_compare="last" 与最近一次启动剖析日志对比：渲染差异表格."""
    project = _make_runnable_project(tmp_path)

    def fake_profile(
        cmd: list[str],
        env: dict[str, str] | None = None,
        on_summary: Callable[[dict[str, Any]], None] | None = None,
    ) -> int:
        if on_summary is not None:
            on_summary(
                {
                    "wall_ms": 80.0,
                    "returncode": 0,
                    "stages": [("环境准备", 12.0), ("用户入口执行", 30.0)],
                    "top_imports": [],
                    "entry_imports": [],
                    "top_self": [],
                }
            )
        return 0

    monkeypatch.setattr("fspack.runner.run_with_profile", fake_profile)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    # 预置历史启动剖析日志（比本次慢 30ms，环境准备慢 8ms 显著）
    from fspack.packaging.profile_log import save_profile_log

    baseline_data = {
        "schema": "fspack/run-profile/1",
        "created": "2026-08-24T09:00:00",
        "project": {"name": "app", "version": "0.0.0"},
        "python": "3.13.14",
        "platform": "windows",
        "entry": "app",
        "debug": False,
        "wall_time": 0.11,
        "returncode": 0,
        "stages": [{"name": "环境准备", "elapsed": 0.02}, {"name": "用户入口执行", "elapsed": 0.032}],
    }
    save_profile_log(baseline_data, project / ".benchmarks", prefix="fsp-r-")

    # 对比表经 fspack.console 的 rich console 输出，须用 rich capture 捕获
    from fspack.console import console

    with console.rich.capture() as capture:
        run_run(project, options=_opts(profile=True, profile_compare="last"))
    out = capture.get()
    assert "性能对比" in out
    assert "墙钟时间" in out
    # 80ms vs 110ms = -27.3% ▼ 改善
    assert "-27.3%" in out
    assert "▼" in out
    # 环境准备 12ms vs 20ms：差 8ms 超 5ms 阈值且 -40% 显著列入
    assert "环境准备" in out


def test_run_profile_compare_last_without_history_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """profile_compare="last" 无历史日志时警告跳过，不报错."""

    def fake_profile(
        cmd: list[str],
        env: dict[str, str] | None = None,
        on_summary: Callable[[dict[str, Any]], None] | None = None,
    ) -> int:
        if on_summary is not None:
            on_summary(
                {
                    "wall_ms": 80.0,
                    "returncode": 0,
                    "stages": [],
                    "top_imports": [],
                    "entry_imports": [],
                    "top_self": [],
                }
            )
        return 0

    monkeypatch.setattr("fspack.runner.run_with_profile", fake_profile)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    project = _make_runnable_project(tmp_path)
    with caplog.at_level("WARNING"):
        run_run(project, options=_opts(profile=True, profile_compare="last"))
    assert any("未找到可对比的历史启动剖析日志" in r.message for r in caplog.records)


def test_run_profile_specified_baseline_schema_mismatch_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """指定基准为构建日志（schema 不一致）时警告跳过对比."""

    def fake_profile(
        cmd: list[str],
        env: dict[str, str] | None = None,
        on_summary: Callable[[dict[str, Any]], None] | None = None,
    ) -> int:
        if on_summary is not None:
            on_summary(
                {
                    "wall_ms": 80.0,
                    "returncode": 0,
                    "stages": [],
                    "top_imports": [],
                    "entry_imports": [],
                    "top_self": [],
                }
            )
        return 0

    monkeypatch.setattr("fspack.runner.run_with_profile", fake_profile)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    project = _make_runnable_project(tmp_path)
    # 预置一个构建日志（fsp-b 前缀 + build schema）
    build_log = project / ".benchmarks" / "fsp-b-20260824-100000.json"
    build_log.parent.mkdir(parents=True, exist_ok=True)
    build_log.write_text('{"schema": "fspack/build-profile/1", "wall_time": 3.0, "stages": []}', encoding="utf-8")
    with caplog.at_level("WARNING"):
        run_run(project, options=_opts(profile=True, profile_compare=str(build_log)))
    assert any("类型不一致" in r.message for r in caplog.records)
