"""离线 e2e 回归：真实文件系统与真实 pip 输出，不联网、单测 < 0.5s.

覆盖 P0 两处缺陷的端到端位（不依赖 mock 的解析/装配层）：

- 并行下载 stdout 并集解析：fixture 由真实 pip 26 的输出原样捕获后拼接，
  模拟并行 worker stdout 直接串联的现实形态（tests/data/pip_download/）
- 包级入口 ``<pkg>/<mod>/__init__.py``：在真实项目树上走
  ``ProjectInfo.from_dir`` → ``EntryWrapper.dotted_module_name`` →
  ``generate_wrapper_source`` 全链路，断言以包模块（``app.sub.__init__``）
  运行而非顶层 ``run_path``

真实构建/运行链路的用例在 ``test_e2e_slow.py``（标记 slow + e2e）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.config import ProjectInfo
from fspack.packaging.entry import EntryWrapper
from fspack.packaging.wheels.output import _parse_pip_download_wheels

pytestmark = pytest.mark.e2e

_DATA = Path(__file__).parent / "data" / "pip_download"


def _read(name: str) -> str:
    """读取真实 pip 输出 fixture（UTF-8）.

    fixture 采集自 Windows，``Saved`` 行路径为反斜杠；``Path().name`` 仅在
    Windows 上把反斜杠当分隔符，Linux/macOS 会得到整串路径而解析不出包名。
    读取时统一转为正斜杠，保证断言跨平台一致（解析逻辑本身与分隔符无关）。
    """
    return (_DATA / name).read_text(encoding="utf-8").replace("\\", "/")


def test_pip_stdout_parallel_workers_union_from_real_capture() -> None:
    """并行 worker stdout 拼接后解析出全部包（回归：只取首个成功行会丢依赖）."""
    combined = _read("worker_six.txt") + _read("worker_idna.txt")
    wheels = _parse_pip_download_wheels(combined)
    assert wheels == ["six-1.17.0-py2.py3-none-any.whl", "idna-3.20-py3-none-any.whl"]


def test_pip_stdout_backtracking_keeps_only_final_resolution() -> None:
    """真实回溯输出（cp311/win_amd64）只保留最终解析集，且每包恰好一个版本."""
    wheels = _parse_pip_download_wheels(_read("backtrack_pydantic.txt"))
    assert wheels == [
        "pydantic-2.7.4-py3-none-any.whl",
        "pydantic_core-2.18.4-cp311-none-win_amd64.whl",
        "annotated_types-0.8.0-py3-none-any.whl",
        "typing_extensions-4.16.0-py3-none-any.whl",
    ]


def test_package_entry_init_py_resolves_to_package_module(tmp_path: Path) -> None:
    """``app.sub:main`` 解析到 ``app/sub/__init__.py``，wrapper 以包模块运行.

    回归 P0「包级入口」缺陷：候选缺少 ``<pkg>/<mod>/__init__.py`` 时
    ``ProjectInfo.from_dir`` 会直接报入口不存在；补候选后 wrapper 的
    module 名为 ``app.sub.__init__``（``runpy.run_module`` 可执行），
    而不是被误判为顶层脚本走 ``run_path``。
    """
    pkg = tmp_path / "app" / "sub"
    pkg.mkdir(parents=True)
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text(
        "from .helper import MSG\n\n\ndef main() -> None:\n    print(MSG)\n",
        encoding="utf-8",
    )
    (pkg / "helper.py").write_text('MSG = "pkg entry ok"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pkg-entry-e2e"\nversion = "0.1.0"\nrequires-python = ">=3.8"\n\n'
        '[project.scripts]\npkghello = "app.sub:main"\n',
        encoding="utf-8",
    )

    info = ProjectInfo.from_dir(tmp_path)
    entry = next(ep for ep in info.all_entries if ep.name == "pkghello")
    assert entry.file == pkg / "__init__.py"
    entry_rel = entry.entry_rel(info.src_dir)
    assert entry_rel == "app/sub/__init__.py"

    dotted = EntryWrapper.dotted_module_name(info.src_dir, entry.file)
    assert dotted is not None
    module_dotted, pkg_root_rel = dotted
    assert module_dotted == "app.sub.__init__"

    source = EntryWrapper.generate_wrapper_source(entry.name, module_dotted, entry_rel, pkg_root_rel)
    assert "_ENTRY_MODULE = 'app.sub.__init__'" in source
    assert "_PKG_ROOT_REL = 'src'" in source
    # 模板里两分支都在，运行时按 _ENTRY_MODULE 是否为空选择；非空即走 run_module，
    # 空串才会退化为顶层 run_path。
    assert "_ENTRY_MODULE = ''" not in source
