"""图标资源处理：favicon 自动搜索与图片格式转换.

windres 的 ``ICON`` 资源类型仅接受 ``.ico`` 文件，本模块负责：

1. :func:`find_favicon` —— 递归扫描项目目录查找 ``favicon.*`` 文件，
   按格式优先级（.ico > .png > .bmp > .jpg > .jpeg > .gif > .webp）返回首个命中。
2. :func:`ensure_ico` —— 将任意支持的图片格式转换为 ``.ico``。
   ``.ico`` 原样返回；其余格式（``.png``/``.bmp``/``.jpg``/``.jpeg``/``.gif``/``.webp``）
   通过 Pillow 转换为 ``.ico``。Pillow 不可用时返回 ``None``，调用方回退到默认 icon。

Pillow 作为 optional 依赖 ``fspack[image]`` 提供，未安装时不影响 ``.ico`` 与默认 icon 流程。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

__all__ = ["SUPPORTED_IMAGE_EXTS", "ensure_ico", "find_favicon"]

_logger = logging.getLogger(__name__)

# favicon 搜索匹配的扩展名集合（小写，含点）
# 顺序即优先级：.ico 优先于 .png 优先于 .bmp 优先于其他
_FAVICON_EXTS: tuple[str, ...] = (
    ".ico",
    ".png",
    ".bmp",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
)

# 对外暴露：支持的图片扩展名集合（用于文档/校验）
SUPPORTED_IMAGE_EXTS: frozenset[str] = frozenset(_FAVICON_EXTS)

# favicon 搜索时排除的目录名（避免扫描构建产物/虚拟环境/IDE 配置等）
_FAVICON_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "dist",
        "build",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".git",
        ".idea",
        ".vscode",
        "node_modules",
        ".fspack",
        "htmlcov",
        ".pytest_cache",
        ".ruff_cache",
        ".pyrefly_cache",
        ".mypy_cache",
        ".uv-cache",
        ".tox",
        ".trae",
    }
)


def find_favicon(project_dir: Path) -> Path | None:
    """递归搜索项目目录下的 ``favicon.*`` 文件，返回首个命中路径。

    扫描规则（按优先级）：

    1. **浅层目录优先**：``os.walk`` 自顶向下遍历，项目根目录的 favicon 优先于
       子目录（用户通常将主 favicon 放在浅层位置）
    2. **同目录内按扩展名优先级**：``.ico`` > ``.png`` > ``.bmp`` > ``.jpg`` >
       ``.jpeg`` > ``.gif`` > ``.webp``（避免不必要的图片转换）
    3. **跳过排除目录**：不进入 :data:`_FAVICON_SKIP_DIRS` 中的目录子树
       （dist/build/.venv/.tox 等构建产物与缓存）

    文件名匹配大小写不敏感（``favicon.ICO`` 等同 ``favicon.ico``）。
    返回 ``None`` 表示未找到任何 favicon 文件。
    """
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        return None

    # os.walk 自顶向下遍历：浅层目录优先于子目录
    for root, dirs, files in os.walk(project_dir):
        # 原地修改 dirs 跳过排除目录，避免进入其子树（os.walk 标准用法）
        dirs[:] = [d for d in dirs if d not in _FAVICON_SKIP_DIRS]
        # 同目录内按扩展名优先级查找：构建小写文件名→原始名映射，O(exts+files)
        lower_map = {fname.lower(): fname for fname in files}
        for ext in _FAVICON_EXTS:
            target = f"favicon{ext}"
            fname = lower_map.get(target)
            if fname is not None:
                path = Path(root) / fname
                _logger.info("发现 favicon: %s", path)
                return path
    return None


def ensure_ico(src: Path, work_dir: Path) -> Path | None:
    """确保 icon 资源为 windres 可处理的 ``.ico`` 格式，返回路径。

    - ``.ico``：原样返回（无需转换）
    - 其他支持的图片格式（``.png``/``.bmp``/``.jpg``/``.jpeg``/``.gif``/``.webp``）：
      调用 Pillow 转换为 ``icon.ico`` 写入 ``work_dir``，返回新路径
    - Pillow 不可用或转换失败：warning 并返回 ``None``，调用方回退到默认 icon

    ``src`` 不存在时返回 ``None``。``work_dir`` 不存在时自动创建。
    """
    if not src.is_file():
        _logger.warning("icon 文件不存在: %s", src)
        return None

    suffix = src.suffix.lower()
    if suffix == ".ico":
        return src

    if suffix not in SUPPORTED_IMAGE_EXTS:
        _logger.warning("不支持的 icon 格式 %s，跳过: %s", suffix, src)
        return None

    work_dir.mkdir(parents=True, exist_ok=True)
    dst = work_dir / "icon.ico"
    if _convert_image_to_ico(src, dst):
        return dst
    return None


def _convert_image_to_ico(src: Path, dst: Path) -> bool:
    """用 Pillow 将图片转换为 ``.ico``，成功返回 True。

    Pillow 不可用或转换抛异常时记录 warning 并返回 False。
    转换参数：``RGBA`` 模式保留透明通道，尺寸自动适配 ico 多档（16/32/48/64/128/256）。
    """
    try:
        from PIL import Image
    except ImportError:
        _logger.warning(
            "图片转 .ico 需要 Pillow，未安装已跳过（安装 fspack[image] 或 Pillow 后重试）: %s",
            src,
        )
        return False

    img = None
    try:
        img = Image.open(src)
        # RGBA 保留透明通道；非 RGBA 转 RGBA（如 JPEG 无 alpha）
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        # ico 多尺寸：windres 选最匹配档位嵌入 exe
        sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        img.save(dst, format="ICO", sizes=sizes)
        _logger.info("图片转换 .ico 成功: %s -> %s", src, dst)
        return True
    except (OSError, ValueError) as e:
        _logger.warning("图片转换 .ico 失败，跳过: %s\n%s", src, e)
        return False
    finally:
        # Image.open 持有文件句柄，显式关闭避免 Windows 文件占用
        if img is not None:
            img.close()
