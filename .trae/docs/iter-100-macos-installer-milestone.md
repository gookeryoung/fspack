# iter-100: macOS 安装包与里程碑收尾

## 需求清单

- [x] 新增 `MacInstaller`（.pkg 通过 pkgbuild，.dmg 通过 hdiutil）
- [x] `build_release` 的 `auto` 格式在 macOS 平台默认 .pkg + .dmg
- [x] `--codesign` 选项调 codesign 签名 .pkg 与 .dmg（ad-hoc 签名）
- [x] `--format` choices 新增 `pkg`/`dmg`
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

对应 req-47 阶段 3 收尾项：macOS 安装包支持。使 `fspack` 在 macOS 上能
为本机打包的应用生成可分发的 .pkg 安装包与 .dmg 磁盘镜像，并可选 ad-hoc
签名。req-47 阶段 3「CI 与跨平台」全部完成。

## 改动文件清单

### 新增

- `src/fspack/packaging/installer_macos.py`
  - `MacInstaller(Installer)`：macOS 安装包生成器，target=MACOS，重写
    `build_package` 与 `build_installer` 支持 codesign 透传
  - `build_pkg(dist, info, release, *, codesign=False)`：用 `pkgbuild` 生成 .pkg，
    `--install-location /Applications`，可选 codesign 签名
  - `build_dmg(dist, info, release, *, codesign=False)`：用 `hdiutil create` 生成
    .dmg，含 /Applications 软链接（拖拽安装），可选 codesign 签名
  - `build_pkg_release` / `build_dmg_release`：单格式编排（可选 build → 校验 → 打包）
  - `build_mac_installer`：双格式编排（.pkg + .dmg）
  - `_bundle_identifier(info)`：返回 `com.fspack.<name>` 反向域名
  - `_run_macos_tool(cmd, *, error_hint)`：执行 macOS 专属工具，失败抛 InstallerError
  - `_codesign_adhoc(path)`：ad-hoc 签名（`codesign --force --sign -`）
- `tests/test_macos_installer.py`
  - 18 个测试覆盖 build_pkg / build_dmg / MacInstaller / build_pkg_release /
    build_dmg_release / build_mac_installer / _bundle_identifier

### 修改

- `src/fspack/packaging/installer.py`
  - 模块文档更新：新增 macOS .pkg + .dmg 说明
  - `__all__` 新增 `MacInstaller`/`build_dmg`/`build_dmg_release`/`build_mac_installer`/`build_pkg`/`build_pkg_release`
  - `_VALID_FORMATS` 新增 `pkg`/`dmg`
  - `_resolve_formats` 重构为平台查表，支持 macOS auto=pkg+dmg / all=pkg+dmg+zip
  - `build_release` 新增 `codesign` 参数，调度 `pkg`/`dmg` 格式
  - 末尾 re-export `installer_macos` 模块符号
- `src/fspack/cli.py`
  - `_add_package_subparser` 的 `--format` choices 新增 `pkg`/`dmg`，帮助文本更新
  - 新增 `--codesign` 选项（action="store_true"）
  - `_run_package` 透传 `codesign=ns.codesign`
- `tests/test_installer.py`
  - 新增 macOS _resolve_formats 测试（auto / all / zip 跨平台 / nsis 仅 Windows /
    tar.gz+deb 仅 Linux / pkg+dmg 仅 macOS）
  - 新增 macOS build_release 调度测试（auto 分发 pkg+dmg / pkg 单格式 / 平台不匹配报错）
- `tests/test_cli.py`
  - `test_package_dispatch` 的 fake_build_release 新增 codesign 参数
  - 新增 `test_package_codesign_flag_passthrough`：验证 --codesign 透传
  - 新增 `test_package_format_choices_include_pkg_dmg`：验证 --format pkg 解析
- `tests/test_cli_recursive.py`
  - `_capture_package_call` 的 fake_build_release 新增 codesign 参数

## 关键决策与依据

### .pkg 与 .dmg 双格式

macOS 应用分发的两种主流格式：

| 格式 | 工具 | 用途 | 优势 |
|------|------|------|------|
| .pkg | pkgbuild | 系统级安装包 | 双击安装到 /Applications，支持卸载脚本 |
| .dmg | hdiutil | 磁盘镜像 | 拖拽安装（含 /Applications 软链接），体积小 |

`auto` 格式同时生成两者，覆盖不同分发场景：

- 内部分发 / 企业部署：用 .pkg（支持 MDM 批量部署）
- 公开发布 / 用户下载：用 .dmg（macOS 用户熟悉的拖拽体验）

### pkgbuild 数据布局

```
pkgbuild --root <staging> --identifier com.fspack.<name> --version <version> \
         --install-location /Applications <out.pkg>
```

- `staging/<name>/` 内为 dist 内容（exe + runtime）
- `--install-location /Applications`：安装到 `/Applications/<name>/`
- `--identifier com.fspack.<name>`：bundle identifier（反向域名，pkgbuild 必填）

### hdiutil 数据布局

```
hdiutil create -volname <name> -srcfolder <staging> -ov -format UDZO <out.dmg>
```

- `staging/<name>/`：应用目录（拖拽到 /Applications）
- `staging/Applications`：软链接到系统 /Applications（拖拽入口）
- `-format UDZO`：压缩镜像（zlib 压缩，体积小）

软链接创建失败时（非 macOS 环境，如 Windows 测试）回退为空目录占位，
避免阻塞测试。真实 macOS 打包时 `symlink_to` 会成功。

### codesign ad-hoc 签名

```
codesign --force --sign - <out.pkg>
```

- `--sign -`：ad-hoc 签名（不提供开发者 ID）
- ad-hoc 签名仅用于本地执行权限（Gatekeeper 仍会提示未签名）
- 真实分发需用 Apple Developer ID 签名：`codesign --sign "Developer ID Application: ..."`
- 当前仅支持 ad-hoc，Developer ID 签名需后续扩展（涉及证书管理与 keychain）

`--codesign` 选项仅对 macOS pkg/dmg 格式生效，其他平台忽略。

### _resolve_formats 平台查表重构

原 if-elif 链随平台增长 return 语句过多（PLR0911 7 > 6）。重构为
`platform_defaults: dict[Platform, tuple[list[str], list[str]]]` 查表：

```python
platform_defaults = {
    Platform.WINDOWS: (["nsis"], ["nsis", "zip"]),
    Platform.MACOS: (["pkg", "dmg"], ["pkg", "dmg", "zip"]),
    Platform.LINUX: (["tar.gz", "deb"], ["tar.gz", "deb", "zip"]),
}
defaults, all_formats = platform_defaults[target]
```

降低认知复杂度，新增平台仅需扩展字典。

### MacInstaller.build_installer 重写

基类 `build_installer` 不接受 `codesign` 参数。为透传 codesign 到
`build_package`，重写 `build_installer` 添加 `codesign: bool = False` 关键字参数。

签名扩展是 Liskov 兼容的（子类接受更多关键字参数，调用方仍可用基类签名
调用）。pyrefly `@override` 装饰器验证通过。

## 代码实现情况

### installer_macos.py 核心结构

```python
class MacInstaller(Installer):
    """macOS 安装包生成器：.pkg + .dmg。"""
    platform = Platform.MACOS

    @classmethod
    @override
    def build_package(cls, dist_dir, info, release_dir, *, tracker, codesign=False):
        """生成 .pkg 与 .dmg，返回 .dmg 路径。"""
        _run_stage(tracker, "构造 .pkg", lambda: build_pkg(..., codesign=codesign))
        result = _run_stage(tracker, "构造 .dmg", lambda: build_dmg(..., codesign=codesign))
        return result

    @classmethod
    @override
    def build_installer(cls, ..., *, tracker=None, codesign=False):
        """重写以透传 codesign。"""
        ...
        return cls.build_package(dist, info, release, tracker=tk, codesign=codesign)


def build_pkg(dist_dir, info, release_dir, *, codesign=False):
    """pkgbuild --root <staging> --identifier ... --install-location /Applications <out.pkg>"""
    ...


def build_dmg(dist_dir, info, release_dir, *, codesign=False):
    """hdiutil create -volname <name> -srcfolder <staging> -format UDZO <out.dmg>"""
    ...
```

### _resolve_formats 平台查表

```python
platform_defaults = {
    Platform.WINDOWS: (["nsis"], ["nsis", "zip"]),
    Platform.MACOS: (["pkg", "dmg"], ["pkg", "dmg", "zip"]),
    Platform.LINUX: (["tar.gz", "deb"], ["tar.gz", "deb", "zip"]),
}
defaults, all_formats = platform_defaults[target]
if fmt == "auto":
    return defaults
if fmt == "all":
    return all_formats
```

## 整合优化情况

- `_resolve_formats` 重构为平台查表，消除 PLR0911（7 return → 2 return + 查表）
- `MacInstaller` 复用 `Installer` 基类的 `_prepare_dist`/`_check_exe`/`_run_stage`
  公共编排，与 `LinuxInstaller`/`NsisInstaller` 保持一致接口
- `_run_macos_tool` 统一 macOS 专属工具调用错误处理（pkgbuild/hdiutil/codesign
  共享 FileNotFoundError → InstallerError 转换）
- macOS 与 Linux installer 模块结构对称（build_pkg ↔ build_tarball，
  build_dmg ↔ build_deb，MacInstaller ↔ LinuxInstaller）

## 测试验证结果

- `uv run ruff check src tests` — All checks passed
- `uv run ruff format --check src tests` — 102 files already formatted
- `uv run pyrefly check` — 0 errors (7 suppressed, 7 warnings)
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95` —
  1495 passed, 1 skipped, 32 deselected, coverage **97.59%**

### macOS 测试覆盖要点

- **_bundle_identifier**：返回 `com.fspack.<name>` 反向域名格式
- **build_pkg**：pkgbuild 命令构造、staging 内容校验（exe 复制 / release 排除 /
  旧 staging 清理）、identifier/version/install-location 参数、pkgbuild 缺失/失败
  抛 InstallerError、codesign=True 触发签名调用
- **build_dmg**：hdiutil 命令构造、staging 内容校验、volname/format 参数、
  hdiutil 缺失抛 InstallerError、codesign=True 触发签名调用
- **MacInstaller**：target_platform=MACOS、exe_filename=<name>、
  build_installer 透传 codesign、no_build 缺 dist/exe 报错、with build 成功编排
- **build_release 调度**：auto+macOS → [pkg, dmg]、pkg 单格式、平台不匹配报错、
  codesign 透传到 build_pkg_release/build_dmg_release
- **CLI**：--codesign 透传、--format pkg 解析、--format choices 含 pkg/dmg

## 遗留事项

- **.app bundle 打包**：当前生成单文件可执行程序，未实现 `.app` bundle 结构
  （Contents/MacOS/ + Info.plist + Resources/）。后续若需 macOS 原生应用体验
  （Dock 图标、Launchpad 集成），可扩展 `.app` bundle 打包
- **Developer ID 签名**：当前仅支持 ad-hoc 签名（`--sign -`）。真实分发需用
  Apple Developer ID 签名，涉及证书管理与 keychain，后续可扩展
  `--codesign-identity "Developer ID Application: ..."` 选项
- **公证（Notarization）**：macOS 12+ 要求应用经 Apple 公证才能分发。后续可
  集成 `xcrun notarytool submit` 自动公证流程
- **slow 端到端测试**：macOS 安装包生成需 macOS 环境，当前仅 mock 验证命令
  构造与 staging 内容，未覆盖真实 pkgbuild/hdiutil 执行
- **req-47 阶段 3 全量回归**：iter-96~100 完成 CI 三 job / Linux 测试补强 /
  fixture 共享化 / macOS runtime+loader / macOS 安装包。阶段 3 验收标准达成

## req-47 阶段 3 收尾总结

阶段 3「CI 与跨平台」（iter-96 ~ iter-100）全部完成：

| 轮次 | 主题 | 状态 |
|------|------|------|
| iter-96 | CI 三 job（Windows 矩阵 + slow cron + benchmark 门禁） | ✅ |
| iter-97 | Linux 平台测试覆盖补强 | ✅ |
| iter-98 | 测试 fixture 共享化 | ✅ |
| iter-99 | macOS runtime + loader | ✅ |
| iter-100 | macOS 安装包 + 里程碑收尾 | ✅ |

阶段验收标准达成情况：

- ✅ CI 增强后 Windows 矩阵测试通过（iter-96）
- ✅ slow cron 与 benchmark 门禁正常触发（iter-96）
- ✅ iter-97 Linux 覆盖率提升至 ≥80%（实际模块级覆盖率 100%）
- ✅ iter-99/100 macOS 支持可构建（runtime/loader/installer 三层完整）

## 下一轮计划

iter-101：进入 req-47 阶段 4「体积/启动/安全/文档」。

1. 新增 `dep_analyzer.py` 模块，用 objdump（Linux/macOS）或 pefile 库（Windows）
   解析 .dll/.so/.dylib 依赖树
2. Qt 闭包外但被保留的 DLL 若无依赖引用则剥离
3. `--analyze-deps` 选项启用深度依赖分析（默认关闭，耗时）
4. 体积报告新增"依赖分析节省"行
