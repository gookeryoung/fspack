# skill-02 NSIS 安装包生成

## 核心决策

### NSIS 脚本设计
- MUI2 宏实现安装向导（Welcome/Directory/InstFiles/Finish）。
- 安装目录 `$PROGRAMFILES64\<name>`，`RequestExecutionLevel admin`。
- `Section "Main"`：`File /r "dist\*.*"` 递归复制 + `WriteUninstaller`。
- GUI 项目额外创建开始菜单与桌面快捷方式。
- `Section "Uninstall"`：`RMDir /r $INSTDIR` + 清理快捷方式。
- 中英文双语（SimpChinese + English）。

### makensis 调用
- `subprocess.run(["makensis", str(nsi_path)], check=True, capture_output=True)`。
- 缺失 makensis 抛 `InstallerError`（提示安装 NSIS）。
- 编译产出 `<name>-setup.exe` 放 `dist/release/`。

### fsp p 命令
- 默认先 build 再生成安装包；`--no-build` 跳过重建直接用已有 dist。
- dist 不存在或缺 exe 时抛 `FspackError` 提示先 `fsp b`。
