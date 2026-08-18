"""init 模板冒烟矩阵测试：全部模板生成项目后执行 build dry-run 全链路.

护栏目标：拦截模板腐化——模板 ``pyproject.toml`` 格式错误、入口脚本缺失、
``requires-python`` 约束无法解析版本、``[tool.fspack]`` 配置非法等，在
模板改动或 fspack 解析逻辑回归时第一时间失败，而非等用户踩坑。

``dry_run=True`` 仅执行项目解析与依赖分析（AST 扫描），不下载运行时/
wheel、不编译 loader、不构建前端、不写盘，全平台可离线运行，因此纳入
常规测试套件（每次 ``make check`` / CI 都跑），无需单独的定期 job。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.cli_init import init_project
from fspack.config import get_mirror
from fspack.console import console
from fspack.packaging.pipeline import build
from fspack.platform import Platform
from fspack.templates import list_templates

# 全部 init 角色模板 id（当前 24 个），新增模板自动纳入冒烟矩阵
_INIT_TEMPLATE_IDS: tuple[str, ...] = tuple(t.id for t in list_templates(role="init"))


@pytest.mark.parametrize("template_id", _INIT_TEMPLATE_IDS)
def test_init_template_build_dry_run_smoke(tmp_path: Path, template_id: str) -> None:
    """每个 init 模板：init 生成项目 → build dry-run 无异常且不写盘.

    失败归因：参数化测试名含模板 id 定位问题模板；异常栈定位根因——
    ProjectError（pyproject 解析）为模板配置错，其余为 fspack 回归。
    """
    target = init_project(f"smoke-{template_id}", template_id=template_id, directory=tmp_path)
    with console.rich.capture():
        info = build(target, get_mirror("aliyun"), None, target=Platform.WINDOWS, dry_run=True)
    # dry-run 返回解析后的项目信息，名称与入口是模板渲染正确性的最小断言
    assert info.name == f"smoke-{template_id}"
    assert info.entry_file.is_file()
    # dry-run 不写盘：dist 目录不应被创建
    assert not (target / "dist").exists()


def test_smoke_matrix_covers_all_categories() -> None:
    """冒烟矩阵模板数不少于已注册 init 模板数，防止参数化列表意外缩水."""
    assert len(_INIT_TEMPLATE_IDS) >= 20
    # 各分类至少一个代表：cli/gui/game/sci/web/config
    assert "helloworld" in _INIT_TEMPLATE_IDS
    assert "pyside2" in _INIT_TEMPLATE_IDS
    assert "pygame" in _INIT_TEMPLATE_IDS
    assert "numpy" in _INIT_TEMPLATE_IDS
    assert "flask" in _INIT_TEMPLATE_IDS
    assert "multi-entry" in _INIT_TEMPLATE_IDS
