"""分层与依赖方向守卫（见 docs/adr/0001-layering.md）.

用 AST 扫描 ``src/fspack`` 全部模块的 import（含函数体内的延迟导入），断言
M4 定下的依赖方向没有被新代码改回去：

* ``config`` 不得依赖 ``analyzer``（原先为了复用 AST 导入收集而反向依赖，
  M4 抽出 ``fspack.astutil`` 后由双方共同向下依赖）；
* ``analyzer`` / ``slim`` 不得依赖 ``packaging``（底层引擎不依赖打包编排层）；
* ``packaging`` 只有编排/展示入口可以引用 ``fspack.console`` / ``fspack.progress``，
  纯逻辑模块（解包、解析、运行时下载等）引用它们会连锁把 rich 拖进 import 链，
  直接抬高 --help/启动耗时；
* M4 拆出的叶子模块 ``metrics`` / ``astutil`` / ``archive`` 只依赖标准库与
  ``fspack.exceptions``，否则"叶子"名不副实。

新增违规时这里先失败，而不是等到 import 链膨胀、启动耗时回退后才发现。
"""

from __future__ import annotations

import ast
from pathlib import Path

import fspack

PACKAGE_ROOT = Path(fspack.__file__).parent
# 模板工程是被打包的示例载荷，不参与 fspack 自身的 import 图。
_SKIP_DIRS = ("assets",)

DISPLAY_MODULES = ("fspack.console", "fspack.progress")
# 允许引用 console/progress 的 packaging 模块：全部是"阶段编排入口"或
# "给终端用户打印结果"的展示入口，它们本来就在 CLI 调用栈的顶层。
ALLOWED_DISPLAY_IMPORTERS = frozenset(
    {
        "packaging/installer/base.py",
        "packaging/installer/facade.py",
        "packaging/installer/linux.py",
        "packaging/installer/macos.py",
        "packaging/installer/nsis.py",
        "packaging/installer/request.py",
        "packaging/installer/sevenzip.py",
        "packaging/installer/zip.py",
        "packaging/loader/compile.py",
        "packaging/manifest.py",
        "packaging/net.py",
        # 编译/前端驱动：需要把 nuitka/gcc、npm/vite 的原始输出按行转发到 DEBUG，
        # 属于「阶段执行 + 展示」入口，不是纯逻辑模块。
        "packaging/nuitka/progress.py",
        "packaging/pipeline/frontend_stage.py",
        "packaging/pipeline/context.py",
        "packaging/pipeline/dist_helpers.py",
        "packaging/pipeline/executor.py",
        "packaging/pipeline/plan_printer.py",
        "packaging/profile.py",
        "packaging/profile_log.py",
        "packaging/size_report.py",
        "packaging/wheels/process.py",
    }
)
LEAF_MODULES = ("metrics.py", "astutil.py", "archive.py", "jsoncache.py")


def _iter_sources() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_ROOT.rglob("*.py")
        if not any(part in _SKIP_DIRS for part in path.relative_to(PACKAGE_ROOT).parts)
    )


def _rel(path: Path) -> str:
    return path.relative_to(PACKAGE_ROOT).as_posix()


def _iter_imports(path: Path) -> set[str]:
    """返回模块引用的全部绝对点分模块名（含 ``from X import a`` 的 ``X.a``）."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # 相对导入（如 packaging/dep_analyzer/__init__.py 的 ``from .common``）
                # 解析成绝对名，避免漏检。
                package = _rel(path).split("/")[:-1]
                base = ["fspack", *package[: len(package) - node.level + 1]]
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            modules.add(module)
            modules.update(f"{module}.{alias.name}" for alias in node.names if module)
    return modules


def _importers(prefix: str) -> dict[str, set[str]]:
    """返回 ``{相对路径: 命中的 import 名}``，只保留引用了 ``prefix`` 的模块."""
    found: dict[str, set[str]] = {}
    for path in _iter_sources():
        hits = {name for name in _iter_imports(path) if name == prefix or name.startswith(f"{prefix}.")}
        if hits:
            found[_rel(path)] = hits
    return found


def _assert_no_dependency(source_pkg: str, target_pkg: str) -> None:
    offender = _importers(target_pkg)
    bad = {path: hits for path, hits in offender.items() if path.startswith(f"{source_pkg}/")}
    assert not bad, f"{source_pkg} 不得依赖 {target_pkg}（M4 分层，见 docs/adr/0001-layering.md）：{bad}"


def test_scanner_sees_nested_and_lazy_imports() -> None:
    """扫描器自检：函数体内的延迟导入、facade re-export 都要能识别."""
    imports = _iter_imports(PACKAGE_ROOT / "packaging" / "pipeline" / "executor.py")
    assert "fspack.progress" in imports  # 函数体内延迟导入
    assert "fspack.packaging.profile_log" in imports


def test_config_does_not_depend_on_analyzer() -> None:
    _assert_no_dependency("config", "fspack.analyzer")


def test_analyzer_does_not_depend_on_packaging() -> None:
    _assert_no_dependency("analyzer", "fspack.packaging")


def test_slim_does_not_depend_on_packaging() -> None:
    _assert_no_dependency("slim", "fspack.packaging")


def test_packaging_display_imports_stay_in_entry_points() -> None:
    """packaging 内引用 console/progress 的模块必须登记在案，且每个都是入口."""
    actual: set[str] = set()
    for path in _iter_sources():
        rel = _rel(path)
        if not rel.startswith("packaging/"):
            continue
        if any(name == mod or name.startswith(f"{mod}.") for name in _iter_imports(path) for mod in DISPLAY_MODULES):
            actual.add(rel)
    assert actual <= ALLOWED_DISPLAY_IMPORTERS, (
        "packaging 纯逻辑模块引入了 console/progress，会把 rich 拖进 import 链；"
        f"要么改走参数注入，要么把文件补进 allowlist 并说明它是编排/展示入口：{sorted(actual - ALLOWED_DISPLAY_IMPORTERS)}"
    )
    # 白名单里的文件若已不再引用，说明规则过期；及时清掉避免"僵尸豁免"。
    assert actual >= ALLOWED_DISPLAY_IMPORTERS, (
        f"白名单已过期（这些文件不再引用 console/progress）：{sorted(ALLOWED_DISPLAY_IMPORTERS - actual)}"
    )


def test_leaf_modules_only_depend_on_stdlib_and_exceptions() -> None:
    for name in LEAF_MODULES:
        path = PACKAGE_ROOT / name
        internal = {mod for mod in _iter_imports(path) if mod == "fspack" or mod.startswith("fspack.")}
        assert all(mod == "fspack.exceptions" or mod.startswith("fspack.exceptions.") for mod in internal), (
            f"{name} 应是叶子模块，只允许依赖 fspack.exceptions，实际：{sorted(internal)}"
        )
