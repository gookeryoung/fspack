# loader 编译缓存与多入口配置分类

## 缓存键

- `LoaderCompiler.compile`（`src/fspack/packaging/loader/compile.py`）持久缓存目录默认
  `~/.fspack/cache/loaders/`（可用 `FSPACK_CACHE_DIR` 覆盖）。
- 缓存键：`sha256(source + app_type.value + platform.value + icon_hash + version_info_hash)`
  前 16 字符 hex（`cache_keys._loader_cache_key`）；缓存文件名 `<key><exe_suffix>`。
- `icon_hash`：icon 文件内容 sha256 前 16 字符；icon 路径不存在时按无图标处理（hash 空串）。
- `version_info_hash`：`LoaderVersionInfo` 五字段（name/version/description/author/exe_filename）
  组合 sha256 前 16 字符；`exe_filename` 纳入使多入口项目各入口 exe 资源段（OriginalFilename）
  独立缓存。
- 命中：复制缓存 exe 到 `out_exe`，不创建编译工作目录；未命中：编译后 best-effort 回写。
- 资源段编译失败（windres 不可用/rc 语法错误）但有 icon/version_info 时不回写缓存，
  避免资源段缺失的 exe 被永久命中。

## 多入口配置分类去重（`pipeline/compile_stage._build_entry_loaders`）

- 单次构建内 loader 源码/平台/icon 全局一致，`(app_type, version_info)` 与持久缓存键
  一一等价；按该键对入口分组，只编译组内首个入口（代表），其余入口用 `shutil.copy2`
  从代表 exe 复制（产物逐字节相同）。
- 分组键可哈希依据：`LoaderVersionInfo` 为 frozen dataclass。
- 平台边界：
  - Windows：`version_info.exe_filename` 按入口名区分 → 每组仅一入口，不做构建内去重
    （各入口 exe 的 OriginalFilename 保持与自身文件名一致，跨构建由持久缓存兜底）。
  - Linux/macOS：`version_info=None` → 同 `app_type` 多入口共享一次编译，消除并行同键
    全部缓存 miss 导致的重复编译。
- 入口文件不受去重影响：每个入口的 `_entry_<name>.py` 包装器与 `<name>.entry`（单入口
  回退 `.entry`）必须全部写出（`_write_entry_files`）。
- 并行策略：配置类数 > 1 时用 `ThreadPoolExecutor` 并行编译各配置类代表入口，
  `max_workers = min(cpu_count, _MAX_LOADER_WORKERS=4)`；配置类数 == 1 走串行路径不建池。
- 异常语义：任一代表入口 `compile_loader` 抛异常时等待其余 future 完成后重抛首个异常，
  不执行同组复制。

## 接口

- `pipeline/compile_stage._write_entry_files(ctx, ep, has_tkinter) -> tuple[AppType, LoaderVersionInfo | None]`：
  写 wrapper/.entry，返回编译配置。模块私有，仅 `_build_entry_loaders` 调用。
- `pipeline/compile_stage._compile_entry_loader(ctx, ep, source, work_dir, resolved_icon, app_type, version_info, stage) -> None`：
  编译单个代表入口 loader。模块私有。
- 原 `_build_one_loader` 已被上述两函数取代，不得再引用。
