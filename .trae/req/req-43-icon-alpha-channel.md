# 需求：icon 转换透明通道支持

## 背景

`packaging/icon.py` 的 `_convert_image_to_ico` 将 favicon/用户指定图片转换为
`.ico` 供 windres 嵌入 exe。原实现存在两个透明通道隐患：

1. **Pillow 版本兼容性**：项目要求 `Pillow>=9.0`，但 Pillow 9.0-9.3 保存 ICO 时
   小尺寸条目（16/32/48）默认用 BMP 格式，alpha 退化为 1-bit AND mask，
   **丢失半透明信息**。Pillow 9.4+ 才支持 `bitmap_format="png"` 参数强制所有
   尺寸用 PNG 格式保存。
2. **透明通道处理不显式**：代码注释仅提及"RGBA 保留透明通道"，未覆盖
   `P`+`transparency`（GIF / PNG-8 单透明索引）、`P`+`trns`（多 alpha 值）、
   `La`/`PA` 等 L+alpha 模式的处理逻辑，缺乏测试覆盖。

## 需求

- [x] 1. `_convert_image_to_ico` 使用 `bitmap_format="png"` 参数保存 ICO，
      强制所有尺寸条目（16/32/48/64/128/256）用 PNG 格式，保留完整 8-bit alpha
- [x] 2. 升级 Pillow 依赖到 `>=9.4.0`（`bitmap_format` 参数最低版本）
- [x] 3. 更新 `_convert_image_to_ico` docstring 与模块顶部 docstring，
      明确透明通道保留策略（RGBA / P+transparency / P+trns / La / PA / RGB 等模式）
- [x] 4. 新增测试覆盖透明通道场景：
      - `test_convert_image_preserves_alpha_channel`：RGBA 半透明图片转换后
        ICO 所有条目为 PNG 格式，四角 alpha=0、中心 alpha=128
      - `test_convert_image_preserves_p_mode_transparency`：P+transparency PNG
        转换后四角透明、中心不透明
      - `test_convert_image_preserves_gif_transparency`：GIF+transparency
        转换后四角透明、中心不透明
- [x] 5. 全套门禁通过（ruff/format/pyrefly/pytest/coverage ≥ 95%）

## 验收标准

- RGBA 带半透明像素的图片转换后 ICO 保留完整 8-bit alpha（含 alpha=128 等中间值）
- P 模式带 transparency 的 PNG（PNG-8 单透明索引）转换后透明区域 alpha=0
- GIF 带 transparency 转换后透明区域 alpha=0
- ICO 文件所有尺寸条目（含 16x16/32x32/48x48 小尺寸）均为 PNG 格式，
  避免默认 BMP 格式的 1-bit AND mask 退化
- Pillow < 9.4 环境下 `bitmap_format` 参数被忽略不报错（向后兼容）

## 关键决策

- **`bitmap_format="png"` 强制 PNG 格式**：默认 BMP 格式的小尺寸条目仅用
  1-bit AND mask 表示透明，会丢失半透明信息；PNG 格式保留完整 8-bit alpha，
  确保 exe 图标在 Windows 资源管理器 / 任务栏 / Alt+Tab 中正确显示透明背景
- **Pillow 依赖升级到 >=9.4.0**：`bitmap_format` 参数在 9.4.0 引入，
  升级确保参数生效；9.4.0 发布于 2023-01-02，已稳定 3 年
- **测试用 `struct` 解析 ICO + `io.BytesIO` 读回 PNG 条目**：避免直接修改
  `Image.size` 只读属性（pyrefly 报错），用二进制解析提取条目数据后用
  `Image.open` 读取，类型安全且准确验证 alpha 通道
