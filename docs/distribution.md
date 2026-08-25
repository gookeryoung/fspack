# 分发指南

涵盖安全分发（资源段嵌入、代码签名、误报申诉）与 Windows 7 兼容支持。

## 安全与分发

### 资源段嵌入（自动）

fspack 打包 Windows exe 时自动嵌入 PE 资源段，降低 Windows Defender 等杀软启发式误报：

- **VS_VERSIONINFO**：从 `pyproject.toml` 的 `[project].description` 与 `[project].authors[0].name` 提取，填充 CompanyName / FileDescription / ProductName / OriginalFilename 等字段
- **application manifest**：声明 asInvoker（loader 不提权）、PerMonitorV2 DPI 感知、Win7-11 supportedOS
- **图标**：按 CLI `--icon` > 配置 `icon` > favicon 自动搜索 > 默认图标 优先级解析

在 `pyproject.toml` 声明 `description` 与 `authors` 即可丰富资源段（未声明时回退到项目名，不留空值字段）：

```toml
[project]
name = "my-app"
version = "1.0.0"
description = "我的桌面应用"
authors = [{ name = "张三" }]
```

### 代码签名（推荐）

生产分发建议用 Authenticode 证书签名 exe 与安装包，进一步降低误报。fspack 打包后用 Windows SDK `signtool` 签名：

```bash
fsp b                                              # 产出 dist/my-app.exe
signtool sign /fd SHA256 /f cert.pfx /p <密码> dist/my-app.exe
signtool sign /fd SHA256 /f cert.pfx /p <密码> dist/release/my-app-setup.exe
```

无证书时可生成自签名证书用于内部分发，或向 DigiCert / Sectigo 等机构购买代码签名证书。

### 误报申诉

mingw 编译的小型 exe 即使嵌入资源段仍可能被部分杀软误报。确认安全但被误报时：

1. **Microsoft Defender**：访问 [微软安全智能提交页](https://www.microsoft.com/wdsi/filesubmission)，选择"我认为此文件是安全的"
2. **VirusTotal**：上传至 [virustotal.com](https://www.virustotal.com) 查看各引擎检测结果，对命中引擎单独申诉
3. **代码签名**：签名后的 exe 误报率显著降低，多数杀软对已签名文件放宽启发式阈值
4. **重复检测**：杀软定义更新后可能自动解除误报，签名 + 等待更新通常即可解决

## Windows 7 支持

Windows 产物可在 **Win7 SP1** 上运行（Python 3.9–3.14），fspack 自动处理两代兼容问题：

| Python 版本 | 兼容手段 |
|------------|---------|
| 3.9–3.11 | 构建时注入内置 `api-ms-win-core-path-l1-1-0.dll` shim（官方 dll 仅缺此 API Set） |
| 3.12–3.14 | 按 sha256 清单从 [PythonVista](https://github.com/adang1345/PythonVista) 下载 Win7 重编译版 `python3XX.dll` 替换官方件（官方 dll 静态导入 Win8+ API，shim 无法覆盖） |

构建期自动执行三道门禁（无需配置）：loader exe 导入表校验（违规即构建失败）、python3XX.dll 双重校验（sha256 + 导入表）、dist 全量 `.dll`/`.pyd` 扫描并输出报告 `dist/release/win7-compat-report.txt`（第三方依赖违规仅报告不阻断，可据此更换依赖版本；`--no-win7-scan` 可关闭）。

**运行前提**：目标机需已安装 UCRT（Win7 装 [KB2999226](https://www.microsoft.com/en-us/download/details.aspx?id=49077)，Win10/11 自带）。NSIS 安装包会在启动时检测 `ucrtbase.dll`，缺失时提示 KB 编号并询问是否继续；zip 便携包请自行确认该前提。

**已知限制**：

- 第三方 pyd 若自身链接了 Win8+ API，在 Win7 上加载会失败——兼容报告会列出此类文件，但 fspack 无法自动修复（只能更换依赖版本）；Nuitka 编译模式（`--nuitka`）的用户代码产物同样受扫描报告监督
- Nuitka 编译模式与 Win7 重编译版 runtime（py≥3.12）互斥：官方工具链编译的 .pyd 在重编译版 python3XX.dll 进程内加载即崩溃，显式 `--nuitka` 时自动置 `--no-win7-dll`（产物仅 Win8+），可用 `--no-win7-dll` 主动跳过 Win7 DLL 注入
