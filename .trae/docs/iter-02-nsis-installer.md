# iter-02 NSIS 安装包生成

## 迭代目标

在 P1 dist 产出基础上，生成 NSIS 安装脚本并调用 makensis 编译为单个 `<name>-setup.exe` 安装包，通过 `fsp p`（package）子命令触发。

## 关键决策

### 模块结构
```
src/fspack/
  installer.py         # NSIS 脚本生成 + makensis 编译
  commands/package.py  # fsp p → 编排 build + installer
  cli.py               # 加 package/p 子命令
```

### NSIS 脚本设计
- 用 MUI2 宏实现现代化安装向导（Welcome/Directory/InstFiles/Finish）。
- 安装目录 `$PROGRAMFILES64\<name>`，`RequestExecutionLevel admin`。
- `Section "Main"`：`SetOutPath $INSTDIR` + `File /r "dist\*.*"` 递归复制 dist 全量 + `WriteUninstaller`。
- GUI 项目额外创建开始菜单与桌面快捷方式（`CreateShortCut`）。
- `Section "Uninstall"`：`RMDir /r $INSTDIR` + 清理快捷方式。
- 中英文双语（SimpChinese + English）。

### makensis 调用
- `subprocess.run(["makensis", str(nsi_path)], check=True, capture_output=True)`。
- 缺失 makensis 抛 `InstallerError`（提示安装 NSIS）。
- 编译产出 `<name>-setup.exe` 放 `dist/release/`。
- 真实编译标 `@pytest.mark.slow`。

### fsp p 命令
- `fsp p [project] [--no-build] [--mirror X] [--py-version X]`。
- 默认先 `build` 再生成安装包；`--no-build` 跳过重建直接用已有 dist。
- dist 不存在或缺 exe 时抛 `FspackError` 提示先 `fsp b`。

## 改动文件清单

新增：
- src/fspack/installer.py
- src/fspack/commands/package.py
- tests/test_installer.py

修改：
- src/fspack/cli.py（加 package/p 子命令）
- src/fspack/exceptions.py（加 InstallerError）
- tests/test_commands.py（补 package.run）
- tests/test_cli.py（补 fsp p 分发）

## 验证结果

非 slow 门禁全过（2026-07-12）：

- `uv run ruff check src tests`：All checks passed!
- `uv run ruff format --check src tests`：30 files already formatted
- `uv run pyrefly check`：0 errors（1 suppressed）
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：96 passed, 1 deselected, 覆盖率 99.53%

模块覆盖：
- `installer.py` 100%（generate_nsis_script CLI/GUI 内容断言、compile_installer missing/error/no-output/success、build_installer no_build 三分支 + with_build）
- `commands/package.py` 100%（默认参数 + 显式选项）
- `cli.py` 98%（`fsp p` 分发已覆盖）

## 遗留事项

- makensis 需用户安装（`sudo apt install -y nsis`）；安装前 slow 编译测试与端到端安装包产出验证无法运行。
- 需求 #5 端到端验证待 makensis 就绪后跑 `pytest -m slow`。
- P3：Linux 支持。
