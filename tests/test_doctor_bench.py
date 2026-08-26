"""doctor/bench.py 测试：机器指纹、剖析日志落盘与历史基准对比."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from fspack.console import console
from fspack.doctor import (
    TemplateBuildResult,
    TemplateRunResult,
    _bench_profile_log_data,
    _collect_machine_info,
    _machine_id,
    _save_and_compare_bench,
)
from fspack.packaging.profile_log import DOCTOR_PROFILE_LOG_SCHEMA, ProfileOptions


@pytest.fixture(autouse=True)
def _fixed_rich_width(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """固定 Rich Console 宽度，避免窄终端环境下 word wrap 导致断言失败.

    多个测试渲染含 8 列的 Rich Table（bench 模式汇总表），窄终端（width<80）下
    Rich 会截断长文本（如 ``ModuleNotFoundError`` → ``ModuleNot…``）或丢弃列，
    导致断言偶发失败。固定 width=200 确保所有环境渲染一致。

    必须直接 patch ``_width`` 而非走 ``width`` 属性往返：rich 的 ``width`` getter
    在 ``_width``/``_height`` 均已设置时（shell 导出 ``COLUMNS``/``LINES`` 环境变量
    即如此）返回 ``_width - legacy_windows``，而 setter 存原始值——往返一次宽度净
    减 1（legacy Windows 控制台）。本文件数百个测试逐个缩水，跑完后宽度变负数，
    rich 会把后续所有文本裁剪为空，殃及后续文件 27 个 capsys 断言。
    ``monkeypatch`` 记录的是原始 ``_width`` 值，恢复无损。
    """
    monkeypatch.setattr(console.rich, "_width", 200)
    yield


# ---- 基准剖析日志聚合落盘与历史对比 ----


def test_bench_profile_log_data_structure() -> None:
    """聚合日志结构：成功模板进 stages（含扩展字段），失败模板单列 failures."""
    rr = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.5)
    results = [
        TemplateBuildResult(
            template_id="tpl_a",
            success=True,
            duration_sec=12.5,
            dist_size=102400,
            entry_count=1,
            run_result=rr,
        ),
        TemplateBuildResult(template_id="tpl_b", success=False, duration_sec=0.1, error="构建失败"),
        TemplateBuildResult(template_id="tpl_c", success=True, duration_sec=3.0, dist_size=2048),
    ]
    data = _bench_profile_log_data(results, wall_time=16.0)

    assert data["schema"] == DOCTOR_PROFILE_LOG_SCHEMA
    assert data["wall_time"] == 16.0
    assert data["project"]["name"] == "doctor-bench"
    assert data["python"] == sys.version.split()[0]
    # stages 仅含成功模板；扩展字段保留（对比渲染只读 elapsed，扩展供事后分析）
    assert [s["name"] for s in data["stages"]] == ["tpl_a", "tpl_c"]
    assert data["stages"][0]["elapsed"] == 12.5
    assert data["stages"][0]["dist_size"] == 102400
    assert data["stages"][0]["run_duration_sec"] == 0.5
    assert data["stages"][1]["run_duration_sec"] is None  # 无运行验证
    # 失败模板单列 failures（中断样本不混入阶段统计）
    assert data["failures"] == [{"template_id": "tpl_b", "error": "构建失败"}]
    # 匿名机器信息
    assert len(data["machine"]["node_id"]) == 8
    # JSON 可序列化
    json.dumps(data, ensure_ascii=False)


def test_bench_profile_log_data_no_failures_omits_field() -> None:
    """全部成功时省略 failures 字段."""
    results = [TemplateBuildResult(template_id="x", success=True, duration_sec=1.0, dist_size=100)]
    data = _bench_profile_log_data(results, wall_time=1.0)
    assert "failures" not in data


def test_machine_id_is_deterministic_and_anonymous() -> None:
    """_machine_id 返回 8 位 hex 编码，同一进程内确定性，不含真实机器名."""
    id1 = _machine_id()
    id2 = _machine_id()
    assert id1 == id2  # 确定性
    assert len(id1) == 8  # 8 位
    # 仅含 hex 字符
    import re

    assert re.match(r"^[0-9a-f]{8}$", id1)
    # 不含真实机器名
    import platform

    assert platform.node() not in id1


def test_collect_machine_info_no_privacy() -> None:
    """_collect_machine_info 不含真实机器名（隐私），含 CPU 性能配置."""
    info = _collect_machine_info()
    import platform

    # 不含真实机器名
    assert "node" not in info
    if platform.node():
        assert platform.node() not in str(info)
    # 含匿名编码
    assert "node_id" in info
    # 含 CPU 性能配置
    assert info["cpu"]["count"] > 0
    assert info["cpu"]["bits"] in (32, 64)


def _write_bench_history(
    bench_dir: Path,
    results: list[TemplateBuildResult],
    wall_time: float,
    name: str,
) -> Path:
    """写入一份历史基准剖析日志（与当前同 schema/环境字段，环境一致可参与统计）."""
    data = _bench_profile_log_data(results, wall_time)
    path = bench_dir / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_save_and_compare_bench_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """-P 首次运行无历史：保存聚合日志到 .benchmarks/fsp-d-*.json，无对比."""
    monkeypatch.chdir(tmp_path)
    results = [TemplateBuildResult(template_id="x", success=True, duration_sec=10.0, dist_size=100)]

    _save_and_compare_bench(results, 10.5, ProfileOptions(enabled=True))
    out = capsys.readouterr().out

    assert "基准剖析日志已保存" in out
    logs = list((tmp_path / ".benchmarks").glob("fsp-d-*.json"))
    assert len(logs) == 1
    data = json.loads(logs[0].read_text(encoding="utf-8"))
    assert data["schema"] == DOCTOR_PROFILE_LOG_SCHEMA
    assert data["wall_time"] == 10.5
    # 无 -PC 不渲染对比
    assert "性能对比" not in out
    assert "趋势" not in out


def test_save_and_compare_bench_compare_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """-PC last 与最近一次历史日志对比：模板阶段差异显著项列入差异表."""
    monkeypatch.chdir(tmp_path)
    bench_dir = tmp_path / ".benchmarks"
    bench_dir.mkdir()
    prev = [TemplateBuildResult(template_id="x", success=True, duration_sec=10.0, dist_size=100)]
    _write_bench_history(bench_dir, prev, 10.5, "fsp-d-20260101-000000.json")

    cur = [TemplateBuildResult(template_id="x", success=True, duration_sec=12.0, dist_size=100)]
    _save_and_compare_bench(cur, 12.5, ProfileOptions(enabled=True, compare="last"))
    out = capsys.readouterr().out

    assert "性能对比" in out
    assert "x" in out  # 模板阶段名
    assert "+2.00s" in out  # 模板耗时 12 vs 10，变慢
    # 两个日志文件（历史 + 本次）
    assert len(list(bench_dir.glob("fsp-d-*.json"))) == 2


def test_save_and_compare_bench_trend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """-PC 不带值输出趋势表：历史 + 本次 + 中位数统计 + 阶段偏离."""
    monkeypatch.chdir(tmp_path)
    bench_dir = tmp_path / ".benchmarks"
    bench_dir.mkdir()
    prev = [TemplateBuildResult(template_id="x", success=True, duration_sec=10.0, dist_size=100)]
    _write_bench_history(bench_dir, prev, 10.5, "fsp-d-20260101-000000.json")

    cur = [TemplateBuildResult(template_id="x", success=True, duration_sec=12.0, dist_size=100)]
    _save_and_compare_bench(cur, 12.5, ProfileOptions(enabled=True, compare="trend"))
    out = capsys.readouterr().out

    assert "基准剖析趋势" in out
    assert "中位数" in out
    # 模板阶段显著偏离（12 vs 10）渲染偏离表
    assert "基准剖析阶段偏离" in out


def test_save_and_compare_bench_out_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """-PO 指定 .json 文件时直写（不落默认目录）。"""
    monkeypatch.chdir(tmp_path)
    out_file = tmp_path / "custom.json"
    results = [TemplateBuildResult(template_id="x", success=True, duration_sec=10.0, dist_size=100)]

    _save_and_compare_bench(results, 10.5, ProfileOptions(enabled=True, out=out_file))
    capsys.readouterr()

    assert out_file.is_file()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["schema"] == DOCTOR_PROFILE_LOG_SCHEMA
    # 默认目录未创建（-PO 已接管输出位置）
    assert not (tmp_path / ".benchmarks").exists()


def test_save_and_compare_bench_compare_last_no_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """-PC last 无历史日志时警告跳过对比，保存不中断."""
    monkeypatch.chdir(tmp_path)
    results = [TemplateBuildResult(template_id="x", success=True, duration_sec=12.0, dist_size=100)]

    # compare=last 但 .benchmarks 无历史：警告后跳过对比（日志仍正常保存）
    _save_and_compare_bench(results, 12.5, ProfileOptions(enabled=True, compare="last"))
    capsys.readouterr()

    logs = list((tmp_path / ".benchmarks").glob("fsp-d-*.json"))
    assert len(logs) == 1
