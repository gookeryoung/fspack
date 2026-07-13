# skill-05 端到端示例验证

## 核心决策与修复

### 默认镜像切换：华为云 → 阿里云
- 华为云 PyPI 索引改版为 HTML 门户，不再返回 PEP 503 simple 索引，pip download 报 `Could not find a version`。
- 阿里云 `https://mirrors.aliyun.com/pypi/simple/` 正常。华为云 embed python 下载仍可用。

### _pth 文件位置：dist/ → runtime/
- CPython embed 模式要求 `python3X._pth` 与 `python3X.dll` 同目录。
- 原设计放在 `dist/`（与 loader.exe 同目录），但 dll 在 `dist/runtime/`，Python 找不到 _pth。
- 修复：`write_pth` 改写到 `dist/runtime/`，路径相对 runtime（`..\src` 引用 dist 下的 src）。

### PySide6 DLL 搜索路径
- Windows 加载 .pyd 时不搜索 .pyd 所在目录的依赖 DLL（仅搜索 exe 目录、系统目录、PATH）。
- 示例入口需 `os.add_dll_directory(PySide6 目录)` 注册。

### pygame SysFont 在 Windows 崩溃
- `pygame.font.SysFont` 调用 `initsysfonts_win32()` 枚举系统字体，注册表返回 int 类型字体名导致 `splitext(font)` 抛 `TypeError`。
- 修复：改用 `pygame.font.Font(None, size)` 使用内置默认字体（freesansbold.ttf）。

### wine 缺 icuuc.dll
- PySide6 的 Qt6Core.dll 依赖 `icuuc.dll`（Windows 10+ 系统 DLL），wine 9.0 不提供。真实 Windows 可运行。

## 示例矩阵
helloworld（无库 CLI）、clitool（requests）、guicalc（PySide6）、pygamedemo（pygame）、webapp（flask）。
