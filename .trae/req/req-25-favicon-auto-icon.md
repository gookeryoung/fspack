# 需求：favicon 自动搜索与图片格式 icon 支持

## 背景

原 icon 流程要求用户手动在 `[tool.fspack] icon` 配置 `.ico` 文件路径，
或通过 CLI `--icon` 指定。Web/桌面项目常自带 `favicon.*`（png/jpg/svg 等），
无法直接作为 exe 图标，需用户手动转换为 `.ico` 后配置。

本次迭代实现：
1. 自动搜索项目子目录下的 `favicon.*` 文件作为 icon
2. 支持手动指定任意常见图片格式（png/jpg/bmp/gif/webp），自动转换为 `.ico`

windres 的 `ICON` 资源类型仅接受 `.ico` 文件，非 `.ico` 格式需通过 Pillow 转换。
Pillow 作为 optional 依赖 `fspack[image]` 提供，未安装时不影响 `.ico` 与默认 icon 流程。

## 需求

- [x] 1. 新增 `packaging/icon.py` 模块：
      - `find_favicon(project_dir)` 递归搜索 `favicon.*`，按格式优先级返回
        （.ico > .png > .bmp > .jpg > .jpeg > .gif > .webp）
      - `ensure_ico(src, work_dir)` 将任意支持格式转换为 `.ico`，
        `.ico` 原样返回，其他格式用 Pillow 转换，失败返回 `None`
      - `SUPPORTED_IMAGE_EXTS` 对外暴露支持的扩展名集合
- [x] 2. `packaging/__init__.py` 导出 `find_favicon`/`ensure_ico`/`SUPPORTED_IMAGE_EXTS`
- [x] 3. `builder.py` 新增 `_resolve_project_icon` 函数，整合优先级链：
      CLI `--icon` > 项目 `[tool.fspack] icon` > 自动搜索 `favicon.*` > 默认 `app.ico`
      非 `.ico` 格式自动转换，转换失败回退默认 icon；Linux 目标统一返回 `None`
- [x] 4. `cli.py` 更新 `--icon` 帮助文本，说明支持多种图片格式与优先级
- [x] 5. `pyproject.toml` 新增 `[project.optional-dependencies] image = ["Pillow>=9.0"]`
- [x] 6. 测试覆盖：favicon 搜索（优先级/排除目录/子目录/目录同名/大小写）、
      `ensure_ico`（.ico 原样/缺失/不支持格式/png 转换/jpg 转换/workdir 创建/
      Pillow 不可用/损坏图片）、`_resolve_project_icon`（Linux/CLI/项目/favicon/
      默认/png 转换/Pillow 缺失回退）
- [x] 7. 全套门禁通过（ruff/format/pyrefly/pytest/coverage ≥ 95%）

## 验收标准

- 项目根/子目录含 `favicon.ico`：自动作为 icon，无需配置
- 项目含 `favicon.png`/`favicon.jpg` 等：安装 `fspack[image]` 后自动转换为 `.ico` 嵌入 exe
- 项目同时含 `favicon.ico` 与 `favicon.png`：优先用 `.ico`（避免转换开销）
- `[tool.fspack] icon` 显式配置优先于 favicon 自动搜索
- CLI `--icon` 指定任意支持格式图片，自动转换并覆盖项目配置
- `dist/`/`build/`/`.venv/` 等目录下的 favicon 被跳过，避免误用构建产物
- 未安装 Pillow 且无 `.ico` 文件：warning 并回退到默认 `app.ico`
- Linux 目标不触发 icon 处理（ELF 无图标资源概念）

## 关键决策

- **Pillow 作为 optional 依赖**：不强制引入核心依赖，用户按需安装 `fspack[image]`
- **favicon 优先级按扩展名分档扫描**：保证同目录 `.ico` 命中先于 `.png`，避免不必要转换
- **windres 仅接受 `.ico`**：`.bmp` 也需通过 Pillow 转换（不能用 `BITMAP` 资源类型作 exe 图标）
- **转换失败回退默认 icon**：保证构建不因 icon 问题中断，warning 提示用户
