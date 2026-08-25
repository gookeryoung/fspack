"""cli 子命令分发测试."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fspack import __version__, cli
from fspack.config import BuildOptions, get_mirror
from fspack.packaging.profile_log import ProfileOptions
from fspack.platform import Platform
from fspack.runner import RunOptions


def test_import_does_not_pull_logging() -> None:
    """导入 fspack.cli 不拉 logging（启动性能契约）.

    logging 导入链约 5-6ms，仅异常分支与 package/init 的 info 日志使用，
    console 顶层自会导入。若有人把 import logging 加回模块顶层，
    ``fsp --help`` 类轻路径将白白多付这段导入耗时（启动剖析实测）。
    用干净子进程验证（pytest 进程自身已加载 logging，进程内断言无效）。
    """
    src_root = str(Path(cli.__file__).resolve().parent.parent)
    code = f"import sys; sys.path.insert(0, r'{src_root}'); import fspack.cli; print('logging' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_build_parser_prog() -> None:
    assert cli.build_parser().prog == "fspack"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["-V"])
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main([])
    assert "fspack" in capsys.readouterr().out


def test_build_bad_pyproject_exits_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """fsp b 遇到语法错误的 pyproject.toml 时打印中文错误并以退出码 2 退出（不抛原始 traceback）.

    回归：single-project 路径（非递归）原先无 FspackError 兜底，ProjectError 会
    以原始 Python traceback 崩溃，用户只能在栈底看到淹没的中文消息。现在 main
    统一捕获 FspackError 输出简洁错误。
    """
    (tmp_path / "pyproject.toml").write_text("this is = = not valid {{{", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["b", str(tmp_path)])
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "语法错误" in out


def test_build_non_utf8_pyproject_exits_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """fsp b 遇到非 UTF-8 编码的 pyproject.toml 时打印中文错误并以退出码 2 退出.

    回归：用户生产环境曾遇到含非法起始字节文件导致命令以原始 UnicodeDecodeError
    traceback 崩溃。现在解析层包装为 ProjectError，main 层统一捕获输出简洁错误。
    """
    # 0xa7 为非法 UTF-8 起始字节，read_text(encoding="utf-8") 必抛 UnicodeDecodeError
    (tmp_path / "pyproject.toml").write_bytes(b"\xa7[project]\nname = 'x'\n")
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["b", str(tmp_path)])
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "编码错误" in out


def test_build_unknown_extra_exits_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """fsp b --extra 指定未知分组时打印中文错误并以退出码 2 退出（ProjectError 被兜底）."""
    _make_minimal_project(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["b", str(tmp_path), "--extra", "nope"])
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "未知的 extras 分组" in out


def test_build_fspack_error_from_build_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """构建流程内部抛 FspackError 子类（如 DependencyError）时同样被 main 兜底为退出码 2."""
    from fspack.exceptions import DependencyError

    _make_minimal_project(tmp_path)

    def fake_build(*_args: Any, **_kwargs: Any) -> None:
        raise DependencyError("wheel 下载失败: connection reset")

    monkeypatch.setattr("fspack.builder.build", fake_build)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["b", str(tmp_path)])
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "wheel 下载失败" in out


def _make_minimal_project(tmp_path: Path) -> Path:
    """在 tmp_path 下创建最小可解析项目（pyproject.toml + 入口脚本）.

    cli.main 在 dispatch 到 builder.build 前会调用 ProjectInfo.from_dir
    读取 [tool.fspack] build_defaults，需要可解析的 pyproject.toml。
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return tmp_path


def _capture_build() -> tuple[dict[str, Any], Any]:
    """构造 fake_build 与 captured dict，fake_build 签名匹配 builder.build()."""
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: Platform | None = None,
        options: BuildOptions | None = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: ProfileOptions | None = None,
        auto_clean: bool = False,
    ) -> None:
        captured["project"] = project
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["target"] = target
        captured["options"] = options
        captured["extra_index_urls"] = extra_index_urls
        captured["find_links"] = find_links
        captured["dry_run"] = dry_run

    return captured, fake_build


def test_build_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--mirror", "aliyun"])
    assert called["project"] == tmp_path.resolve()
    assert called["mirror"] == get_mirror("aliyun")


def test_build_default_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    monkeypatch.chdir(tmp_path)
    cli.main(["build"])
    assert called["project"] == tmp_path.resolve()


def test_build_custom_py_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--py-version", "3.12.3"])
    assert called["py_version"] == "3.12.3"


def test_build_target_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--target", "linux"])
    assert called["target"] is Platform.LINUX


def test_build_target_windows_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--target", "windows"])
    assert called["target"] is Platform.WINDOWS


def test_build_target_macos_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--target macos` 解析为 Platform.MACOS."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--target", "macos"])
    assert called["target"] is Platform.MACOS


def test_build_keep_module_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--keep-module", "PySide2.QtGui", "--keep-module", "PySide2.QtNetwork"])
    assert called["options"].keep_modules == {"PySide2.QtGui", "PySide2.QtNetwork"}


def test_build_lazy_import_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--lazy-import numpy,pandas` 解析为元组封装到 BuildOptions.lazy_imports."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--lazy-import", "numpy,pandas"])
    assert called["options"].lazy_imports == ("numpy", "pandas")


def test_build_lazy_import_default_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --lazy-import 时 lazy_imports 为空元组."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].lazy_imports == ()


def test_build_icon_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fspack b <project> --icon <path>` 解析为绝对路径并封装到 BuildOptions.icon."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    icon_abs = tmp_path / "custom.ico"
    cli.main(["b", str(tmp_path), "--icon", str(icon_abs)])
    assert called["options"].icon == icon_abs.resolve()


def test_build_no_icon_passes_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --icon 时 BuildOptions.icon 为 None（由 builder 回退到默认 app.ico）."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].icon is None


def test_build_pyc_options_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--no-pyc`/`--pyc-strip`/`--no-stdlib-trim` 解析并封装到 BuildOptions."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--no-pyc", "--pyc-strip", "--no-stdlib-trim"])
    assert called["options"].no_pyc is True
    assert called["options"].pyc_strip is True
    assert called["options"].no_stdlib_trim is True


def test_build_pyc_options_default_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 pyc 选项时 BuildOptions 各开关均为默认值（开启精简与预编译）."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    opts = called["options"]
    assert isinstance(opts, BuildOptions)
    assert opts.no_pyc is False
    assert opts.pyc_strip is False
    assert opts.no_stdlib_trim is False
    # --pyc-optimize 默认 2（与 cli.py argparse default 一致）
    assert opts.pyc_optimize == 2
    assert opts.no_site is False
    assert opts.nuitka is False


def test_build_splash_and_stdlib_zip_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--splash`/`--no-stdlib-zip` 解析并封装到 BuildOptions."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--splash", "--no-stdlib-zip"])
    assert called["options"].splash is True
    assert called["options"].no_stdlib_zip is True


def test_build_splash_and_stdlib_zip_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定时 splash 关闭（默认不启用启动画面）、stdlib zip 化开启."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].splash is False
    assert called["options"].no_stdlib_zip is False


def test_build_slim_stdlib_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--slim-stdlib aggressive` 解析并封装到 BuildOptions；未指定默认 default."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--slim-stdlib", "aggressive"])
    assert called["options"].slim_stdlib == "aggressive"

    cli.main(["b", str(tmp_path)])
    assert called["options"].slim_stdlib == "default"


def test_build_new_options_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--pyc-optimize`/`--no-site`/`--nuitka` 解析并封装到 BuildOptions."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--pyc-optimize", "2", "--no-site", "--nuitka"])
    assert called["options"].pyc_optimize == 2
    assert called["options"].no_site is True
    assert called["options"].nuitka is True


def test_build_pyc_optimize_invalid_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--pyc-optimize` 仅接受 0/1/2，非法值 argparse 报错."""
    _make_minimal_project(tmp_path)
    monkeypatch.setattr("fspack.builder.build", lambda *a, **kw: None)
    with pytest.raises(SystemExit):
        cli.main(["b", str(tmp_path), "--pyc-optimize", "3"])


def _make_project_with_fspack_config(tmp_path: Path, fspack_config: str) -> Path:
    """在 tmp_path 下创建带 [tool.fspack] 配置的最小项目."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0.1"\n\n{fspack_config}\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return tmp_path


def test_build_config_defaults_applied_when_cli_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[tool.fspack] build_defaults 在 CLI 未指定对应标志时作为回退默认值."""
    _make_project_with_fspack_config(
        tmp_path,
        "[tool.fspack]\nnuitka = true\npyc_strip = true\nno_site = true\npyc_optimize = 1\nno_pyc = true\nno_stdlib_trim = true\n",
    )
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    opts = called["options"]
    assert opts.nuitka is True
    assert opts.pyc_strip is True
    assert opts.no_site is True
    assert opts.pyc_optimize == 1
    assert opts.no_pyc is True
    assert opts.no_stdlib_trim is True


def test_build_cli_flag_overrides_config_pyc_optimize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --pyc-optimize 显式指定优先于 [tool.fspack] pyc_optimize 配置."""
    _make_project_with_fspack_config(tmp_path, "[tool.fspack]\npyc_optimize = 1\n")
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--pyc-optimize", "0"])
    assert called["options"].pyc_optimize == 0


def test_build_cli_and_config_merged_with_any(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """布尔开关用 any([cli, config]) 合并：配置启用 + CLI 未指定 → 启用."""
    _make_project_with_fspack_config(tmp_path, "[tool.fspack]\nnuitka = true\n")
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].nuitka is True


def test_build_cli_nuitka_flag_with_no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 [tool.fspack] 配置时 --nuitka CLI 标志正常生效."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--nuitka"])
    assert called["options"].nuitka is True


def test_build_config_pyc_optimize_default_when_both_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 与 [tool.fspack] 均未指定 pyc_optimize 时回退到默认值 2."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].pyc_optimize == 2


def test_run_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_run(project: Path, rest_args: list[str] | None = None, options: RunOptions | None = None) -> None:
        called["project"] = project
        called["rest"] = rest_args
        called["options"] = options

    monkeypatch.setattr("fspack.runner.run", fake_run)
    cli.main(["r", str(tmp_path), "--", "--foo", "bar"])
    assert called["rest"] == ["--foo", "bar"]


def test_run_debug_flag_after_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fspack r <project> --debug` 应解析为 debug 标志,而非透传参数。

    回归测试:曾用 argparse.REMAINDER 导致 --debug 被捕获到 rest_args,
    改用 nargs="*" 后 --debug 正确解析为 fspack 选项。
    """
    called: dict[str, Any] = {}

    def fake_run(project: Path, rest_args: list[str] | None = None, options: RunOptions | None = None) -> None:
        called["project"] = project
        called["rest"] = rest_args
        called["options"] = options

    monkeypatch.setattr("fspack.runner.run", fake_run)
    cli.main(["r", str(tmp_path), "--debug"])
    assert called["options"].debug is True
    assert called["rest"] == []


def test_run_entry_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fspack r <project> --entry cli` 解析 entry 参数."""
    called: dict[str, Any] = {}

    def fake_run(project: Path, rest_args: list[str] | None = None, options: RunOptions | None = None) -> None:
        called["entry"] = options.entry if options else None

    monkeypatch.setattr("fspack.runner.run", fake_run)
    cli.main(["r", str(tmp_path), "--entry", "cli"])
    assert called["entry"] == "cli"


def test_run_profile_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fspack r <project> --profile` 解析 profile 开关."""
    called: dict[str, Any] = {}

    def fake_run(project: Path, rest_args: list[str] | None = None, options: RunOptions | None = None) -> None:
        called["profile"] = options.profile if options else None

    monkeypatch.setattr("fspack.runner.run", fake_run)
    cli.main(["r", str(tmp_path), "--profile"])
    assert called["profile"].enabled is True


def test_profile_short_aliases_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`-P`/`-PO`/`-PC` 短别名与长选项等价（-PO 精确匹配优先于 -P 前缀）."""
    called: dict[str, Any] = {}

    def fake_run(project: Path, rest_args: list[str] | None = None, options: RunOptions | None = None) -> None:
        called["profile"] = options.profile if options else None

    monkeypatch.setattr("fspack.runner.run", fake_run)
    ref = tmp_path / "base.json"
    cli.main(["r", str(tmp_path), "-P", "-PO", str(tmp_path / "perflogs"), "-PC", str(ref)])
    assert called["profile"].enabled is True
    assert called["profile"].out == (tmp_path / "perflogs").resolve()
    assert called["profile"].compare == str(ref)


def test_profile_short_aliases_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build 侧 `-P`/`-PO`/`-PC` 短别名与长选项等价."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    called: dict[str, Any] = {}

    def fake_build(*a: Any, **kw: Any) -> None:
        called["profile"] = kw.get("profile")

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "-P", "-PO", str(tmp_path / "perflogs"), "-PC"])
    assert called["profile"].enabled is True
    assert called["profile"].out == (tmp_path / "perflogs").resolve()
    # -PC 不带值 → 哨兵 "trend"（历次趋势表）
    assert called["profile"].compare == "trend"


def test_clean_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Path] = {}
    monkeypatch.setattr("fspack.builder.clean_dist", lambda project: called.__setitem__("p", project))
    cli.main(["c", str(tmp_path)])
    assert called["p"] == tmp_path.resolve()


def _capture_build_release() -> tuple[dict[str, Any], Any]:
    """构造 fake_build_release 与 captured dict，签名匹配 installer.build_release()."""
    from fspack.packaging.installer import ReleaseRequest, SignOptions

    called: dict[str, Any] = {}

    def fake_build_release(
        req: ReleaseRequest,
        *,
        target: object = None,
        fmt: str = "auto",
        sign: SignOptions | None = None,
    ) -> list[Path]:
        so = sign or SignOptions()
        called["project"] = req.project_dir
        called["mirror"] = req.mirror
        called["py_version"] = req.py_version
        called["no_build"] = req.no_build
        called["target"] = target
        called["fmt"] = fmt
        called["codesign"] = so.codesign
        called["extras"] = req.extras
        called["sign_exe"] = so.sign_exe
        called["sign_exe_certificate"] = so.sign_exe_certificate
        called["sign_exe_password"] = so.sign_exe_password
        called["sign_deb"] = so.sign_deb
        called["sign_deb_key"] = so.sign_deb_key
        return []

    return called, fake_build_release


def test_package_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """package 子命令直接调用 installer.build_release，校验参数透传."""
    _make_minimal_project(tmp_path)
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--mirror", "aliyun", "--no-build"])
    assert called["project"] == tmp_path.resolve()
    # mirror 经 get_mirror 转为 MirrorConfig，校验 name 字段
    assert called["mirror"].name == "阿里云"
    assert called["no_build"] is True
    assert called["codesign"] is False
    # 未指定 --extra 时透传 None（让 build_release 用配置默认）
    assert called["extras"] is None
    # 签名默认关闭
    assert called["sign_exe"] is False
    assert called["sign_deb"] is False
    assert called["sign_exe_certificate"] is None
    assert called["sign_exe_password"] is None
    assert called["sign_deb_key"] is None


def test_package_codesign_flag_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fsp p <project> --codesign` 透传到 build_release(codesign=True)."""
    _make_minimal_project(tmp_path)
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--codesign"])
    assert called["codesign"] is True


def test_package_format_choices_include_pkg_dmg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--format` choices 含 pkg/dmg，解析后透传到 build_release."""
    _make_minimal_project(tmp_path)
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--format", "pkg"])
    assert called["fmt"] == "pkg"


def test_drop_separator() -> None:
    assert cli._drop_separator(["--", "a", "b"]) == ["a", "b"]
    assert cli._drop_separator(["a", "b"]) == ["a", "b"]
    assert cli._drop_separator([]) == []


def test_parse_lazy_imports_none_uses_base() -> None:
    """CLI 未指定 --lazy-import 时用配置默认 base."""
    assert cli._parse_lazy_imports(None, ("numpy",)) == ("numpy",)


def test_parse_lazy_imports_empty_string_clears() -> None:
    """--lazy-import '' 显式清除：返回空元组."""
    assert cli._parse_lazy_imports("", ("numpy",)) == ()


def test_parse_lazy_imports_single() -> None:
    """--lazy-import numpy 解析为单元素元组."""
    assert cli._parse_lazy_imports("numpy", ()) == ("numpy",)


def test_parse_lazy_imports_multiple() -> None:
    """--lazy-import numpy,pandas 解析为多元素元组."""
    assert cli._parse_lazy_imports("numpy,pandas", ()) == ("numpy", "pandas")


def test_parse_lazy_imports_strips_whitespace() -> None:
    """--lazy-import 值含空白：strip 后去空元素."""
    assert cli._parse_lazy_imports(" numpy , pandas , ", ()) == ("numpy", "pandas")


def test_parse_lazy_imports_dedupes() -> None:
    """--lazy-import numpy,numpy 去重保留首次出现."""
    assert cli._parse_lazy_imports("numpy,numpy,pandas", ()) == ("numpy", "pandas")


def test_parse_lazy_imports_overrides_base() -> None:
    """CLI 完全覆盖配置默认（与 extras 语义一致，非合并）."""
    assert cli._parse_lazy_imports("pandas", ("numpy",)) == ("pandas",)


def test_invalid_mirror_rejected(tmp_path: Path) -> None:
    """非法 --mirror 在执行期校验失败（SystemExit(2)，与 argparse 退出码一致）."""
    _make_minimal_project(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["b", str(tmp_path), "--mirror", "nope"])


# ---------- 私有包源（--extra-index-url / --find-links）----------


def test_build_extra_index_url_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--extra-index-url 可多次指定，作为元组透传给 build()."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(
        [
            "b",
            str(tmp_path),
            "--extra-index-url",
            "https://pypi.company.com/simple/",
            "--extra-index-url",
            "https://mirror.example.com/pypi",
        ]
    )
    assert called["extra_index_urls"] == (
        "https://pypi.company.com/simple/",
        "https://mirror.example.com/pypi",
    )


def test_build_find_links_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--find-links 可多次指定，作为元组透传给 build()."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--find-links", "./wheels", "--find-links", "https://example.com/wheels/"])
    assert called["find_links"] == ("./wheels", "https://example.com/wheels/")


def test_build_no_private_sources_defaults_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --extra-index-url/--find-links 时传空元组给 build()."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["extra_index_urls"] == ()
    assert called["find_links"] == ()


def test_build_extra_index_url_combined_with_find_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同时指定 --extra-index-url 与 --find-links，二者各自透传."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(
        [
            "b",
            str(tmp_path),
            "--extra-index-url",
            "https://pypi.company.com/simple/",
            "--find-links",
            "./wheels",
        ]
    )
    assert called["extra_index_urls"] == ("https://pypi.company.com/simple/",)
    assert called["find_links"] == ("./wheels",)


# ---------- CLI 启动懒加载 ----------


def test_build_parser_does_not_load_config() -> None:
    """build_parser() 不触发 fspack.config 导入（--mirror 无 choices 校验）."""
    import subprocess
    import sys

    code = (
        "import sys; import fspack.cli; fspack.cli.build_parser(); sys.exit(1 if 'fspack.config' in sys.modules else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert result.returncode == 0, "build_parser() 不应导入 fspack.config"


def test_mirror_help_lists_all_mirror_keys() -> None:
    """--mirror help 文本与 MIRRORS 键同步（防止静态列表漂移）."""
    from fspack.config import MIRRORS

    parser = cli.build_parser()
    helps: list[str] = []
    for action in parser._subparsers._group_actions:  # type: ignore[attr-defined]
        for choice_action in action.choices.values():
            helps.extend(a.help or "" for a in choice_action._actions if "--mirror" in (a.option_strings or []))
    assert helps, "未找到 --mirror 参数"
    for key in MIRRORS:
        assert any(key in h for h in helps), f"--mirror help 缺少镜像键: {key}"


def test_resolve_mirror_invalid_exits_with_code_2(tmp_path: Path) -> None:
    """非法 --mirror 在执行期报错并以退出码 2 退出（与 argparse 一致）."""
    with pytest.raises(SystemExit) as exc_info:
        cli._resolve_mirror("nope")
    assert exc_info.value.code == 2


def test_cli_module_no_top_level_console_import() -> None:
    """cli.py 顶部不再导入 fspack.console（懒加载优化）.

    验证 ``from fspack.console import console`` 不在模块顶层导入，
    确保 ``fsp --help`` 不触发 rich 模块加载（~17ms）。
    """
    import inspect

    source = inspect.getsource(cli)
    # 顶部导入区（main 之前的全局代码）不应含 console
    top_section = source.split("def main")[0]
    assert "from fspack.console" not in top_section
    assert "import fspack.console" not in top_section


def test_cli_module_no_top_level_platform_import() -> None:
    """cli.py 顶部不再导入 fspack.platform（懒加载优化）.

    Platform 仅在 TYPE_CHECKING 块和 _parse_target 函数内导入。
    """
    import inspect

    source = inspect.getsource(cli)
    top_section = source.split("def main")[0]
    # TYPE_CHECKING 块内的导入允许（含 MirrorConfig/Platform 两行注解导入）
    type_checking_block = (
        "if TYPE_CHECKING:\n    from fspack.config import MirrorConfig\n    from fspack.platform import Platform\n"
    )
    assert "from fspack.platform import Platform" not in top_section.replace(type_checking_block, "")


def test_help_does_not_load_heavy_modules() -> None:
    """import 基线：``fsp --help`` 全链路不加载 config/console/platform/rich.

    子进程内执行 ``main(['--help'])``（argparse 打印帮助后以 SystemExit(0) 退出），
    随后断言重模块均未进入 ``sys.modules``。任何顶部误引入重模块的回退都会被
    本测试拦截（典型成本：config ~20ms / rich ~17ms / platform 轻量但链式引入）。
    """
    import subprocess
    import sys

    code = (
        "import sys\n"
        "from fspack.cli import main\n"
        "try:\n"
        "    main(['--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "heavy = [m for m in ('fspack.config', 'fspack.console', 'fspack.platform', 'rich') if m in sys.modules]\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert result.returncode == 0, "fsp --help 不应加载 config/console/platform/rich"


# ---------- 安全加固：哈希校验与 SBOM 开关 ----------


def test_build_require_hashes_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fsp b <project> --require-hashes` 解析 ns.require_hashes 为 True，封装到 BuildOptions."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--require-hashes"])
    assert called["options"].require_hashes is True


def test_build_require_hashes_default_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --require-hashes 时 BuildOptions.require_hashes 为 False."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].require_hashes is False


def test_build_require_hashes_config_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[tool.fspack] require_hashes = true 在 CLI 未指定时作为回退默认值."""
    _make_project_with_fspack_config(tmp_path, "[tool.fspack]\nrequire_hashes = true\n")
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].require_hashes is True


def test_build_no_sbom_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fsp b <project> --no-sbom` 解析 ns.no_sbom 为 True，封装到 BuildOptions."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--no-sbom"])
    assert called["options"].no_sbom is True


def test_build_no_sbom_default_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --no-sbom 时 BuildOptions.no_sbom 为 False."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].no_sbom is False


def test_build_no_sbom_config_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[tool.fspack] no_sbom = true 在 CLI 未指定时作为回退默认值."""
    _make_project_with_fspack_config(tmp_path, "[tool.fspack]\nno_sbom = true\n")
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].no_sbom is True


# ---------- 安全加固：Windows exe 代码签名 ----------


def test_package_sign_exe_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fsp p <project> --sign-exe` 解析 ns.sign_exe 为 True，透传到 build_release."""
    _make_minimal_project(tmp_path)
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--sign-exe", "--sign-exe-certificate", str(tmp_path / "cert.pfx")])
    assert called["sign_exe"] is True


def test_package_sign_exe_certificate_resolved_to_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--sign-exe-certificate <path>` 解析为绝对路径透传."""
    _make_minimal_project(tmp_path)
    cert = tmp_path / "cert.pfx"
    cert.write_bytes(b"fake cert")
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--sign-exe", "--sign-exe-certificate", str(cert)])
    assert called["sign_exe_certificate"] == cert.resolve()


def test_package_sign_exe_password_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--sign-exe-password <pwd>` 原样透传到 build_release."""
    _make_minimal_project(tmp_path)
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(
        [
            "p",
            str(tmp_path),
            "--sign-exe",
            "--sign-exe-certificate",
            str(tmp_path / "cert.pfx"),
            "--sign-exe-password",
            "s3cret",
        ]
    )
    assert called["sign_exe"] is True
    assert called["sign_exe_password"] == "s3cret"


def test_package_sign_exe_certificate_config_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[tool.fspack] sign-exe-certificate 在 CLI 未指定时作为回退（相对 cwd 解析为绝对路径）."""
    _make_project_with_fspack_config(tmp_path, '[tool.fspack]\nsign-exe-certificate = "cert.pfx"\n')
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--sign-exe"])
    # 配置层证书字符串原样透传，cli._run_package 用 Path(cfg).resolve() 解析为绝对路径
    assert called["sign_exe_certificate"] == Path("cert.pfx").resolve()
    assert called["sign_exe"] is True


def test_package_sign_exe_password_config_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[tool.fspack] sign-exe-password 在 CLI 未指定时作为回退."""
    _make_project_with_fspack_config(tmp_path, '[tool.fspack]\nsign-exe-password = "cfg-pwd"\n')
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--sign-exe", "--sign-exe-certificate", str(tmp_path / "cert.pfx")])
    assert called["sign_exe_password"] == "cfg-pwd"


def test_package_sign_exe_cli_overrides_config_certificate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --sign-exe-certificate 优先于 [tool.fspack] sign-exe-certificate."""
    _make_project_with_fspack_config(tmp_path, '[tool.fspack]\nsign-exe-certificate = "cfg-cert.pfx"\n')
    cli_cert = tmp_path / "cli-cert.pfx"
    cli_cert.write_bytes(b"cli cert")
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--sign-exe", "--sign-exe-certificate", str(cli_cert)])
    assert called["sign_exe_certificate"] == cli_cert.resolve()


def test_package_sign_exe_password_cli_overrides_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --sign-exe-password 优先于 [tool.fspack] sign-exe-password."""
    _make_project_with_fspack_config(tmp_path, '[tool.fspack]\nsign-exe-password = "cfg-pwd"\n')
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(
        [
            "p",
            str(tmp_path),
            "--sign-exe",
            "--sign-exe-certificate",
            str(tmp_path / "cert.pfx"),
            "--sign-exe-password",
            "cli-pwd",
        ]
    )
    assert called["sign_exe_password"] == "cli-pwd"


# ---------- 安全加固：Linux .deb GPG 签名 ----------


def test_package_sign_deb_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fsp p <project> --sign-deb` 解析 ns.sign_deb 为 True，透传到 build_release."""
    _make_minimal_project(tmp_path)
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--sign-deb", "--sign-deb-key", "0x12345678"])
    assert called["sign_deb"] is True
    assert called["sign_deb_key"] == "0x12345678"


def test_package_sign_deb_key_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--sign-deb-key <key>` 原样透传到 build_release."""
    _make_minimal_project(tmp_path)
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--sign-deb", "--sign-deb-key", "user@example.com"])
    assert called["sign_deb"] is True
    assert called["sign_deb_key"] == "user@example.com"


def test_package_sign_deb_key_config_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[tool.fspack] sign-deb-key 在 CLI 未指定时作为回退."""
    _make_project_with_fspack_config(tmp_path, '[tool.fspack]\nsign-deb-key = "0xABCD1234"\n')
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--sign-deb"])
    assert called["sign_deb"] is True
    assert called["sign_deb_key"] == "0xABCD1234"


def test_package_sign_deb_key_cli_overrides_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --sign-deb-key 优先于 [tool.fspack] sign-deb-key."""
    _make_project_with_fspack_config(tmp_path, '[tool.fspack]\nsign-deb-key = "cfg-key"\n')
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--sign-deb", "--sign-deb-key", "cli-key"])
    assert called["sign_deb_key"] == "cli-key"


def test_package_sign_deb_without_key_uses_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--sign-deb` 未指定 key 且无配置时 sign_deb_key 为 None（用 GPG 默认密钥）."""
    _make_minimal_project(tmp_path)
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--sign-deb"])
    assert called["sign_deb"] is True
    assert called["sign_deb_key"] is None


def test_package_no_sign_flags_defaults_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定任何签名标志时 sign_exe/sign_deb 均为 False."""
    _make_minimal_project(tmp_path)
    called, fake_build_release = _capture_build_release()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path)])
    assert called["sign_exe"] is False
    assert called["sign_deb"] is False


# ---------- builder import 热路径懒加载基线 ----------


def test_net_module_no_top_level_heavy_imports() -> None:
    """源码基线：net.py 顶部不导入 urllib.request/rich.progress/console（运行时重模块）.

    守护 iter-121~122 net.py 顶部轻量化：``urllib.request``（~15ms）、
    ``rich.progress`` 多 column 类（~8ms）与 ``fspack.console`` 单例全部移到
    ``Downloader.download`` 方法内延迟导入，确保 ``import fspack.packaging.net``
    不连带加载这些重模块。``StageRecorder`` 在 TYPE_CHECKING 块内仅为类型注解，
    运行时不执行，不在守护范围。

    通过 inspect 源码检查顶部导入区（``class Downloader`` 之前）不含运行时重模块 import，
    避免回退引入。iter-122 将测试 patch 路径从 ``fspack.packaging.net.urllib.request.urlopen``
    改为全局 ``urllib.request.urlopen``，释放 net 顶部 urllib 约束。
    """
    import inspect
    import re

    from fspack.packaging import net

    source = inspect.getsource(net)
    top_section = source.split("class Downloader")[0]
    # 移除 TYPE_CHECKING 块内容（仅类型注解，运行时不执行）
    top_runtime = re.sub(r"if TYPE_CHECKING:.*?(?=\n\n|\n[^\s])", "", top_section, flags=re.DOTALL)
    # 顶部运行时导入区不应含 urllib.request / rich.progress / console
    assert "import urllib.request" not in top_runtime
    assert "from rich.progress import" not in top_runtime
    assert "from fspack.console import" not in top_runtime


def test_builder_import_does_not_load_urllib_request() -> None:
    """import 基线：``import fspack.builder`` 不触发 ``urllib.request`` 加载.

    iter-122 将 ``urllib.request`` 从 net.py 顶部移到 ``Downloader.download``
    方法内延迟导入。本测试在子进程内执行 ``import fspack.builder``，断言
    ``urllib.request`` 未进入 ``sys.modules``。任何顶部误引入 ``urllib.request``
    的回退都会被本测试拦截（典型成本：urllib.request 链式加载 ~15ms）。
    """
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import fspack.builder\n"
        "heavy = [m for m in ('urllib.request',) if m in sys.modules]\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert result.returncode == 0, "import fspack.builder 不应加载 urllib.request"


def test_builder_import_does_not_load_progress() -> None:
    """import 基线：``import fspack.builder`` 不触发 ``fspack.progress`` 加载.

    iter-123 将 ``BuildTracker``/``StageRecorder`` 从 stages.py 与 pipeline/__init__.py
    顶部移到 TYPE_CHECKING 块（类型注解）与 ``build()`` 方法内（实例化）。本测试
    在子进程内执行 ``import fspack.builder``，断言 ``fspack.progress`` 未进入
    ``sys.modules``。任何顶部误引入 ``fspack.progress`` 的回退都会被本测试拦截
    （典型成本：fspack.progress 链式加载 rich.progress/rich.table ~12ms）。
    """
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import fspack.builder\n"
        "heavy = [m for m in ('fspack.progress',) if m in sys.modules]\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert result.returncode == 0, "import fspack.builder 不应加载 fspack.progress"


def test_builder_import_does_not_load_console() -> None:
    """import 基线：``import fspack.builder`` 不触发 ``fspack.console`` 加载.

    iter-124 将 ``fspack.console`` 与 ``fspack.packaging.profile`` 从
    pipeline/__init__.py 顶部移到 ``_execute_build``/``_print_build_plan``/
    ``build()`` 方法内延迟导入（profile 顶部 ``from fspack.console import console``
    会连锁触发 rich.console/rich.logging/rich.theme ~17ms）。本测试在子进程内
    执行 ``import fspack.builder``，断言 ``fspack.console`` 与
    ``fspack.packaging.profile`` 未进入 ``sys.modules``。任何顶部误引入的回退
    都会被本测试拦截。
    """
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import fspack.builder\n"
        "heavy = [m for m in ('fspack.console', 'fspack.packaging.profile') if m in sys.modules]\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert result.returncode == 0, "import fspack.builder 不应加载 fspack.console/fspack.packaging.profile"


def test_pipeline_module_no_top_level_heavy_imports() -> None:
    """源码基线：pipeline/__init__.py 顶部不导入 fspack.console/fspack.progress/fspack.packaging.profile.

    守护 iter-124/135 pipeline/__init__.py 顶部轻量化：``fspack.console`` 单例
    （~17ms）、``fspack.progress`` 的 ``BuildTracker``（连锁 rich.progress/rich.table
    ~12ms）、``fspack.packaging.profile`` 的 ``ProfileContext``（连锁 fspack.console）
    全部移到 ``build``/``_execute_build``/``_print_build_plan`` 函数内延迟导入，
    ``BuildTracker``/``ProfileContext`` 类型注解仅在 TYPE_CHECKING 块内（运行时不执行）。
    子进程级守护见 :func:`test_builder_import_does_not_load_console`/
    :func:`test_builder_import_does_not_load_progress`，本测试为源码级防回退。
    """
    import inspect
    import re

    from fspack.packaging import pipeline

    source = inspect.getsource(pipeline)
    # 顶部导入区锚点：第一个函数定义 ``def resolve_project_info``
    top_section = source.split("def resolve_project_info")[0]
    # 移除 TYPE_CHECKING 块内容（仅类型注解，运行时不执行）
    top_runtime = re.sub(r"if TYPE_CHECKING:.*?(?=\n\n|\n[^\s])", "", top_section, flags=re.DOTALL)
    assert "from fspack.console import" not in top_runtime
    assert "import fspack.console" not in top_runtime
    assert "from fspack.progress import" not in top_runtime
    assert "import fspack.progress" not in top_runtime
    assert "from fspack.packaging.profile import" not in top_runtime
    assert "import fspack.packaging.profile" not in top_runtime


def test_downloader_module_no_top_level_threading_import() -> None:
    """源码基线：wheels/downloader.py 顶部不导入 threading.

    守护 iter-135 downloader.py 顶部轻量化：``threading`` 移到 ``_stream_subprocess``
    函数内延迟导入，保持模块顶部零 stdlib 副作用约定（与 net.py/runtime.py 等热路径
    模块一致）。虽然 site.py 启动期已加载 threading（无运行时性能收益），但保持
    顶部轻量约定有助于源码可读性与静态分析一致性。``StageRecorder`` 在 TYPE_CHECKING
    块内仅为类型注解，运行时不执行，不在守护范围。
    """
    import inspect
    import re

    from fspack.packaging.wheels import downloader

    source = inspect.getsource(downloader)
    # 顶部导入区锚点：第一个函数定义（``def _find_pip_python`` 之前的所有全局代码）
    top_section = source.split("\ndef ")[0]
    # 移除 TYPE_CHECKING 块内容（仅类型注解，运行时不执行）
    top_runtime = re.sub(r"if TYPE_CHECKING:.*?(?=\n\n|\n[^\s])", "", top_section, flags=re.DOTALL)
    assert "import threading" not in top_runtime


# --- iter-148 前后端分离 Web 打包：--open-browser CLI 标志 ---


def test_build_open_browser_flag_present_in_help() -> None:
    """build 子命令含 --open-browser 标志."""
    parser = cli.build_parser()
    # --open-browser 在 build 子解析器中，非顶层 parser
    sub_action = next(a for a in parser._actions if hasattr(a, "choices") and isinstance(a.choices, dict))
    build_parser = sub_action.choices["build"]  # type: ignore[index]
    flag_names = {opt for a in build_parser._actions for opt in a.option_strings}
    assert "--open-browser" in flag_names


def test_all_subparser_help_render() -> None:
    """全量渲染各子命令 help，拦截 help 文本中的 argparse 格式化错误.

    argparse 以 ``help % params`` 展开 help 文本，裸 ``%``（如 ``>10%``）会让
    ``-h`` 渲染直接抛 ``ValueError: badly formed help string``。Python 3.14 在
    ``add_argument`` 时即校验（构建 parser 就崩），≤3.13 仅在渲染时抛——用户
    执行 ``fsp b -h`` 才触发。逐个渲染所有子命令 help 可在全版本拦截此类问题。
    """
    parser = cli.build_parser()
    parser.format_help()
    sub_action = next(a for a in parser._actions if hasattr(a, "choices") and isinstance(a.choices, dict))
    for sub in sub_action.choices.values():  # type: ignore[union-attr]
        sub.format_help()


def test_build_open_browser_flag_sets_build_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fsp b <project> --open-browser` 解析为 BuildOptions.open_browser=True."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--open-browser"])
    assert called["options"].open_browser is True


def test_build_no_win7_dll_flag_sets_build_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fsp b <project> --no-win7-dll` 解析为 BuildOptions.no_win7_dll=True.

    网络受限环境跳过 Win7 组件替换（3.12+ 需从 GitHub 下载重编译版 embed zip），
    避免下载失败阻断构建；产物仅面向 Win8+/Win10+。
    """
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--no-win7-dll"])
    assert called["options"].no_win7_dll is True


def test_build_no_win7_dll_default_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 --no-win7-dll 标志且未配置时默认 False（保持 Win7 兼容注入）."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].no_win7_dll is False


def test_build_no_win7_dll_config_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[tool.fspack] no_win7_dll = true 配置（无 CLI 标志）同样生效."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nno_win7_dll = true\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].no_win7_dll is True
