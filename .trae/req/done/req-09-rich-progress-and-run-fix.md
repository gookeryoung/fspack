# P9 rich 彩色进度显示 + fsp r 平台感知修复

## 背景

用户提两个需求：

1. **打包过程进度可视化**：当前 build 流程仅靠 logging INFO 输出，步骤边界不清晰，错误/警告与一般消息无颜色区分。需要引入 rich 实现彩色显示，按级别着色（ERROR 红/WARNING 黄/INFO 一般色），步骤进度明显。
2. **`fsp r` 运行失败**：Linux target 构建产出 `dist/<name>`（无后缀，原生可执行文件），但 `fsp r` 硬编码找 `dist/<name>.exe`，导致 Linux 构建后无法 `fsp r` 运行。

## 需求清单

- [x] 引入 rich 依赖（`rich>=13.0`）
- [x] 新建 `src/fspack/console.py`：`Console` 单例 + `Theme`（info/warning/error/success/step 颜色）+ `RichHandler` 配置 + `step()`/`success()`/`warn()`/`error()` 辅助函数
- [x] `cli.py` 加 `--verbose` 选项 + `setup_logging()` 调用
- [x] `builder.build` 在"准备运行时/分析依赖/复制源码/生成 C loader"四步前调 `step()`，完成时 `success()`
- [x] `installer.build_installer` 在"生成 NSIS 脚本/编译 NSIS 安装包"两步前调 `step()`，完成时 `success()`
- [x] `linux_installer.build_linux_installer` 在"生成 tar.gz 便携包/构造 .deb 安装包"两步前调 `step()`，完成时 `success()`
- [x] 新建 `tests/test_console.py` 覆盖 `step/success/warn/error/setup_logging`
- [x] `commands/run.py` 新增 `_find_exe()`：Linux 优先找原生 `dist/<name>`，回退 `dist/<name>.exe`；Windows 找 `dist/<name>.exe`
- [x] `commands/run.py` 新增 `_build_cmd()`：Linux 下 `.exe` 用 wine，原生可执行文件直跑
- [x] `tests/test_commands.py` 新增 6 个测试覆盖 `_find_exe` 三分支与 `_build_cmd` Linux 原生/wine/Windows
- [x] 真机验证 `fsp b` 彩色输出与 `fsp r` Linux 原生运行
- [x] 跑全套门禁确认无回归

## 验收标准

- `fsp b` 输出有清晰步骤标记（▶）与成功标记（✓），颜色按级别区分
- `fsp r` 在 Linux 上能找到 `dist/<name>`（无后缀）并直接运行
- `fsp r` 在 Linux 上对 `dist/<name>.exe` 仍能用 wine 运行（向后兼容）
- 非 slow 门禁全过（ruff/pyrefly/pytest cov≥95%）
