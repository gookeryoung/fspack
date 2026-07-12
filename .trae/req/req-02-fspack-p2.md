# fspack P2 需求清单

P1 已交付 CLI + 解析 + 下载 + loader 垂直切片（非 slow 门禁全过）。P2 在此基础上增加 NSIS 安装包生成，使用户能产出可分发的 Windows 安装程序。

## P2 范围

[x] 1. NSIS 脚本生成器：根据 ProjectInfo 与 dist 目录内容生成 `.nsi` 脚本，含 Name/OutFile/InstallDir/Section(File /r)/UninstallSection/CreateShortCut（GUI 时）。
[x] 2. makensis 编译调用：调用 `makensis` 将 .nsi 编译为单个 `<name>-setup.exe` 安装包，输出到 `dist/release/`（编译命令已实现并单测 mock，真实编译待 makensis）。
[x] 3. `fsp p`(package) 子命令：在 `fsp b` 产出 dist 基础上生成安装包；支持 `--no-build` 跳过重建直接打包已有 dist。
[x] 4. 单测：NSIS 脚本内容断言（Name/OutFile/File/Shortcut/Uninstall）、makensis 调用 mock、`fsp p` 命令分发。
[] 5. 端到端验证（slow）：真实 makensis 编译安装包，断言 .exe 产出（待 makensis 安装）。

## 不在 P2 范围

- 数字签名（signtool）。
- 自动更新（在线升级机制）。
- 多语言安装界面（仅中文 + 英文默认）。
- Linux 平台支持（P3）。

## 验收标准

- `fsp p` 对已构建项目产出 `dist/release/<name>-setup.exe`。
- NSIS 脚本含完整安装/卸载逻辑，GUI 项目额外创建开始菜单与桌面快捷方式。
- 全套门禁通过：`ruff check`、`ruff format --check`、`pyrefly check`、`pytest --cov≥95%`（makensis 相关标 slow）。
- Python 3.8 兼容。

## 约束

- 不引入新依赖，NSIS 脚本用字符串模板生成。
- makensis 调用用 subprocess，标 slow。
- 遵循 rule-11 Python 规范。
