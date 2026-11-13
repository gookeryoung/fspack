# 分发指南

涵盖安全分发（资源段嵌入、代码签名、误报申诉）与 Windows 7 兼容支持。

## 平台、架构与 Python 版本

一次 `fsp b` 只产出一个目标：平台由 `--target` 指定，架构由构建机决定，Python
版本由 `--py-version` 指定。产物统一写入 `dist/`，不同目标会互相覆盖，跨目标
批量构建需自行换目录或分次执行。

| 平台 | 架构 | 可用 Python | 运行时 | CI 真实构建 |
|------|------|-------------|--------|-------------|
| Windows | x86_64 | 3.8–3.14（含 3.13t/3.14t） | embed（自由线程走 standalone） | 有（release.yml） |
| Linux | x86_64 | 3.10–3.14（含 3.13t/3.14t） | standalone | 有（release.yml） |
| macOS | x86_64 / arm64 | 3.10–3.14（含 3.13t/3.14t） | standalone | 双格：macos-latest（arm64）+ macos-15-intel（x86_64） |

版本下限不是配置项而是上游资产约束：python-build-standalone 自 3.8/3.9 EOL 后
不再发布对应 tarball，因此这两个版本只能在 Windows embed 路径使用。显式传
`--py-version 3.8` 到 Linux/macOS 会因资产不存在而下载失败；`.python-version`
写 3.8 时会被忽略并回退到 `requires-python` 解析出的版本，注意区分。

Windows/Linux 目前只有 x86_64；macOS 两架构按构建机 `platform.machine()` 选择，
无 `--arch` 开关。Windows 目标可从任意构建机交叉产出；Linux/macOS 目标要求
同平台构建机。

## 产物命名与迁移

0.6.14 起发行包基名插入架构段，manifest/SBOM 名补 py 与平台，目的是让
「同名产物内容不同」可被文件名区分。**旧名不保留兼容**，`dist/release/`
只产出新名。

| 产物 | 0.6.13 及之前 | 0.6.14 起 |
|------|---------------|-----------|
| Windows setup | `app-1.0-py3.14.6-windows-slim-setup.exe` | `app-1.0-py3.14.6-windows-x86_64-slim-setup.exe` |
| Windows zip | `app-1.0-py3.14.6-windows-slim.zip` | `app-1.0-py3.14.6-windows-x86_64-slim.zip` |
| Linux tar.gz | `app-1.0-py3.14.6-linux-slim.tar.gz` | `app-1.0-py3.14.6-linux-x86_64-slim.tar.gz` |
| Linux deb | `app_1.0-py3.14.6-slim_amd64.deb` | 文件名不变；`Architecture` 改为按目标架构推导 |
| macOS pkg / dmg | `app-1.0-py3.14.6-macos-slim.pkg` | `app-1.0-py3.14.6-macos-arm64-slim.pkg`（Intel 为 `x86_64`） |
| manifest | `app-1.0-manifest.json` | `app-1.0-py3.14.6-windows-manifest.json` |
| SBOM | `app-1.0-sbom.json` | `app-1.0-py3.14.6-windows-sbom.json` |

迁移建议：

- 下游脚本不要硬编码完整文件名，改用通配：`*-windows-*-slim-setup.exe`、
  `*-linux-*-slim.tar.gz`、`*-macos-*-slim.dmg`、`*-manifest.json`。
- Release 附件名会随之变化，若有按附件名下载的流程需同步更新。
- 若确实需要旧名，只能在调用方打包后自行重命名；fspack 不提供兼容开关。

Release 说明可参考：

> 本版本重命名了发行产物，以携带架构信息：`<name>-<ver>-py<ver>-<platform>-slim`
> 变为 `<name>-<ver>-py<ver>-<platform>-<arch>-slim`，manifest/SBOM 文件名新增
> py 与平台字段。未提供旧名兼容开关；若你的 CI 或脚本按固定文件名取产物，
> 请改为通配匹配（如 `*-windows-*-slim-setup.exe`）。功能与产物内容未变。

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

生产分发建议用证书签名 exe 与安装包，进一步降低误报。`fsp p` 内置三平台签名开关，
无需手工后处理：

| 平台 | CLI 选项 | 行为 |
|------|---------|------|
| Windows | `--sign-exe` + `--sign-exe-certificate <pfx>` + `--sign-exe-password <pwd>` | 调用 `signtool sign /f <pfx> /p <pwd> /t <时间戳>`，签名 dist 内 exe 与 release 下的 NSIS 安装包；需 Windows SDK 的 `signtool` 在 PATH 中。证书路径/密码也可用 `[tool.fspack]` 的 `sign-exe-certificate`/`sign-exe-password` 配置默认值（CLI 优先） |
| Linux | `--sign-deb` + `--sign-deb-key <keyid>` | `gpg --detach-sign --armor` 对 `.deb` 做分离签名，产出 `<deb>.asc`；未指定 key 时用 GPG 默认密钥，也可用 `[tool.fspack] sign-deb-key` 配置默认值 |
| macOS | `--codesign` | 对 pkg/dmg 做 ad-hoc 签名（`codesign --force --sign -`），仅适用于本地执行；真实分发仍需 Apple Developer ID 签名 |

```bash
fsp p --sign-exe --sign-exe-certificate cert.pfx --sign-exe-password <密码>
fsp p --sign-deb --sign-deb-key 0x12345678
fsp p --format pkg --codesign
```

也可在打包后用外部工具手工签名：

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
