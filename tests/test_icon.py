"""图标资源处理测试：favicon 搜索与图片格式转换."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Tuple, cast

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
    """跳过 dist/build/.venv/.tox/.trae 等排除目录下的 favicon."""
    for skip_dir in ("dist", "build", ".venv", ".tox", ".trae", ".mypy_cache", ".uv-cache"):
        (tmp_path / skip_dir).mkdir()
        (tmp_path / skip_dir / "favicon.ico").write_bytes(b"ico")
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
def test_convert_image_preserves_alpha_channel(tmp_path: Path) -> None:
    """转换后 ICO 保留完整 8-bit alpha 通道（含半透明像素）.

    验证 ``bitmap_format="png"`` 生效：所有尺寸条目用 PNG 格式保存，
    避免默认 BMP 格式的小尺寸条目将 alpha 退化为 1-bit AND mask。
    """
    import io
    import struct

    from PIL import Image, ImageDraw

    # 创建带透明背景 + 半透明前景的 RGBA 图片
    src = tmp_path / "alpha.png"
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))  # 完全透明背景
    draw = ImageDraw.Draw(img)
    draw.ellipse((64, 64, 192, 192), fill=(255, 0, 0, 128))  # 半透明红色圆
    img.save(src, format="PNG")

    dst = tmp_path / "out.ico"
    assert _convert_image_to_ico(src, dst) is True

    # 解析 ICO 文件结构，验证所有条目都是 PNG 格式（保留完整 alpha）
    data = dst.read_bytes()
    _reserved, _type, count = struct.unpack("<HHH", data[0:6])
    assert count == 6  # 6 档尺寸
    offset = 6
    png_256_data = b""
    for i in range(count):
        w, h, _pixels, _color = struct.unpack("<BBBB", data[offset : offset + 4])
        entry_size, offset_val = struct.unpack("<II", data[offset + 8 : offset + 16])
        width = w if w else 256
        height = h if h else 256
        entry_header = data[offset_val : offset_val + 8]
        # PNG 格式以 \x89PNG 开头；BMP 格式以 BITMAPINFOHEADER（40 00 00 00）开头
        assert entry_header.startswith(b"\x89PNG"), (
            f"条目 {i} ({width}x{height}) 应为 PNG 格式以保留 alpha，实际头部: {entry_header[:4].hex()}"
        )
        if width == 256 and height == 256:
            png_256_data = data[offset_val : offset_val + entry_size]
        offset += 16

    # 读回 256x256 PNG 条目验证 alpha 通道
    assert png_256_data, "未找到 256x256 条目"
    rgba = Image.open(io.BytesIO(png_256_data)).convert("RGBA")
    # 四角应为完全透明（alpha=0）
    for corner in ((0, 0), (255, 0), (0, 255), (255, 255)):
        pixel = _rgba_pixel(rgba, corner)
        assert pixel[3] == 0, f"角 {corner} 应透明 alpha=0，实际: {pixel}"
    # 中心应为半透明红色（alpha≈128）
    center = _rgba_pixel(rgba, (128, 128))
    assert center[0] == 255 and center[3] == 128, f"中心应半透明 (255,0,0,128)，实际: {center}"


def _extract_ico_png_entry(data: bytes, target_size: int) -> bytes:
    """从 ICO 文件二进制数据中提取指定尺寸的 PNG 条目数据.

    辅助函数：解析 ICO 文件结构，遍历条目目录找到目标尺寸的条目，
    返回其原始 PNG 字节流。用于透明通道测试中读回特定尺寸条目验证 alpha。
    """
    import struct

    _reserved, _type, count = struct.unpack("<HHH", data[0:6])
    offset = 6
    for _i in range(count):
        w, h, _pixels, _color = struct.unpack("<BBBB", data[offset : offset + 4])
        entry_size, offset_val = struct.unpack("<II", data[offset + 8 : offset + 16])
        width = w if w else 256
        height = h if h else 256
        if width == target_size and height == target_size:
            return data[offset_val : offset_val + entry_size]
        offset += 16
    return b""


def _rgba_pixel(img: Any, xy: tuple[int, int]) -> tuple[int, int, int, int]:
    """读取 RGBA 图片指定位置的像素值（4 元组）.

    辅助函数：Pillow ``getpixel`` 的 stub 返回类型为 ``float | None``，
    但 RGBA 图片实际返回 ``tuple[int, int, int, int]``。用 ``cast`` 收窄类型
    供测试断言使用（Pillow stub 限制，类型系统无法表达）。
    """
    return cast(Tuple[int, int, int, int], img.getpixel(xy))


@_skip_no_pil
def test_convert_image_preserves_p_mode_transparency(tmp_path: Path) -> None:
    """P 模式带 transparency 的 PNG（PNG-8 单透明索引）转换后保留透明.

    模拟 favicon.png 带 8-bit 透明的常见场景：调色板图片通过 transparency 索引
    标记透明色。``convert("RGBA")`` 应将透明索引像素置为 alpha=0。
    """
    import io

    from PIL import Image, ImageDraw

    src = tmp_path / "p_trans.png"
    # 创建 RGBA 图片后量化为 P 模式带 transparency
    img_rgba = Image.new("RGBA", (256, 256), (0, 0, 0, 0))  # 透明背景
    draw = ImageDraw.Draw(img_rgba)
    draw.ellipse((64, 64, 192, 192), fill=(255, 0, 0, 255))  # 不透明红色圆
    img_p = img_rgba.quantize(colors=255, method=Image.Quantize.FASTOCTREE)
    img_p.save(src, format="PNG", transparency=0)

    dst = tmp_path / "out.ico"
    assert _convert_image_to_ico(src, dst) is True

    # 读回 256x256 条目验证：四角透明，中心不透明红色
    png_data = _extract_ico_png_entry(dst.read_bytes(), 256)
    assert png_data, "未找到 256x256 条目"
    rgba = Image.open(io.BytesIO(png_data)).convert("RGBA")
    for corner in ((0, 0), (255, 0), (0, 255), (255, 255)):
        pixel = _rgba_pixel(rgba, corner)
        assert pixel[3] == 0, f"角 {corner} 应透明 alpha=0，实际: {pixel}"
    center = _rgba_pixel(rgba, (128, 128))
    assert center[0] == 255 and center[3] == 255, f"中心应不透明 (255,0,0,255)，实际: {center}"


@_skip_no_pil
def test_convert_image_preserves_gif_transparency(tmp_path: Path) -> None:
    """GIF 带 transparency 转换后保留透明.

    GIF 是 P 模式带单一 transparency 索引的典型格式，``convert("RGBA")`` 应正确展开。
    """
    import io

    from PIL import Image, ImageDraw

    src = tmp_path / "transparent.gif"
    img = Image.new("P", (256, 256), 0)  # 索引 0 = 透明
    # 调色板：索引 0=任意（透明），索引 1=红色
    img.putpalette([0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254)
    draw = ImageDraw.Draw(img)
    draw.ellipse((64, 64, 192, 192), fill=1)  # 红色圆
    img.save(src, format="GIF", transparency=0)

    dst = tmp_path / "out.ico"
    assert _convert_image_to_ico(src, dst) is True

    png_data = _extract_ico_png_entry(dst.read_bytes(), 256)
    assert png_data, "未找到 256x256 条目"
    rgba = Image.open(io.BytesIO(png_data)).convert("RGBA")
    for corner in ((0, 0), (255, 0), (0, 255), (255, 255)):
        pixel = _rgba_pixel(rgba, corner)
        assert pixel[3] == 0, f"角 {corner} 应透明 alpha=0，实际: {pixel}"
    center = _rgba_pixel(rgba, (128, 128))
    assert center[0] == 255 and center[3] == 255, f"中心应红色 (255,0,0,255)，实际: {center}"


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

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "PIL":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

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

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "PIL":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

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

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "PIL":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cli_png = tmp_path / "custom.png"
    cli_png.write_bytes(b"fake png")
    result = _resolve_project_icon(cli_png, None, tmp_path, tmp_path / "work", Platform.WINDOWS)
    assert result == _DEFAULT_ICON
