# iter-31 favicon 自动搜索与图片格式 icon 支持

## 需求清单

- [x] 新增 `packaging/icon.py`：favicon 自动搜索 + 图片转 .ico
- [x] `packaging/__init__.py` 导出 icon API
- [x] `builder.py` 集成 favicon 搜索与 ensure_ico 转换
- [x] `cli.py` 更新 `--icon` 帮助文本
- [x] `pyproject.toml` 新增 `image` optional 依赖
- [x] `tests/test_icon.py` 测试覆盖
- [x] 全套门禁通过

## 迭代目标

原 icon 流程要求用户手动配置 `.ico` 文件路径，无法利用项目自带的 `favicon.*`
图片，且仅支持 `.ico` 格式。本次迭代实现 favicon 自动搜索与多格式图片支持，
降低用户配置成本。

## 改动文件清单

- `src/fspack/packaging/icon.py`（新增）：`find_favicon` 递归搜索 `favicon.*`，
  按扩展名优先级返回；`ensure_ico` 将任意支持格式转换为 `.ico`（Pillow 可选）；
  `SUPPORTED_IMAGE_EXTS` 对外暴露支持的扩展名集合
- `src/fspack/packaging/__init__.py`：导出 `find_favicon`/`ensure_ico`/`SUPPORTED_IMAGE_EXTS`
- `src/fspack/builder.py`：新增 `_resolve_project_icon` 函数，整合优先级链
  （CLI > 项目配置 > favicon 自动搜索 > 默认 icon），非 .ico 格式自动转换
- `src/fspack/cli.py`：更新 `--icon` 帮助文本
- `pyproject.toml`：新增 `image = ["Pillow>=9.0"]` optional 依赖
- `tests/test_icon.py`（新增）：30 个测试覆盖 favicon 搜索、ensure_ico 转换、
  _resolve_project_icon 优先级链
- `.trae/req/req-25-favicon-auto-icon.md`（新增）：需求记录

## 关键决策与依据

- **Pillow 作为 optional 依赖**：不强制引入核心依赖，用户按需安装 `fspack[image]`。
  未安装时不影响 `.ico` 与默认 icon 流程，仅非 `.ico` 格式转换时 warning 回退。
- **favicon 优先级按扩展名分档扫描**：`for ext in _FAVICON_EXTS: for path in rglob(...)`，
  保证同目录 `.ico` 命中先于 `.png`，避免不必要的图片转换。
- **windres 仅接受 `.ico`**：`ICON` 资源类型只接受 `.ico` 文件，`.bmp` 也不能直接用
  （`BITMAP` 资源类型不能作 exe 图标）。所有非 `.ico` 格式统一通过 Pillow 转换。
- **转换失败回退默认 icon**：保证构建不因 icon 问题中断，warning 提示用户。
- **Linux 目标统一返回 None**：ELF 无图标资源概念，跳过整个 icon 处理流程。

## 代码实现情况

### find_favicon

- 按 `_FAVICON_EXTS` 优先级逐档 `rglob` 扫描
- `_FAVICON_SKIP_DIRS` 排除 dist/build/.venv 等构建产物目录
- `is_file()` 过滤同名目录（rglob 会匹配目录）
- `path.suffix.lower() != ext` 二次校验（大小写不敏感系统保护）

### ensure_ico

- `.ico` 原样返回（无需转换，work_dir 不创建）
- 其他支持格式：`work_dir/icon.ico` 输出，Pillow RGBA 模式 + 多尺寸（16~256）
- Pillow 不可用 / 转换失败：返回 `None`，调用方回退默认
- `finally` 块显式 `img.close()` 避免 Windows 文件句柄占用

### _resolve_project_icon（builder.py）

- 优先级：CLI > 项目配置 > favicon 自动搜索 > 默认 `app.ico`
- 候选 icon 经 `ensure_ico` 转换为 `.ico` 后传给 loader
- 转换失败回退 `_DEFAULT_ICON`（fspack 自带 `assets/icons/app.ico`）

## 整合优化情况

- `_resolve_project_icon` 封装 icon 解析逻辑，builder.build 主流程仅一行调用，
  保持主流程清晰
- icon 转换工作目录复用 `dist/build`，与 loader 编译工作目录一致，避免新增临时目录
- 向后兼容：无 favicon 且无显式配置时回退到原有默认 icon 行为

## 测试验证结果

- `tests/test_icon.py`：30 个测试，覆盖：
  - find_favicon（12 个）：空目录/找到 ico/png/优先级/排除目录/子目录/非 favicon 文件/
    不支持扩展名/目录同名/大小写
  - ensure_ico（7 个）：.ico 原样/缺失/不支持格式/png 转换/jpg 转换/workdir 创建
  - _convert_image_to_ico（3 个）：成功/损坏图片/Pillow 不可用
  - _resolve_project_icon（8 个）：Linux/CLI 优先/项目优先/favicon 自动/默认/png 转换/
    Pillow 缺失回退/CLI 非 ico Pillow 缺失回退
- 门禁全过：ruff check / ruff format --check / pyrefly check 0 errors /
  pytest 631 passed / coverage 97.80%
- `packaging/icon.py` 覆盖率 96%

## 遗留事项

- 无

## 下一轮计划

- 无（需求已完整实现）
