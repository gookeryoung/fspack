"""图标资源处理测试：favicon 搜索与图片格式转换."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest

from fspack.builder import _DEFAULT_ICON, _resolve_project_icon
from fspack.packaging.icon import (
    SUPPORTED_IMAGE_EXTS,
    _convert_image_to_ico,
    ensure_ico,
    find_favicon,
)
from fspack.platform import Platform

# Pillow 是否可用（图片转换测试依赖）
_HAS_PIL = importlib.util.find_spec("PIL") is not None
_skip_no_pil = pytest.mark.skipif(not _HAS_PIL, reason="Pillow 未安装，跳过图片转换测试")


# --- find_favicon 测试 ---


def test_find_favicon_no_dir_returns_none(tmp_path: Path) -> None:
    """目录不存在时返回 None."""
    assert find_favicon(tmp_path / "missing") is None


def test_find_favicon_empty_dir_returns_none(tmp_path: Path) -> None:
    """空目录返回 None."""
    assert find_favicon(tmp_path) is None


def test_find_favicon_finds_ico(tmp_path: Path) -> None:
    """找到 .ico 返回路径."""
    (tmp_path / "favicon.ico").write_bytes(b"ico")
    result = find_favicon(tmp_path)
    assert result == tmp_path / "favicon.ico"


def test_find_favicon_finds_png(tmp_path: Path) -> None:
    """找到 .png 返回路径."""
    (tmp_path / "favicon.png").write_bytes(b"png")
    result = find_favicon(tmp_path)
    assert result == tmp_path / "favicon.png"


def test_find_favicon_priority_ico_over_png(tmp_path: Path) -> None:
    """同目录内 .ico 优先于 .png."""
    (tmp_path / "favicon.png").write_bytes(b"png")
    (tmp_path / "favicon.ico").write_bytes(b"ico")
    result = find_favicon(tmp_path)
    assert result is not None
    assert result.suffix == ".ico"


def test_find_favicon_priority_png_over_bmp(tmp_path: Path) -> None:
    """同目录内 .png 优先于 .bmp."""
    (tmp_path / "favicon.bmp").write_bytes(b"bmp")
    (tmp_path / "favicon.png").write_bytes(b"png")
    result = find_favicon(tmp_path)
    assert result is not None
    assert result.suffix == ".png"


def test_find_favicon_skips_excluded_dirs(tmp_path: Path) -> None:
    """跳过 dist/build/.venv 等排除目录下的 favicon."""
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "favicon.ico").write_bytes(b"ico")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "favicon.png").write_bytes(b"png")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "favicon.ico").write_bytes(b"ico")
    # 排除目录下都不命中，返回 None
    assert find_favicon(tmp_path) is None


def test_find_favicon_finds_in_subdir(tmp_path: Path) -> None:
    """在子目录中找到 favicon."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "favicon.png").write_bytes(b"png")
    result = find_favicon(tmp_path)
    assert result == tmp_path / "assets" / "favicon.png"


def test_find_favicon_shallow_dir_overrides_deep_ico(tmp_path: Path) -> None:
    """浅层目录的 .png 优先于深层目录的 .ico.

    项目根 favicon.png 优先于子目录 assets/favicon.ico，
    因为用户通常将主 favicon 放在浅层位置。
    """
    (tmp_path / "favicon.png").write_bytes(b"png")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "favicon.ico").write_bytes(b"ico")
    result = find_favicon(tmp_path)
    assert result == tmp_path / "favicon.png"


def test_find_favicon_shallow_ico_overrides_deep_png(tmp_path: Path) -> None:
    """浅层目录的 .ico 优先于深层目录的 .png."""
    (tmp_path / "favicon.ico").write_bytes(b"ico")
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "favicon.png").write_bytes(b"png")
    result = find_favicon(tmp_path)
    assert result == tmp_path / "favicon.ico"


def test_find_favicon_ignores_non_favicon_files(tmp_path: Path) -> None:
    """不匹配非 favicon 前缀的文件."""
    (tmp_path / "icon.ico").write_bytes(b"ico")
    (tmp_path / "logo.png").write_bytes(b"png")
    assert find_favicon(tmp_path) is None


def test_find_favicon_ignores_unsupported_ext(tmp_path: Path) -> None:
    """不匹配不在 SUPPORTED_IMAGE_EXTS 内的扩展名."""
    (tmp_path / "favicon.txt").write_bytes(b"txt")
    (tmp_path / "favicon.svg").write_bytes(b"svg")
    assert find_favicon(tmp_path) is None


def test_find_favicon_ignores_directory_named_favicon(tmp_path: Path) -> None:
    """名为 favicon.ico 的目录不应被当作文件命中，应继续搜索其他候选."""
    # 创建 favicon.ico 目录（rglob 会匹配到目录，is_file() 应过滤）
    (tmp_path / "favicon.ico").mkdir()
    # 创建 favicon.png 文件作为次优候选（.ico 优先级高但被目录占用）
    (tmp_path / "favicon.png").write_bytes(b"png")
    result = find_favicon(tmp_path)
    # 应跳过 favicon.ico 目录，返回 favicon.png 文件
    assert result == tmp_path / "favicon.png"


def test_find_favicon_case_insensitive_match(tmp_path: Path) -> None:
    """文件名大小写不敏感匹配（favicon.ICO 等同 favicon.ico）.

    os.walk + fname.lower() 比较在所有平台一致匹配，
    不依赖文件系统大小写敏感性。
    """
    (tmp_path / "favicon.ICO").write_bytes(b"ico")
    result = find_favicon(tmp_path)
    assert result is not None
    assert result.suffix.lower() == ".ico"
    assert result.name.lower() == "favicon.ico"


def test_supported_image_exts_contains_common_formats() -> None:
    """SUPPORTED_IMAGE_EXTS 包含常见图片格式."""
    for ext in (".ico", ".png", ".bmp", ".jpg", ".jpeg", ".gif", ".webp"):
        assert ext in SUPPORTED_IMAGE_EXTS


# --- ensure_ico 测试 ---


def test_ensure_ico_ico_returns_as_is(tmp_path: Path) -> None:
    """.ico 文件原样返回."""
    ico = tmp_path / "icon.ico"
    ico.write_bytes(b"ico")
    work = tmp_path / "work"
    result = ensure_ico(ico, work)
    assert result == ico
    # work_dir 不应被创建（无需转换）
    assert not work.exists()


def test_ensure_ico_missing_file_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """文件不存在时返回 None 并 warning."""
    with caplog.at_level(logging.WARNING):
        result = ensure_ico(tmp_path / "missing.png", tmp_path / "work")
    assert result is None
    assert "icon 文件不存在" in caplog.text


def test_ensure_ico_unsupported_format_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """不支持的格式返回 None 并 warning."""
    src = tmp_path / "icon.svg"
    src.write_bytes(b"svg")
    with caplog.at_level(logging.WARNING):
        result = ensure_ico(src, tmp_path / "work")
    assert result is None
    assert "不支持的 icon 格式" in caplog.text


@_skip_no_pil
def test_ensure_ico_converts_png_to_ico(tmp_path: Path) -> None:
    """png 转换为 ico 返回新路径."""
    from PIL import Image

    # 生成有效 PNG 图片
    src = tmp_path / "favicon.png"
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(src, format="PNG")
    work = tmp_path / "work"
    result = ensure_ico(src, work)
    assert result is not None
    assert result == work / "icon.ico"
    assert result.is_file()
    # 校验是有效 ico：ICO 头部 6 字节 = reserved(2B 全 0) + type(2B LE=1) + count(2B LE)
    # type=1 表示 ICO 格式（little-endian 存储：0x01 0x00）
    data = result.read_bytes()
    assert data[0:2] == b"\x00\x00"
    assert data[2:4] == b"\x01\x00"


@_skip_no_pil
def test_ensure_ico_converts_jpg_to_ico(tmp_path: Path) -> None:
    """jpg 转换为 ico 返回新路径."""
    from PIL import Image

    src = tmp_path / "favicon.jpg"
    Image.new("RGB", (64, 64), (0, 255, 0)).save(src, format="JPEG")
    work = tmp_path / "work"
    result = ensure_ico(src, work)
    assert result is not None
    assert result.is_file()


@_skip_no_pil
def test_ensure_ico_creates_workdir(tmp_path: Path) -> None:
    """work_dir 不存在时自动创建."""
    from PIL import Image

    src = tmp_path / "favicon.png"
    Image.new("RGBA", (32, 32)).save(src, format="PNG")
    work = tmp_path / "nested" / "work"
    result = ensure_ico(src, work)
    assert result is not None
    assert work.is_dir()


# --- _convert_image_to_ico 测试 ---


@_skip_no_pil
def test_convert_image_success(tmp_path: Path) -> None:
    """成功转换返回 True."""
    from PIL import Image

    src = tmp_path / "src.png"
    Image.new("RGBA", (48, 48), (0, 0, 255, 128)).save(src, format="PNG")
    dst = tmp_path / "out.ico"
    assert _convert_image_to_ico(src, dst) is True
    assert dst.is_file()


@_skip_no_pil
def test_convert_image_corrupt_returns_false(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """损坏的图片文件返回 False 并 warning."""
    src = tmp_path / "broken.png"
    src.write_bytes(b"not a real png")
    dst = tmp_path / "out.ico"
    with caplog.at_level(logging.WARNING):
        assert _convert_image_to_ico(src, dst) is False
    assert "图片转换 .ico 失败" in caplog.text


def test_convert_image_no_pillow_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Pillow 不可用时返回 False 并 warning."""
    # 模拟 Pillow 未安装
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "PIL":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    src = tmp_path / "src.png"
    src.write_bytes(b"png")
    dst = tmp_path / "out.ico"
    with caplog.at_level(logging.WARNING):
        assert _convert_image_to_ico(src, dst) is False
    assert "Pillow" in caplog.text
    assert not dst.is_file()


# --- _resolve_project_icon 测试 ---


def test_resolve_icon_linux_returns_none(tmp_path: Path) -> None:
    """Linux 目标始终返回 None."""
    ico = tmp_path / "icon.ico"
    ico.write_bytes(b"ico")
    result = _resolve_project_icon(ico, None, tmp_path, tmp_path / "work", Platform.LINUX)
    assert result is None


def test_resolve_icon_cli_overrides_project(tmp_path: Path) -> None:
    """CLI icon 优先于项目 icon."""
    cli_ico = tmp_path / "cli.ico"
    cli_ico.write_bytes(b"cli")
    proj_ico = tmp_path / "proj.ico"
    proj_ico.write_bytes(b"proj")
    result = _resolve_project_icon(cli_ico, proj_ico, tmp_path, tmp_path / "work", Platform.WINDOWS)
    assert result == cli_ico


def test_resolve_icon_project_overrides_favicon(tmp_path: Path) -> None:
    """项目 icon 优先于 favicon 自动搜索."""
    proj_ico = tmp_path / "declared.ico"
    proj_ico.write_bytes(b"declared")
    (tmp_path / "favicon.ico").write_bytes(b"fav")
    result = _resolve_project_icon(None, proj_ico, tmp_path, tmp_path / "work", Platform.WINDOWS)
    assert result == proj_ico


def test_resolve_icon_favicon_when_no_explicit(tmp_path: Path) -> None:
    """无显式配置时自动搜索 favicon."""
    fav = tmp_path / "favicon.ico"
    fav.write_bytes(b"fav")
    result = _resolve_project_icon(None, None, tmp_path, tmp_path / "work", Platform.WINDOWS)
    assert result == fav


def test_resolve_icon_default_when_nothing_found(tmp_path: Path) -> None:
    """无任何 icon 候选时返回默认 icon."""
    result = _resolve_project_icon(None, None, tmp_path, tmp_path / "work", Platform.WINDOWS)
    assert result == _DEFAULT_ICON


@_skip_no_pil
def test_resolve_icon_converts_favicon_png(tmp_path: Path) -> None:
    """favicon 是 png 时自动转换为 ico."""
    from PIL import Image

    fav = tmp_path / "favicon.png"
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(fav, format="PNG")
    work = tmp_path / "work"
    result = _resolve_project_icon(None, None, tmp_path, work, Platform.WINDOWS)
    assert result is not None
    assert result == work / "icon.ico"
    assert result.is_file()


def test_resolve_icon_falls_back_when_pillow_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """非 .ico favicon 且 Pillow 不可用时回退到默认 icon."""
    # 模拟 Pillow 未安装
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "PIL":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    (tmp_path / "favicon.png").write_bytes(b"fake png")
    with caplog.at_level(logging.WARNING):
        result = _resolve_project_icon(None, None, tmp_path, tmp_path / "work", Platform.WINDOWS)
    assert result == _DEFAULT_ICON
    assert "Pillow" in caplog.text


def test_resolve_icon_cli_non_ico_falls_back_when_no_pillow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 指定非 .ico 文件且 Pillow 不可用时回退到默认 icon."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "PIL":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cli_png = tmp_path / "custom.png"
    cli_png.write_bytes(b"fake png")
    result = _resolve_project_icon(cli_png, None, tmp_path, tmp_path / "work", Platform.WINDOWS)
    assert result == _DEFAULT_ICON
