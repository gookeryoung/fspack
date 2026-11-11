"""真实构建/运行的 e2e（slow + e2e，主门禁排除）.

覆盖只在单测 mock 层验证、真实路径可能出问题的环节：

- 真实链路 native：``init_project`` → 真实构建（镜像下载运行时）→ 运行产物
- 多入口 + 包级入口：``[project.scripts]`` 两个入口（含 ``pkg/sub/__init__.py``）
  真实构建并逐个运行（P0「包级入口」端到端回归）
- 并行下载并集：4 个纯 Python 依赖 + 冷 wheel 缓存，uv 并行路径下所有依赖
  必须都进入 site-packages 且产物可 import（P0「并行只装 1 个」回归）
- 平台 marker 过滤：Linux 上 ``--target windows``，win-only 依赖在、linux-only
  与 python_version 不满足的依赖不在（P0「marker 剥离」端到端回归）
- ``--lock``/``--frozen``：真实构建写锁 → 篡改 hash 后 frozen 构建必须失败

运行方式（联网 + 需要 gcc/mingw，单次数十秒）::

    uv run pytest -m e2e

CI 中由独立 job 执行，避免拖慢 ``-m "not slow"`` 主门禁。

已知限制（无法在当前矩阵验证，见 docs 或提交说明）：
- Linux 上产出的 Windows exe 需要 wine 才能执行，本文件只做 PE/entry 结构断言
- macOS 目标与 pkg/dmg 安装包只能在 macOS runner 验证，未纳入本文件
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fspack.cli_init import init_project
from fspack.config import BuildOptions, get_mirror
from fspack.exceptions import DependencyError
from fspack.packaging.pipeline import build
from fspack.platform import Platform

pytestmark = [pytest.mark.slow, pytest.mark.e2e]

_MIRROR = get_mirror("aliyun")


def _require_native_toolchain() -> None:
    """跳过缺少原生 C 编译器的环境（构建 loader 必需）."""
    if sys.platform == "win32":
        from fspack.packaging.loader.toolchain import _find_mingw_gcc

        if _find_mingw_gcc() is None:
            pytest.skip("Windows 原生构建需要 mingw gcc")
    elif sys.platform.startswith("linux"):
        if shutil.which("gcc") is None:
            pytest.skip("Linux 原生构建需要 gcc")
    elif sys.platform == "darwin":
        if shutil.which("clang") is None:
            pytest.skip("macOS 原生构建需要 clang")
    else:  # pragma: no cover - 未知平台
        pytest.skip(f"不支持的原生构建平台: {sys.platform}")


def _build(
    project_dir: Path,
    *,
    target: Platform | None = None,
    options: BuildOptions | None = None,
    embed_cache: Path | None = None,
) -> None:
    """执行真实构建（联网下载运行时/依赖），失败直接抛错."""
    build(project_dir, _MIRROR, dist_dir=project_dir / "dist", target=target, options=options, embed_cache=embed_cache)


def _exe_path(project_dir: Path, name: str, *, windows: bool = sys.platform == "win32") -> Path:
    """产物可执行文件路径（后缀由**目标**平台决定，交叉构建同样生效）."""
    return project_dir / "dist" / f"{name}{'.exe' if windows else ''}"


def _run(exe: Path, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """运行产物并捕获输出（UTF-8，超时 120s 防御挂起）."""
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return subprocess.run(
        [str(exe)],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
        check=False,
    )


def _write_project(root: Path, name: str, scripts: dict[str, str], files: dict[str, str], deps: list[str]) -> Path:
    """在 ``root`` 写一个最小可构建项目并返回项目目录."""
    project = root / name
    deps_block = "dependencies = [\n" + "".join(f'    "{d}",\n' for d in deps) + "]\n"
    scripts_block = "[project.scripts]\n" + "".join(f'{k} = "{v}"\n' for k, v in scripts.items())
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.8,<3.12"\n{deps_block}\n{scripts_block}',
        encoding="utf-8",
    )
    for rel, text in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


def test_e2e_native_build_and_run_smoke(tmp_path: Path) -> None:
    """真实链路：初始化工程 → 构建 → 运行产物断言输出."""
    _require_native_toolchain()
    project = init_project("e2e-smoke", template_id="helloworld", directory=tmp_path)
    _build(project)
    exe = _exe_path(project, "e2e-smoke")
    assert exe.is_file(), f"产物缺失: {exe}"
    result = _run(exe, cwd=project)
    assert result.returncode == 0, result.stderr
    assert "hello, world" in result.stdout


def test_e2e_multi_entry_and_package_entry_build_run(tmp_path: Path) -> None:
    """多入口 + 包级入口：两个 exe 都能跑，包级入口走 ``app.sub.__init__``."""
    _require_native_toolchain()
    project = _write_project(
        tmp_path,
        "e2e-multi",
        {"hello": "app.main:main", "pkghello": "app.sub:main"},
        {
            "app/__init__.py": "",
            "app/main.py": (
                'def main() -> None:\n    print("hello from app.main")\n\n\nif __name__ == "__main__":\n    main()\n'
            ),
            "app/sub/__init__.py": (
                "from .helper import MSG\n\n\n"
                "def main() -> None:\n"
                "    print(MSG)\n\n\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            ),
            "app/sub/helper.py": 'MSG = "hello from app.sub package entry"\n',
        },
        [],
    )
    _build(project)
    dist = project / "dist"
    for entry_name in ("hello", "pkghello"):
        assert (dist / f"{entry_name}.entry").is_file()
        exe = _exe_path(project, entry_name)
        assert exe.is_file(), f"入口 {entry_name} 产物缺失"
        result = _run(exe, cwd=project)
        assert result.returncode == 0, result.stderr
    assert "hello from app.main" in _run(_exe_path(project, "hello"), cwd=project).stdout
    assert "hello from app.sub package entry" in _run(_exe_path(project, "pkghello"), cwd=project).stdout
    # 包级入口源码按原路径进入 dist，供 runpy 以包模块方式执行
    assert (dist / "src" / "app" / "sub" / "__init__.py").is_file()


def test_e2e_parallel_download_installs_all_deps(tmp_path: Path) -> None:
    """并行下载并集：冷缓存下 4 个依赖全部装进 dist 且产物可 import（P0 回归）."""
    _require_native_toolchain()
    if shutil.which("uv") is None:
        pytest.skip("uv 不可用，无法覆盖并行下载路径")
    deps = ["six", "packaging", "idna", "charset-normalizer"]
    project = _write_project(
        tmp_path,
        "e2e-parallel",
        {"deps": "app.main:main"},
        {
            "app/__init__.py": "",
            "app/main.py": (
                "import charset_normalizer  # noqa: F401\n"
                "import idna  # noqa: F401\n"
                "import packaging  # noqa: F401\n"
                "import six\n\n\n"
                "def main() -> None:\n"
                '    print("deps-ok:" + six.__version__)\n\n\n'
                'if __name__ == "__main__":\n'
                "    main()\n"
            ),
        },
        deps,
    )
    # 独立 embed_cache：保证 wheel 缓存与依赖解析缓存均为冷启动，触发并行下载路径
    _build(project, embed_cache=tmp_path / "cache")
    result = _run(_exe_path(project, "deps"), cwd=project)
    assert result.returncode == 0, result.stderr
    assert "deps-ok:" in result.stdout
    site = project / "dist" / "site-packages"
    installed = {p.name.split("-")[0].lower().replace("_", "-") for p in site.iterdir()}
    for dep in deps:
        assert dep.replace("_", "-") in installed, f"{dep} 未进入 site-packages: {sorted(installed)}"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="仅 Linux + mingw 可交叉构建 Windows 目标")
def test_e2e_cross_target_windows_marker_filtering(tmp_path: Path) -> None:
    """Linux 上 ``--target windows``：平台 marker 正确取舍（P0 回归）.

    ``uvloop`` 在 win_amd64 无 wheel，若 marker 未按目标平台剔除，构建会直接
    失败；``colorama`` 仅 win32 生效必须保留；``tomli`` 在目标 3.11 应剔除。
    Windows 产物在 Linux 上无法执行（需 wine），只断言 PE 结构与 entry 文件。
    """
    from fspack.packaging.loader.toolchain import _find_mingw_gcc

    if _find_mingw_gcc() is None:
        pytest.skip("需要 x86_64-w64-mingw32-gcc（apt install mingw-w64）")
    project = _write_project(
        tmp_path,
        "e2e-cross",
        {"cross": "app.main:main"},
        {
            "app/__init__.py": "",
            "app/main.py": (
                "import colorama  # noqa: F401\n\n\n"
                "def main() -> None:\n"
                '    print("win-marker-ok")\n\n\n'
                'if __name__ == "__main__":\n'
                "    main()\n"
            ),
        },
        ["colorama; sys_platform == 'win32'", "uvloop; sys_platform == 'linux'", "tomli; python_version < '3.11'"],
    )
    _build(project, target=Platform.WINDOWS)
    exe = _exe_path(project, "cross", windows=True)
    assert exe.read_bytes()[:2] == b"MZ", "未产出 Windows PE 可执行文件"
    assert (exe.parent / "cross.entry").is_file()
    site = project / "dist" / "site-packages"
    names = {p.name.lower() for p in site.iterdir()}
    assert any(name.startswith("colorama") for name in names)
    assert not any(name.startswith("uvloop") for name in names), "linux-only 依赖未按目标平台剔除"
    assert not any(name.startswith("tomli") for name in names), "python_version 不满足的依赖未剔除"


def test_e2e_lock_then_frozen_rejects_tampered_hash(tmp_path: Path) -> None:
    """``--lock`` 写锁后篡改 hash，``--frozen`` 构建必须失败（P1 回归）."""
    _require_native_toolchain()
    project = _write_project(
        tmp_path,
        "e2e-lock",
        {"locked": "app.main:main"},
        {
            "app/__init__.py": "",
            "app/main.py": (
                "import six\n\n\n"
                "def main() -> None:\n"
                "    print(six.__version__)\n\n\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            ),
        },
        ["six"],
    )
    _build(project, options=BuildOptions(lock=True))
    lock_file = project / "requirements.lock"
    assert lock_file.is_file(), "--lock 未生成 requirements.lock"
    content = lock_file.read_text(encoding="utf-8")
    assert "six==" in content
    assert "--hash=sha256:" in content

    tampered = re.sub(
        r"--hash=sha256:([0-9a-f])", lambda m: f"--hash=sha256:{'0' if m.group(1) != '0' else '1'}", content, count=1
    )
    lock_file.write_text(tampered, encoding="utf-8")
    with pytest.raises(DependencyError):
        _build(project, options=BuildOptions(frozen=True))
