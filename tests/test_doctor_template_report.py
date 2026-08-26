"""doctor/template_report.py 测试：构建汇总渲染、运行列、性能分析与结果数据模型."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from fspack.console import console
from fspack.doctor import (
    TemplateBuildResult,
    TemplateRunResult,
    _format_run_status,
    _print_performance_analysis,
    _print_template_build_summary,
)


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


# ---- TemplateBuildResult / 模板构建汇总 ----


def test_template_build_result_frozen() -> None:
    """TemplateBuildResult 是 frozen dataclass，不可变."""
    r = TemplateBuildResult(template_id="test", success=True, duration_sec=1.0)
    with pytest.raises(AttributeError):
        r.success = False  # type: ignore[misc]


def test_template_build_result_defaults() -> None:
    """TemplateBuildResult 默认值：error/dist_size/entry_count 为零值."""
    r = TemplateBuildResult(template_id="test", success=True, duration_sec=1.0)
    assert r.error == ""
    assert r.dist_size == 0
    assert r.entry_count == 0


def test_print_template_build_summary_all_success(capsys: pytest.CaptureFixture[str]) -> None:
    """全部成功时汇总输出含"全部成功"与总耗时."""
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.5, dist_size=1024, entry_count=1),
        TemplateBuildResult(template_id="b", success=True, duration_sec=2.5, dist_size=2048, entry_count=1),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "全部成功" in out
    assert "2/2" in out
    assert "4.0s" in out  # 1.5 + 2.5


def test_print_template_build_summary_capability_tags(capsys: pytest.CaptureFixture[str]) -> None:
    """汇总表能力列：登记的模板 id 显示能力维度标签，未登记的显示 ``-``."""
    results = [
        TemplateBuildResult(template_id="sci_stack", success=True, duration_sec=1.0, dist_size=100, entry_count=1),
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "能力" in out  # 表头
    assert "科学计算栈/3.14t" in out
    # 未登记 id（如测试注入的合成 id）回退 "-"，不报错
    assert "二进制依赖" not in out


def test_capability_tag_all_doctor_templates_registered() -> None:
    """能力标签映射覆盖全部 doctor 模板：新模板加入 doctor 集时须同步登记标签.

    直接访问私有 ``_CAPABILITY_TAGS`` 做注册表对齐校验（公共接口无此暴露）。
    """
    from fspack.doctor.template_report import _CAPABILITY_TAGS
    from fspack.templates.project_template import ProjectTemplate

    doctor_ids = {tpl.id for tpl in ProjectTemplate.list_all()}
    tagged_ids = set(_CAPABILITY_TAGS)
    assert doctor_ids == tagged_ids, (
        f"能力标签与 doctor 模板集不一致：缺标签 {doctor_ids - tagged_ids}，多余标签 {tagged_ids - doctor_ids}"
    )


def test_print_template_build_summary_with_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """有失败时汇总输出含"失败"（错误信息仅在 bench 模式显示）."""
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1),
        TemplateBuildResult(template_id="b", success=False, duration_sec=0.5, error="网络超时"),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "1 成功" in out
    assert "1 失败" in out
    # 非 bench 模式无错误信息列，错误信息不输出
    assert "网络超时" not in out


def test_print_template_build_summary_with_failure_bench(capsys: pytest.CaptureFixture[str]) -> None:
    """bench 模式有失败时输出含错误信息."""
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1),
        TemplateBuildResult(template_id="b", success=False, duration_sec=0.5, error="网络超时"),
    ]
    _print_template_build_summary(results, bench=True)
    out = capsys.readouterr().out
    assert "1 失败" in out
    assert "网络超时" in out


def test_print_template_build_summary_bench_mode(capsys: pytest.CaptureFixture[str]) -> None:
    """bench 模式输出性能分析（耗时排名、产物大小排名）."""
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1),
        TemplateBuildResult(template_id="b", success=True, duration_sec=3.0, dist_size=500, entry_count=1),
        TemplateBuildResult(template_id="c", success=False, duration_sec=0.1, error="失败"),
    ]
    _print_template_build_summary(results, bench=True)
    out = capsys.readouterr().out
    assert "性能分析" in out
    assert "耗时排名" in out
    assert "产物大小排名" in out
    assert "最慢" in out
    assert "最大" in out


def test_print_performance_analysis_single_success(capsys: pytest.CaptureFixture[str]) -> None:
    """仅 1 个成功时不输出性能分析（需 >1 才排名）."""
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.0, dist_size=100),
    ]
    _print_performance_analysis(results)
    out = capsys.readouterr().out
    # 单个成功不输出分析表格
    assert "耗时排名" not in out


def test_print_performance_analysis_all_failed(capsys: pytest.CaptureFixture[str]) -> None:
    """全部失败时不输出性能分析."""
    results = [
        TemplateBuildResult(template_id="a", success=False, duration_sec=0.1, error="err1"),
        TemplateBuildResult(template_id="b", success=False, duration_sec=0.2, error="err2"),
    ]
    _print_performance_analysis(results)
    out = capsys.readouterr().out
    assert "耗时排名" not in out


# ---- TemplateRunResult 数据结构 ----


def test_template_run_result_frozen() -> None:
    """TemplateRunResult 是 frozen dataclass，不可变."""
    r = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.5)
    with pytest.raises(AttributeError):
        r.success = False  # type: ignore[misc]


def test_template_run_result_defaults() -> None:
    """TemplateRunResult 默认 error 为空字符串."""
    r = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.5)
    assert r.error == ""


def test_template_build_result_run_result_default_none() -> None:
    """TemplateBuildResult.run_result 默认 None（构建失败或未运行验证）."""
    r = TemplateBuildResult(template_id="t", success=True, duration_sec=1.0)
    assert r.run_result is None


# ---- _format_run_status ----


def test_format_run_status_build_failed() -> None:
    """构建失败时运行状态显示 '-'（不运行验证）."""
    r = TemplateBuildResult(template_id="t", success=False, duration_sec=0.1, error="构建错误")
    status, err = _format_run_status(r)
    assert status == "-"
    assert err == ""


def test_format_run_status_no_run_result() -> None:
    """构建成功但未运行验证（多入口或未找到 exe）显示 '跳过'."""
    r = TemplateBuildResult(template_id="t", success=True, duration_sec=1.0)
    status, err = _format_run_status(r)
    assert "跳过" in status
    assert err == ""


def test_format_run_status_success_no_timeout() -> None:
    """运行成功且未超时（CLI 正常退出码 0）显示 '√ 成功'."""
    rr = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.2)
    r = TemplateBuildResult(template_id="t", success=True, duration_sec=1.0, run_result=rr)
    status, err = _format_run_status(r)
    assert "成功" in status
    assert err == ""


def test_format_run_status_success_timeout() -> None:
    """运行成功但超时（GUI/Web 事件循环）显示 '√ 超时'."""
    rr = TemplateRunResult(success=True, timed_out=True, exit_code=None, duration_sec=5.0)
    r = TemplateBuildResult(template_id="t", success=True, duration_sec=1.0, run_result=rr)
    status, _ = _format_run_status(r)
    assert "超时" in status


def test_format_run_status_failure() -> None:
    """运行失败显示 '× 失败' 并返回 stderr 错误."""
    rr = TemplateRunResult(success=False, timed_out=False, exit_code=1, duration_sec=0.1, error="ModuleNotFoundError")
    r = TemplateBuildResult(template_id="t", success=True, duration_sec=1.0, run_result=rr)
    status, err = _format_run_status(r)
    assert "失败" in status
    assert err == "ModuleNotFoundError"


# ---- _print_template_build_summary 运行列 ----


def test_print_summary_run_column_success(capsys: pytest.CaptureFixture[str]) -> None:
    """汇总表含运行列：构建+运行均成功."""
    rr = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.2)
    results = [
        TemplateBuildResult(
            template_id="a", success=True, duration_sec=1.5, dist_size=1024, entry_count=1, run_result=rr
        ),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "运行" in out
    assert "全部成功" in out
    assert "运行验证" in out
    assert "全部通过" in out


def test_print_summary_run_column_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """汇总表含运行列：构建成功但运行失败，输出运行失败统计."""
    rr = TemplateRunResult(success=False, timed_out=False, exit_code=1, duration_sec=0.1, error="ImportError")
    results = [
        TemplateBuildResult(
            template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1, run_result=rr
        ),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "运行验证" in out
    assert "1 失败" in out
    assert "ImportError" not in out  # 非 bench 模式不显示错误信息列


def test_print_summary_run_column_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    """汇总表含运行列：构建成功但跳过运行验证（多入口）。"""
    results = [
        TemplateBuildResult(template_id="multi", success=True, duration_sec=1.0, dist_size=100, entry_count=2),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "跳过" in out
    assert "运行验证" in out


def test_print_summary_bench_shows_run_error(capsys: pytest.CaptureFixture[str]) -> None:
    """bench 模式下显示运行错误信息列."""
    rr = TemplateRunResult(
        success=False, timed_out=False, exit_code=1, duration_sec=0.1, error="ModuleNotFoundError: x"
    )
    results = [
        TemplateBuildResult(
            template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1, run_result=rr
        ),
        TemplateBuildResult(
            template_id="b",
            success=True,
            duration_sec=2.0,
            dist_size=200,
            entry_count=1,
            run_result=TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.1),
        ),
    ]
    _print_template_build_summary(results, bench=True)
    out = capsys.readouterr().out
    assert "ModuleNotFoundError" in out
    assert "运行验证" in out
    assert "1 失败" in out


def test_print_summary_bench_shows_startup_column(capsys: pytest.CaptureFixture[str]) -> None:
    """bench 模式汇总表含'启动耗时'列，显示应用调用响应速度."""
    rr = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.42)
    results = [
        TemplateBuildResult(
            template_id="tpl_a",
            success=True,
            duration_sec=10.0,
            dist_size=102400,
            entry_count=1,
            run_result=rr,
        ),
        TemplateBuildResult(template_id="tpl_b", success=True, duration_sec=5.0, dist_size=51200),
    ]
    _print_template_build_summary(results, bench=True)
    out = capsys.readouterr().out
    assert "启动耗时" in out
    assert "0.42s" in out  # tpl_a 的启动耗时
    assert "-" in out  # tpl_b 无 run_result，显示 -


def test_print_performance_analysis_includes_startup_ranking(capsys: pytest.CaptureFixture[str]) -> None:
    """性能分析含'启动耗时排名'表，按应用调用响应速度降序."""
    rr_a = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.8)
    rr_b = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.3)
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=10.0, dist_size=100, run_result=rr_a),
        TemplateBuildResult(template_id="b", success=True, duration_sec=8.0, dist_size=200, run_result=rr_b),
    ]
    _print_performance_analysis(results)
    out = capsys.readouterr().out
    assert "启动耗时排名" in out
    assert "应用调用响应速度" in out
    # a (0.8s) 最慢排第一，b (0.3s) 排第二
    assert "0.80s" in out
    assert "0.30s" in out
