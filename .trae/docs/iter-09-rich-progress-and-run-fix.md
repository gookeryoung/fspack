# P9 迭代：rich 彩色进度显示 + fsp r 平台感知修复

## 迭代目标

1. 引入 rich 实现打包过程彩色进度显示，步骤边界清晰，错误/警告/一般消息颜色区分。
2. 修复 `fsp r` 在 Linux 上找 `dist/<name>.exe` 失败的问题（Linux 构建产出 `dist/<name>` 无后缀）。

## 改动文件清单

| 文件 | 变更 |
|------|------|
| `pyproject.toml` | 加 `rich>=13.0` 依赖 |
| `src/fspack/console.py` | 新建：`Console` 单例 + `Theme` + `RichHandler` 配置 + `step/success/warn/error` 辅助函数 |
| `src/fspack/cli.py` | 加 `--verbose` 选项 + `setup_logging()` 调用 |
| `src/fspack/builder.py` | build 流水线四步前调 `step()`，完成调 `success()` |
| `src/fspack/installer.py` | NSIS 两步前调 `step()`，完成调 `success()` |
| `src/fspack/linux_installer.py` | Linux 安装包两步前调 `step()`，完成调 `success()` |
| `src/fspack/commands/run.py` | 新增 `_find_exe()` 平台感知查找 + `_build_cmd()` wine/直跑分发 |
| `tests/test_console.py` | 新建：5 个测试覆盖 `step/success/warn/error/setup_logging` |
| `tests/test_commands.py` | 新增 6 个测试覆盖 `_find_exe` 三分支与 `_build_cmd` Linux 原生/wine/Windows |

## 关键决策与依据

### 1. console.py 设计

- **`Console` 单例 + `Theme`**：全局共享一个 Console 实例，自定义颜色主题（info=cyan, warning=yellow, error=bold red, success=bold green, step=bold blue）。
- **`RichHandler` 替换默认 logging handler**：`show_time=True` 显示时间戳，`show_level=True` 显示级别，`show_path=False` 隐藏模块路径，`rich_tracebacks=True` 异常栈彩色显示，`markup=True` 启用 rich 标记语法。
- **辅助函数而非 logging 级别**：`step()`/`success()` 等是构建流程专用语义标记，不对应 logging 级别，用 `console.print()` 直接输出更灵活。

### 2. fsp r 平台感知查找

- **Linux 优先原生无后缀**：Linux target 构建产出 `dist/<name>`（gcc 编译，无后缀），应优先查找。
- **回退 .exe（wine）**：保留向后兼容，Linux dev 上若只有 `dist/<name>.exe`（Windows target 构建），仍能用 wine 运行。
- **Windows 只找 .exe**：Windows target 必然产出 `.exe`，无原生无后缀可执行文件。
- **`_build_cmd` 分发**：Linux 下 `.exe` 文件用 `shutil.which("wine")`（回退 `"wine"`），原生可执行文件直接 `[str(exe)]`。

### 3. 测试捕获 rich 输出

- **`console.capture()` 而非 `export_text()`**：`export_text()` 需 `Console(record=True)` 构造参数，否则报错。`capture()` 是上下文管理器，捕获后用 `capture.get()` 取文本，更简洁。
- **不验证颜色标记**：测试只断言关键字（▶ ✓ ! ✗）与消息文本，不验证 ANSI 颜色码（脆弱）。

## 验证结果

### 门禁（非 slow）

```
uv run ruff check src tests          All checks passed!
uv run ruff format --check src tests 44 files already formatted
uv run pyrefly check                  0 errors (2 suppressed)
uv run pytest -m "not slow" --cov     148 passed, 9 deselected, cov 99.89%
```

console.py 100% 覆盖；commands/run.py 100% 覆盖（含 `_find_exe`/`_build_cmd` 全分支）。

### 真机验证

**`fsp b tests/examples/helloworld --target linux`** 输出：

```
INFO  项目: helloworld 0.1.0 (cli) 目标: linux
▶ 准备运行时
INFO  python-build-standalone 已缓存: ...
▶ 分析依赖
INFO  无第三方依赖，跳过 wheel 下载
▶ 复制源码
▶ 生成 C loader
INFO  编译 loader: gcc -O2 -o .../helloworld .../loader.c -ldl
✓ 构建完成: .../dist/helloworld
```

步骤标记 ▶ 蓝色高亮，成功标记 ✓ 绿色，INFO 青色带时间戳。

**`fsp r tests/examples/helloworld`** 输出：

```
INFO  运行: .../dist/helloworld
hello, world
```

正确找到 `dist/helloworld`（无后缀）并直接运行，未调 wine。

## 遗留事项

- 无。
