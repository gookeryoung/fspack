# iter-72：icon 转换透明通道支持

## 需求清单

- [x] req-43：icon 转换透明通道支持

## 迭代目标

修复 `_convert_image_to_ico` 透明通道丢失隐患：Pillow 9.0-9.3 保存 ICO 时
小尺寸条目默认用 BMP 格式，alpha 退化为 1-bit AND mask 丢失半透明信息。
升级 Pillow 依赖到 >=9.4.0 并使用 `bitmap_format="png"` 强制所有尺寸条目
用 PNG 格式保存，保留完整 8-bit alpha 通道。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/fspack/packaging/icon.py` | `_convert_image_to_ico` 新增 `bitmap_format="png"` 参数；更新模块顶部 docstring 与函数 docstring 明确透明通道保留策略 |
| `pyproject.toml` | `Pillow>=9.0` → `Pillow>=9.4.0`（`bitmap_format` 参数最低版本） |
| `tests/test_icon.py` | 新增 3 个透明通道测试 + 2 个辅助函数（`_extract_ico_png_entry` / `_rgba_pixel`） |

## 关键决策与依据

### 1. `bitmap_format="png"` 强制 PNG 格式保存 ICO 条目

**问题**：Pillow 保存 ICO 时，小尺寸条目（16/32/48）默认用 BMP 格式，
alpha 通道通过 1-bit AND mask 表示，**只能编码完全透明或完全不透明**，
丢失半透明像素（如 alpha=128）。

**方案**：`img.save(dst, format="ICO", sizes=sizes, bitmap_format="png")`
强制所有尺寸条目用 PNG 格式保存，保留完整 8-bit alpha。

**依据**：`bitmap_format` 参数在 Pillow 9.4.0 引入（2023-01-02），
项目原要求 `Pillow>=9.0`，升级到 `>=9.4.0` 确保参数生效。

### 2. 透明通道处理策略（覆盖各 Pillow 模式）

| 模式 | 处理 | alpha 结果 |
|------|------|-----------|
| `RGBA` | 原样 | 保留 8-bit alpha（含半透明） |
| `P`+`transparency`（GIF / PNG-8 单透明索引） | `convert("RGBA")` | 透明索引 alpha=0，其余 alpha=255 |
| `P`+`trns`（PNG-8 多 alpha 值） | `convert("RGBA")` | Pillow 自动展开为 RGBA |
| `La` / `PA`（L+alpha / 调色板+alpha） | `convert("RGBA")` | 保留 alpha |
| `RGB` / `L` / `1` / `CMYK`（无 alpha） | `convert("RGBA")` | 填充 alpha=255 |

### 3. 测试用 `struct` 解析 ICO + `io.BytesIO` 读回 PNG 条目

**问题**：直接 `ico.size = (256, 256)` 修改只读属性（pyrefly 报 `read-only`），
`getpixel` stub 返回 `float | None`（pyrefly 报 `bad-index`）。

**方案**：用 `struct` 解析 ICO 二进制结构提取 256x256 条目的 PNG 字节流，
用 `Image.open(io.BytesIO(png_data))` 读取；`_rgba_pixel` 辅助函数用 `cast`
收窄 `getpixel` 返回类型为 `tuple[int, int, int, int]`（Pillow stub 限制，
类型系统无法表达）。

## 代码实现情况

### `src/fspack/packaging/icon.py`

```python
img = Image.open(src)
# 统一转 RGBA：保留透明通道（P+transparency / La / PA / RGBA 原样，
# RGB / L / 1 / CMYK 等无 alpha 模式填充 alpha=255）
if img.mode != "RGBA":
    img = img.convert("RGBA")
sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
# bitmap_format="png" 强制所有尺寸用 PNG 格式保存，保留完整 8-bit alpha；
# 默认 BMP 格式的小尺寸条目仅用 1-bit AND mask，会丢失半透明信息
img.save(dst, format="ICO", sizes=sizes, bitmap_format="png")
```

### `tests/test_icon.py` 新增测试

- `test_convert_image_preserves_alpha_channel`：RGBA 半透明（alpha=128）图片
  转换后 ICO 6 个条目均为 PNG 格式，四角 alpha=0、中心 (255,0,0,128)
- `test_convert_image_preserves_p_mode_transparency`：P+transparency PNG
  转换后四角透明、中心 (255,0,0,255)
- `test_convert_image_preserves_gif_transparency`：GIF+transparency
  转换后四角透明、中心 (255,0,0,255)

## 整合优化情况

- 模块顶部 docstring 新增"透明通道支持"段落，说明各模式处理策略
- `_convert_image_to_ico` docstring 列出所有模式的 alpha 处理结果
- 测试新增 `_extract_ico_png_entry` / `_rgba_pixel` 两个辅助函数，
  避免重复 ICO 解析与类型收窄代码

## 测试验证结果

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：69 files already formatted
- `uv run pyrefly check`：0 errors
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：
  1083 passed, 1 skipped, 30 deselected，覆盖率 98.56%
- `tests/test_icon.py`：35 passed（含 3 个新增透明通道测试）

## 遗留事项

无。

## 下一轮计划

无（修复完成，等待用户下一需求）。
