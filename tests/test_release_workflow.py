"""发布工作流多版本矩阵与项目配置的一致性护栏.

发布工作流（``.github/workflows/release.yml``）按「平台 × Python 版本」逐格打包，
矩阵是显式列表，无法从 ``pyproject.toml`` 直接读取。本测试把它与
``[tool.fspack] python-versions`` 及运行时资产表对齐：

- Windows 用 python.org embed，覆盖配置里的全部版本（当前 3.8–3.14）；
- Linux/macOS 用 python-build-standalone，只有 3.10–3.14
  （3.8/3.9 无上游运行时资产，只在 Windows 发布）。

同时校验构建/打包步骤显式传 ``--py-version``：若省略，fspack 会按配置的
多版本布局输出到 ``dist/py<版本>/``，后续的冒烟测试与产物断言路径全部失效。
"""

from __future__ import annotations

import re
from pathlib import Path

from fspack.config import KNOWN_STANDALONE_VERSIONS, ProjectInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _job_block(text: str, job: str) -> str:
    """截取 workflow 中某个 job 的 YAML 文本块（含其 steps）."""
    pattern = rf"^  {re.escape(job)}:\n(.*?)(?=^  [\w-]+:\n|\Z)"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"release.yml 缺少 job: {job}"
    return match.group(1)


def _matrix_versions(block: str) -> list[str]:
    """解析 job 块内 ``python-version: [...]`` 矩阵列表."""
    match = re.search(r"python-version:\s*\[([^\]]*)\]", block)
    assert match is not None, "矩阵缺少 python-version 列表"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_release_matrix_matches_project_config() -> None:
    """Windows 矩阵等于配置版本；Linux 矩阵等于有 standalone 资产的配置版本."""
    configured = list(ProjectInfo.from_dir(REPO_ROOT).build_defaults.python_versions)
    assert configured, "pyproject.toml 未配置 [tool.fspack] python-versions"

    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    windows = _matrix_versions(_job_block(text, "pack-windows"))
    linux = _matrix_versions(_job_block(text, "pack-linux"))

    assert windows == configured, "pack-windows 矩阵与 [tool.fspack] python-versions 不一致"
    assert linux == [v for v in configured if v in KNOWN_STANDALONE_VERSIONS], (
        "pack-linux 矩阵必须等于配置版本中有 standalone 运行时资产的子集"
    )


def test_release_pack_commands_pass_py_version() -> None:
    """每格的 build/package 显式传 --py-version，避免回落到配置的多版本布局."""
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    for job, target in (("pack-windows", "windows"), ("pack-linux", "linux")):
        block = _job_block(text, job)
        for subcommand in ("b", "p"):
            match = re.search(rf"fspack {subcommand} \. --target {target}[^\n]*", block)
            assert match is not None, f"{job} 缺少 fspack {subcommand} 命令"
            assert "--py-version" in match.group(0), f"{job} 的 fspack {subcommand} 未显式传 --py-version"
