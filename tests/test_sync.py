"""``fspack.packaging.sync`` 源码同步测试：copy_source/_sync_tree、排除规则、前端裁剪与指纹."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fspack.builder import (
    _dir_size,
    _site_packages_fingerprint,
    _sync_tree,
    copy_source,
)
from fspack.packaging.pipeline.frontend_stage import (
    _detect_frontends,
    _frontend_prune_map,
)
from tests._stubs import write_frontend_pkg


def test_copy_source_excludes_dist(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    pass\n")
    (src / "dist").mkdir()
    (src / "dist" / "junk.txt").write_text("x")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "c.pyc").write_text("x")
    dst = tmp_path / "out" / "src"
    copy_source(src, dst)
    assert (dst / "app.py").is_file()
    assert not (dst / "dist").exists()
    assert not (dst / "__pycache__").exists()


def test_copy_source_overwrites_existing(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("v2")
    dst = tmp_path / "out" / "src"
    dst.mkdir(parents=True)
    (dst / "old.py").write_text("old")
    copy_source(src, dst)
    assert (dst / "app.py").read_text() == "v2"
    assert not (dst / "old.py").exists()


def test_copy_source_strips_dev_artifacts(tmp_path: Path) -> None:
    """剥离开发期元数据/工具配置/凭证/文档/测试目录."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')\n")
    # Python 项目元数据
    (src / ".python-version").write_text("3.11\n")
    (src / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (src / "uv.lock").write_text("version = 1\n")
    (src / "uv.toml").write_text("preview = true\n")
    (src / "setup.py").write_text("from setuptools import setup\n")
    (src / "setup.cfg").write_text("[metadata]\n")
    (src / "MANIFEST.in").write_text("include LICENSE\n")
    (src / "requirements.txt").write_text("rich\n")
    (src / "requirements-dev.txt").write_text("pytest\n")
    # 工具链配置
    for cfg in ("ruff.toml", "pyrefly.toml", "pytest.ini", "tox.ini", "uv.toml"):
        if not (src / cfg).exists():
            (src / cfg).write_text("# cfg\n")
    (src / ".ruff.toml").write_text("# ruff\n")
    (src / ".bumpversion.toml").write_text("[bumpversion]\n")
    (src / ".pre-commit-config.yaml").write_text("repos: []\n")
    (src / ".coveragerc").write_text("[run]\n")
    (src / ".readthedocs.yaml").write_text("version: 2\n")
    (src / "Makefile").write_text("all:\n\techo hi\n")
    (src / ".copier-answers.yml").write_text("_commit: x\n")
    # 凭证
    (src / ".env").write_text("SECRET=x\n")
    (src / ".env.local").write_text("SECRET=y\n")
    # 版本控制与 IDE
    (src / ".gitignore").write_text("dist/\n")
    (src / ".gitattributes").write_text("* text=auto\n")
    (src / ".vscode").mkdir()
    (src / ".vscode" / "settings.json").write_text("{}")
    (src / ".idea").mkdir()
    (src / ".github").mkdir()
    (src / ".github" / "ci.yml").write_text("on: push\n")
    # 文档
    (src / "README.md").write_text("# app\n")
    (src / "CHANGELOG.rst").write_text("v0.1\n")
    (src / "docs").mkdir()
    (src / "docs" / "index.md").write_text("# docs\n")
    # 测试目录
    (src / "tests").mkdir()
    (src / "tests" / "test_app.py").write_text("def test(): pass\n")
    # 覆盖率与缓存
    (src / ".coverage").write_text("x")
    (src / "htmlcov").mkdir()
    (src / "htmlcov" / "index.html").write_text("<html/>")
    (src / ".ruff_cache").mkdir()
    (src / ".pyrefly_cache").mkdir()

    dst = tmp_path / "out" / "src"
    copy_source(src, dst)

    # 应用源码保留
    assert (dst / "app.py").is_file()
    # 元数据与配置全部剥离
    for name in (
        ".python-version",
        "pyproject.toml",
        "uv.lock",
        "uv.toml",
        "setup.py",
        "setup.cfg",
        "MANIFEST.in",
        "requirements.txt",
        "requirements-dev.txt",
        "ruff.toml",
        ".ruff.toml",
        "pyrefly.toml",
        "pytest.ini",
        "tox.ini",
        ".bumpversion.toml",
        ".pre-commit-config.yaml",
        ".coveragerc",
        ".readthedocs.yaml",
        "Makefile",
        ".copier-answers.yml",
        ".env",
        ".env.local",
        ".gitignore",
        ".gitattributes",
        "README.md",
        "CHANGELOG.rst",
        ".coverage",
    ):
        assert not (dst / name).exists(), f"应被剥离: {name}"
    # 目录全部剥离
    for d in (".vscode", ".idea", ".github", "docs", "tests", "htmlcov", ".ruff_cache", ".pyrefly_cache"):
        assert not (dst / d).exists(), f"应被剥离目录: {d}"


def test_copy_source_keeps_runtime_resources(tmp_path: Path) -> None:
    """保留运行时所需资源：源码、数据文件、LICENSE、子包."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')\n")
    (src / "LICENSE").write_text("MIT License\n")
    (src / "data.json").write_text("{}\n")
    (src / "assets").mkdir()
    (src / "assets" / "logo.png").write_bytes(b"\x89PNG")
    (src / "pkg").mkdir()
    (src / "pkg" / "__init__.py").write_text("")
    (src / "pkg" / "mod.py").write_text("x = 1\n")
    # 子包内的开发文件也应剥离
    (src / "pkg" / "README.md").write_text("# pkg\n")
    (src / "pkg" / "tests").mkdir()
    (src / "pkg" / "tests" / "test_mod.py").write_text("pass\n")

    dst = tmp_path / "out" / "src"
    copy_source(src, dst)

    assert (dst / "app.py").is_file()
    assert (dst / "LICENSE").is_file(), "LICENSE 应保留以符合开源协议分发要求"
    assert (dst / "data.json").is_file()
    assert (dst / "assets" / "logo.png").is_file()
    assert (dst / "pkg" / "__init__.py").is_file()
    assert (dst / "pkg" / "mod.py").is_file()
    # 子包内的开发文件同样剥离
    assert not (dst / "pkg" / "README.md").exists()
    assert not (dst / "pkg" / "tests").exists()


def test_dir_size_empty_dir(tmp_path: Path) -> None:
    """_dir_size 对空目录返回 0."""
    d = tmp_path / "empty"
    d.mkdir()
    assert _dir_size(d) == 0


def test_dir_size_nested_files(tmp_path: Path) -> None:
    """_dir_size 递归累加所有文件大小."""
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "a.bin").write_bytes(b"x" * 100)
    (d / "sub" / "b.bin").write_bytes(b"y" * 200)
    (d / "sub" / "c.bin").write_bytes(b"z" * 300)
    assert _dir_size(d) == 600


def test_dir_size_handles_concurrent_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_dir_size 遇到 OSError（stat 失败）时跳过，不阻断计算.

    模拟 scandir_tree 返回的条目中，stat(follow_symlinks=False) 抛 OSError
    （并发删除/权限问题）。_dir_size 委托 fspack.fsutil.scandir_dir_size，后者用
    scandir_tree 枚举，DirEntry.stat 复用枚举缓存但仍可能因文件被并发删除抛 OSError。
    """

    class _StatResult:
        def __init__(self, size: int) -> None:
            self.st_size = size

    class _GoodEntry:
        def __init__(self, size: int) -> None:
            self._size = size

        def stat(self, *, follow_symlinks: bool = True) -> _StatResult:
            return _StatResult(self._size)

    class _BrokenEntry:
        def stat(self, *, follow_symlinks: bool = True) -> _StatResult:
            raise OSError("file removed by another process")

    d = tmp_path / "tree"
    d.mkdir()
    # _dir_size 委托 fspack.fsutil.scandir_dir_size，后者用 scandir_tree 枚举。
    # 按"patch 定义所在底层模块"约定 patch fspack.fsutil.scandir_tree，验证
    # stat 抛 OSError 的条目被跳过。
    monkeypatch.setattr(
        "fspack.fsutil.scandir_tree",
        lambda root: [_GoodEntry(100), _BrokenEntry(), _GoodEntry(200)] if root == d else [],
    )
    # BrokenEntry 的 OSError 被跳过，仅累加两个 GoodEntry 的 100 + 200 = 300
    assert _dir_size(d) == 300


# ---- 增量同步（copy_source 保留 __pycache__）----


def test_copy_source_preserves_pycache(tmp_path: Path) -> None:
    """copy_source 增量同步时保留 dst 的 __pycache__ 目录以复用 .pyc 缓存."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('v1')\n")
    dst = tmp_path / "out" / "src"
    dst.mkdir(parents=True)
    (dst / "old.py").write_text("old")
    pycache = dst / "__pycache__"
    pycache.mkdir()
    (pycache / "app.cpython-311.pyc").write_bytes(b"\x00\x00")

    copy_source(src, dst)

    # __pycache__ 保留
    assert pycache.is_dir()
    assert (pycache / "app.cpython-311.pyc").is_file()
    # old.py（src 中不存在）被删除
    assert not (dst / "old.py").exists()
    # app.py 覆盖复制
    assert (dst / "app.py").read_text() == "print('v1')\n"


def test_sync_tree_recursive_preserves_nested_pycache(tmp_path: Path) -> None:
    """_sync_tree 递归保留子目录中的 __pycache__."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "pkg").mkdir()
    (src / "pkg" / "mod.py").write_text("x=1\n")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "pkg").mkdir()
    (dst / "pkg" / "__pycache__").mkdir()
    (dst / "pkg" / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"\x00")
    (dst / "pkg" / "stale.py").write_text("stale")

    import shutil

    _sync_tree(src, dst, shutil.ignore_patterns())

    assert (dst / "pkg" / "__pycache__" / "mod.cpython-311.pyc").is_file()
    assert not (dst / "pkg" / "stale.py").exists()
    assert (dst / "pkg" / "mod.py").read_text() == "x=1\n"


def test_copy_source_syncs_deleted_files(tmp_path: Path) -> None:
    """src 删除文件后 copy_source 同步删除 dst 中对应文件（保留 __pycache__）."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("v1")
    dst = tmp_path / "out" / "src"

    # 第一次复制
    copy_source(src, dst)
    assert (dst / "app.py").is_file()

    # src 删除 app.py，添加 main.py
    (src / "app.py").unlink()
    (src / "main.py").write_text("v2")

    # 第二次同步
    copy_source(src, dst)
    assert not (dst / "app.py").exists(), "src 已删除的文件应从 dst 移除"
    assert (dst / "main.py").is_file()


def test_sync_tree_file_to_dir_type_swap(tmp_path: Path) -> None:
    """src 同名条目由文件改为目录时，先删 dst 残留文件再建目录，不抛 FileExistsError."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo").write_text("v1")
    dst = tmp_path / "dst"
    dst.mkdir()
    _sync_tree(src, dst, shutil.ignore_patterns())
    assert (dst / "foo").is_file()

    # src 侧 foo 由文件改为目录
    (src / "foo").unlink()
    (src / "foo").mkdir()
    (src / "foo" / "bar.py").write_text("in dir")

    _sync_tree(src, dst, shutil.ignore_patterns())

    assert (dst / "foo").is_dir(), "dst/foo 应被替换为目录"
    assert (dst / "foo" / "bar.py").read_text() == "in dir"


def test_sync_tree_dir_to_file_type_swap(tmp_path: Path) -> None:
    """src 同名条目由目录改为文件时，先删 dst 残留目录再复制，不抛 IsADirectoryError."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo").mkdir()
    (src / "foo" / "bar.py").write_text("in dir")
    dst = tmp_path / "dst"
    dst.mkdir()
    _sync_tree(src, dst, shutil.ignore_patterns())
    assert (dst / "foo").is_dir()

    # src 侧 foo 由目录改为文件
    shutil.rmtree(src / "foo")
    (src / "foo").write_text("now a file")

    _sync_tree(src, dst, shutil.ignore_patterns())

    assert (dst / "foo").is_file(), "dst/foo 应被替换为文件"
    assert (dst / "foo").read_text() == "now a file"


def test_sync_tree_deletes_stale_directory(tmp_path: Path) -> None:
    """_sync_tree 删除 dst 中 src 不存在的目录（rmtree 分支）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("v1")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "app.py").write_text("old")
    (dst / "stale_dir").mkdir()
    (dst / "stale_dir" / "file.txt").write_text("stale")

    _sync_tree(src, dst, shutil.ignore_patterns())

    assert not (dst / "stale_dir").exists(), "src 不存在的目录应被删除"
    assert (dst / "app.py").read_text() == "v1"


def test_sync_tree_overwrites_changed_file(tmp_path: Path) -> None:
    """_sync_tree 对 dst 已存在但内容变动的文件调 copy2 覆盖（mtime/size 不同分支）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("new content")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "app.py").write_text("old")

    _sync_tree(src, dst, shutil.ignore_patterns())

    assert (dst / "app.py").read_text() == "new content"


def test_sync_tree_skips_unchanged_file(tmp_path: Path) -> None:
    """_sync_tree 对 mtime+size 相同的文件跳过 copy2（避免不必要磁盘写）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("same")
    dst = tmp_path / "dst"
    dst.mkdir()
    # 先复制一次确保 mtime/size 一致
    shutil.copy2(src / "app.py", dst / "app.py")
    src_stat_before = (src / "app.py").stat()
    dst_stat_before = (dst / "app.py").stat()

    _sync_tree(src, dst, shutil.ignore_patterns())

    # dst 文件未被重写（mtime 不变）
    dst_stat_after = (dst / "app.py").stat()
    assert dst_stat_after.st_mtime_ns == dst_stat_before.st_mtime_ns
    assert src_stat_before.st_mtime_ns == dst_stat_after.st_mtime_ns


def test_site_packages_fingerprint_empty_when_no_dir(tmp_path: Path) -> None:
    """_site_packages_fingerprint 目录不存在时返回空串."""
    assert _site_packages_fingerprint(tmp_path / "nonexistent") == ""


def test_site_packages_fingerprint_empty_when_empty(tmp_path: Path) -> None:
    """_site_packages_fingerprint 目录存在但无 dist-info 时返回非空哈希（空输入）."""
    sp = tmp_path / "site-packages"
    sp.mkdir()
    fp = _site_packages_fingerprint(sp)
    assert isinstance(fp, str)
    assert len(fp) == 64  # sha256 hexdigest 长度


def test_site_packages_fingerprint_changes_with_dist_info(tmp_path: Path) -> None:
    """_site_packages_fingerprint 随 dist-info 目录名变化."""
    sp = tmp_path / "site-packages"
    sp.mkdir()
    fp_empty = _site_packages_fingerprint(sp)
    (sp / "rich-13.0.0.dist-info").mkdir()
    fp_rich = _site_packages_fingerprint(sp)
    (sp / "click-8.1.0.dist-info").mkdir()
    fp_both = _site_packages_fingerprint(sp)
    assert fp_empty != fp_rich
    assert fp_rich != fp_both
    assert len(fp_both) == 64


def test_site_packages_fingerprint_order_independent(tmp_path: Path) -> None:
    """_site_packages_fingerprint 对 dist-info 排序后哈希，顺序无关."""
    sp1 = tmp_path / "sp1"
    sp1.mkdir()
    (sp1 / "aaa-1.0.dist-info").mkdir()
    (sp1 / "zzz-1.0.dist-info").mkdir()
    sp2 = tmp_path / "sp2"
    sp2.mkdir()
    (sp2 / "zzz-1.0.dist-info").mkdir()
    (sp2 / "aaa-1.0.dist-info").mkdir()
    assert _site_packages_fingerprint(sp1) == _site_packages_fingerprint(sp2)


def test_copy_source_with_extra_excludes(tmp_path: Path) -> None:
    """extra_excludes 额外排除 [tool.fspack] exclude 配置的目录."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "examples").mkdir()
    (src / "examples" / "demo.py").write_text("print('demo')")
    (src / "mydata").mkdir()
    (src / "mydata" / "data.txt").write_text("data")
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, extra_excludes=("examples", "mydata"))
    assert (dst / "app.py").is_file()
    assert not (dst / "examples").exists()
    assert not (dst / "mydata").exists()


def test_copy_source_extra_excludes_merged_with_builtin(tmp_path: Path) -> None:
    """extra_excludes 与内置 _EXCLUDE 合并：内置排除仍生效 + 额外排除生效."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "dist").mkdir()  # 内置排除
    (src / "dist" / "junk.txt").write_text("x")
    (src / "custom_excl").mkdir()  # 额外排除
    (src / "custom_excl" / "file.py").write_text("x")
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, extra_excludes=("custom_excl",))
    assert (dst / "app.py").is_file()
    assert not (dst / "dist").exists()
    assert not (dst / "custom_excl").exists()


def test_copy_source_data_dirs_keeps_metadata_in_data_dirs(tmp_path: Path) -> None:
    """data_dirs 内的元数据/文档文件保留（pyproject.toml/README.md/uv.lock 等）.

    模拟 fspack 自身打包场景：``src/fspack/assets/templates/<each>/`` 是完整
    项目模板，其内的 ``pyproject.toml``/``README.md``/``uv.lock`` 是模板必需
    文件，必须原样保留供 ``fsp doctor --test`` 复制后构建。
    """
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    # 模拟 assets/templates/gui/tk_app/ 完整项目模板
    tpl = src / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app"
    tpl.mkdir(parents=True)
    (tpl / "pyproject.toml").write_text('[project]\nname = "tk_app"\n')
    (tpl / "tk_app.py").write_text("def main():\n    print('hi')\n")
    (tpl / "README.md").write_text("# tk_app\n")
    (tpl / "uv.lock").write_text("version = 1\n")
    (tpl / ".python-version").write_text("3.11\n")
    # 项目根目录的元数据文件仍应被剥离
    (src / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (src / "README.md").write_text("# app\n")
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, data_dirs=("src/fspack/assets/templates",))
    # data_dirs 内的元数据/文档文件保留
    assert (dst / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app" / "pyproject.toml").is_file()
    assert (dst / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app" / "README.md").is_file()
    assert (dst / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app" / "uv.lock").is_file()
    assert (dst / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app" / ".python-version").is_file()
    # 应用源码保留
    assert (dst / "app.py").is_file()
    assert (dst / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app" / "tk_app.py").is_file()
    # 项目根目录的元数据文件仍被剥离（data_dirs 只保护子树内的元数据）
    assert not (dst / "pyproject.toml").exists()
    assert not (dst / "README.md").exists()


def test_copy_source_data_dirs_still_excludes_build_artifacts(tmp_path: Path) -> None:
    """data_dirs 内仍排除构建产物/缓存/IDE 等（_EXCLUDE_ALWAYS 始终生效）."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    tpl = src / "assets" / "templates" / "demo"
    tpl.mkdir(parents=True)
    (tpl / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    (tpl / "main.py").write_text("print('demo')\n")
    # 构建产物与缓存：data_dirs 内仍应排除
    (tpl / "__pycache__").mkdir()
    (tpl / "__pycache__" / "main.cpython-311.pyc").write_text("x")
    (tpl / "dist").mkdir()
    (tpl / "dist" / "junk.txt").write_text("x")
    (tpl / ".venv").mkdir()
    (tpl / ".venv" / "pyvenv.cfg").write_text("x")
    (tpl / "node_modules").mkdir()
    (tpl / "node_modules" / ".pnpm").mkdir()
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, data_dirs=("assets/templates",))
    # 元数据保留（data_dirs 保护）
    assert (dst / "assets" / "templates" / "demo" / "pyproject.toml").is_file()
    # 构建产物/缓存排除（_EXCLUDE_ALWAYS 始终生效）
    assert not (dst / "assets" / "templates" / "demo" / "__pycache__").exists()
    assert not (dst / "assets" / "templates" / "demo" / "dist").exists()
    assert not (dst / "assets" / "templates" / "demo" / ".venv").exists()
    # 前端依赖缓存排除：pnpm install 可再生，.pnpm 超长路径会导致 fsp c 清理失败
    assert not (dst / "assets" / "templates" / "demo" / "node_modules").exists()


def test_copy_source_data_dirs_empty_keeps_default_behavior(tmp_path: Path) -> None:
    """data_dirs 为空时行为与不传一致：元数据/文档照常剥离（向后兼容）."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (src / "README.md").write_text("# app\n")
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, data_dirs=())
    assert (dst / "app.py").is_file()
    assert not (dst / "pyproject.toml").exists()
    assert not (dst / "README.md").exists()


# --- iter-148 前后端分离 Web 打包：web_static_dirs 保护 ---


def test_copy_source_web_static_dirs_keeps_metadata(tmp_path: Path) -> None:
    """web_static_dirs 内的元数据/文档文件保留（与 data_dirs 同等保护）."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    # 模拟前端构建产物目录 dist/
    dist_dir = src / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html></html>")
    (dist_dir / "pyproject.toml").write_text('[project]\nname = "frontend"\n')
    (dist_dir / "README.md").write_text("# frontend\n")
    # 项目根目录的元数据文件仍应被剥离
    (src / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (src / "README.md").write_text("# app\n")
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, web_static_dirs=("dist",))
    # web_static_dirs 内的元数据/文档文件保留
    assert (dst / "dist" / "index.html").is_file()
    assert (dst / "dist" / "pyproject.toml").is_file()
    assert (dst / "dist" / "README.md").is_file()
    # 应用源码保留
    assert (dst / "app.py").is_file()
    # 项目根目录的元数据文件仍被剥离
    assert not (dst / "pyproject.toml").exists()
    assert not (dst / "README.md").exists()


def test_copy_source_frontend_prune_keeps_only_output(tmp_path: Path) -> None:
    """前端根目录下只保留产物目录：src/public/package.json 等源码不进 dist."""
    src = tmp_path / "proj"
    fe = src / "src" / "webview_app" / "frontend"
    write_frontend_pkg(fe)
    (fe / "src").mkdir()
    (fe / "src" / "App.vue").write_text("<template/>", encoding="utf-8")
    (fe / "public").mkdir()
    (fe / "vite.config.ts").write_text("export default {}", encoding="utf-8")
    (fe / "index.html").write_text("<html/>", encoding="utf-8")
    deploy = fe / "deploy"
    deploy.mkdir()
    (deploy / "index.html").write_text("<html>built</html>", encoding="utf-8")
    assets = deploy / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")

    dst = tmp_path / "dist_src"
    copy_source(src, dst, frontend_prune=_frontend_prune_map(_detect_frontends(src, ())))

    fe_dst = dst / "src" / "webview_app" / "frontend"
    assert sorted(p.name for p in fe_dst.iterdir()) == ["deploy"]
    assert (fe_dst / "deploy" / "index.html").read_text(encoding="utf-8") == "<html>built</html>"
    assert (fe_dst / "deploy" / "assets" / "app.js").is_file()
    assert not (fe_dst / "package.json").exists()
    assert not (fe_dst / "src").exists()
    assert not (fe_dst / "vite.config.ts").exists()


def test_copy_source_frontend_prune_output_name_dist_restored(tmp_path: Path) -> None:
    """产物目录名为 dist 时命中 _EXCLUDE_ALWAYS 构建产物模式，仍被保护恢复."""
    src = tmp_path / "proj"
    fe = write_frontend_pkg(src / "frontend")
    out = fe / "dist"
    out.mkdir()
    (out / "index.html").write_text("<html/>", encoding="utf-8")

    dst = tmp_path / "dist_src"
    # 显式配置产物目录为 frontend/dist（配置路径识别的 output_dirs）
    prune = {fe.resolve(): (out.resolve(),)}
    copy_source(src, dst, frontend_prune=prune)

    assert sorted(p.name for p in (dst / "frontend").iterdir()) == ["dist"]
    assert (dst / "frontend" / "dist" / "index.html").is_file()


def test_copy_source_frontend_prune_nested_output(tmp_path: Path) -> None:
    """产物目录嵌套（build/www）：逐层裁剪，只保留通往产物的路径链."""
    src = tmp_path / "proj"
    fe = write_frontend_pkg(src / "frontend")
    www = fe / "build" / "www"
    www.mkdir(parents=True)
    (www / "index.html").write_text("<html/>", encoding="utf-8")
    (fe / "build" / "cache.txt").write_text("x", encoding="utf-8")

    dst = tmp_path / "dist_src"
    copy_source(src, dst, frontend_prune={fe.resolve(): (www.resolve(),)})

    fe_dst = dst / "frontend"
    assert sorted(p.name for p in fe_dst.iterdir()) == ["build"]
    assert sorted(p.name for p in (fe_dst / "build").iterdir()) == ["www"]
    assert (fe_dst / "build" / "www" / "index.html").is_file()


def test_copy_source_frontend_prune_output_is_root_no_prune(tmp_path: Path) -> None:
    """产物目录即前端根本身（配置指向前端根，如 flask 手写 html）：不裁剪."""
    src = tmp_path / "proj"
    fe = write_frontend_pkg(src / "frontend")
    (fe / "index.html").write_text("<html/>", encoding="utf-8")

    dst = tmp_path / "dist_src"
    copy_source(src, dst, frontend_prune={fe.resolve(): (fe.resolve(),)})

    # frontend 根即产物：原样复制（package.json 保留）
    assert (dst / "frontend" / "package.json").is_file()
    assert (dst / "frontend" / "index.html").is_file()


def test_copy_source_frontend_prune_incremental_sync(tmp_path: Path) -> None:
    """增量同步路径（dst 已存在）同样应用裁剪：dst 残留的前端源码被删除."""
    src = tmp_path / "proj"
    fe = write_frontend_pkg(src / "frontend")
    (fe / "src").mkdir()
    (fe / "src" / "App.vue").write_text("<template/>", encoding="utf-8")
    deploy = fe / "deploy"
    deploy.mkdir()
    (deploy / "index.html").write_text("<html/>", encoding="utf-8")

    dst = tmp_path / "dist_src"
    # 首次复制（未裁剪，模拟旧版 fspack 打出的 dist 残留前端源码）
    copy_source(src, dst)
    assert (dst / "frontend" / "package.json").is_file()

    # 二次构建（带裁剪）：增量同步删除 dst 中源码侧已排除的文件
    copy_source(src, dst, frontend_prune=_frontend_prune_map(_detect_frontends(src, ())))
    fe_dst = dst / "frontend"
    assert sorted(p.name for p in fe_dst.iterdir()) == ["deploy"]
    assert not (fe_dst / "package.json").exists()
    assert not (fe_dst / "src").exists()
