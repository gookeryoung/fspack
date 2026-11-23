"""fspack 自打包精简配置护栏：排除 rich 的可选能力依赖.

fspack 在 ``[tool.fspack].slim-exclude`` 中剥离 ``pygments``/``markdown_it``/
``mdurl``（约 4.5MB，占自身产物 site-packages 六成以上），前提是 fspack 运行期
**从不**导入这些包及其对应的 ``rich`` 子模块。本文件用两道护栏保证该前提：

1. 静态扫描 ``src/fspack`` 的 import，确认没有引入这些模块（含
   ``rich.syntax``/``rich.traceback``/``rich.markdown``）；
2. 用仓库真实的 ``slim-exclude`` 配置解压一个假 wheel，确认整包（含 dist-info）
   被剥离。

若第 1 项失败，说明新增代码用到了被排除的依赖，需要同时调整
``pyproject.toml`` 的 ``slim-exclude``，否则产物会在运行时 ImportError。
"""

from __future__ import annotations

import ast
import zipfile
from collections.abc import Iterator
from pathlib import Path

from fspack._compat import tomllib
from fspack.config import SlimRules
from fspack.slim import slim_unpack

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "fspack"

# 被自打包配置整包剥离的顶层模块
_EXCLUDED_TOP_MODULES = ("pygments", "markdown_it", "mdurl")

# 依赖上述包的 rich 子模块：导入即需要对应依赖
_EXCLUDED_RICH_SUBMODULES = frozenset({"rich.syntax", "rich.traceback", "rich.markdown"})


def _iter_imported_modules(path: Path) -> Iterator[str]:
    """产出源码中所有 import 的完整模块名（``from a import b`` → ``a.b``）."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                yield f"{node.module}.{alias.name}"


def _iter_fspack_sources() -> Iterator[Path]:
    """遍历 fspack 运行期源码（排除 resources 下的用户项目模板）."""
    for path in _SRC.rglob("*.py"):
        if "resources" in path.relative_to(_SRC).parts:
            continue
        yield path


def _is_excluded(module: str) -> bool:
    """模块（或其本身）是否属于被 self-build slim-exclude 剥离的范围."""
    top = module.split(".", 1)[0]
    if top in _EXCLUDED_TOP_MODULES:
        return True
    return any(module == sub or module.startswith(sub + ".") for sub in _EXCLUDED_RICH_SUBMODULES)


def test_fspack_sources_do_not_import_excluded_rich_optional_deps() -> None:
    """fspack 运行期源码不得导入被自打包剥离的依赖（否则产物会 ImportError）."""
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}: {module}"
        for path in _iter_fspack_sources()
        for module in _iter_imported_modules(path)
        if _is_excluded(module)
    ]
    assert not offenders, "新增了对已剥离依赖的导入，请同步调整 pyproject.toml 的 slim-exclude:\n" + "\n".join(
        offenders
    )


def test_self_build_slim_exclude_strips_whole_wheel(tmp_path: Path) -> None:
    """仓库真实 slim-exclude 配置会整包剥离 pygments（含 dist-info）."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = tuple(data["tool"]["fspack"]["slim-exclude"])
    rules = SlimRules(exclude=patterns)

    whl = tmp_path / "pygments-2.21.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        for name in (
            "pygments/__init__.py",
            "pygments/lexers/__init__.py",
            "pygments/lexers/python.py",
            "pygments-2.21.0.dist-info/METADATA",
            "pygments-2.21.0.dist-info/RECORD",
        ):
            zf.writestr(name, b"x")

    dest = tmp_path / "site-packages"
    slim_unpack([whl], dest, slim_rules=rules)

    assert not (dest / "pygments").exists()
    assert not (dest / "pygments-2.21.0.dist-info").exists()


def test_self_build_slim_exclude_covers_all_optional_dep_patterns() -> None:
    """自打包配置覆盖 pygments / markdown-it-py / mdurl 的包目录与 dist-info."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = set(data["tool"]["fspack"]["slim-exclude"])
    for pkg, dist_info in (
        ("pygments", "pygments-*.dist-info"),
        ("markdown_it", "markdown_it_py-*.dist-info"),
        ("mdurl", "mdurl-*.dist-info"),
    ):
        assert f"{pkg}/**" in patterns
        assert f"{dist_info}/**" in patterns
