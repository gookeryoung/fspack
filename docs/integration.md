# CI/CD 集成指南

fspack 可集成到其他 Python 项目的 CI/CD 工作流，实现自动打包与打包成功验证。
本文介绍三种集成模式、测试反馈机制，并提供可复用的 GitHub Actions workflow 模板。

## fspack 自身的发布流程

fspack 自身通过 [.github/workflows/release.yml](https://github.com/gookeryoung/fspack/blob/main/.github/workflows/release.yml)
在 `git push v*.*.*` tag 时，用**各平台原生 runner 打包自身**发布到 GitHub Release：

| job | runner | 工具链 | 产物 |
|-----|--------|--------|------|
| `pypi` | ubuntu-latest | uv | sdist + wheel（PyPI 发布） |
| `pack-windows` | windows-latest | mingw-w64 + NSIS | NSIS 安装包 `.exe` + 跨平台便携包 `.zip` |
| `pack-linux` | ubuntu-latest | gcc + dpkg-dev | tar.gz 便携包 + `.deb` 安装包 + 跨平台便携包 `.zip` |
| `release` | ubuntu-latest | gh | 收集以上所有产物统一上传到 GitHub Release |

**原生平台打包**而非 ubuntu 交叉编译的原因：

- pyc 预编译仅在 `target is detect_platform()` 时执行（避免跨版本字节码不兼容）
- loader 用本机 gcc/mingw 原生编译，无需 wine 兼容层
- Windows Server 2025 runner 已移除 NSIS 预装，workflow 中显式 `choco install nsis mingw`

任一前置 job 失败时 `release` job 仍尝试发布已成功产物（`if: always() && !cancelled()`），
但最终通过 `Report failure` 步骤标记 workflow 失败，便于追踪问题。

**工具链安装要点**：

- Windows runner：`choco install nsis mingw -y` 安装 NSIS 与带 `x86_64-w64-mingw32-gcc` 前缀的 mingw-w64，
  并将路径追加到 `$GITHUB_PATH`
- Linux runner：`apt-get install -y gcc dpkg-dev`，gcc 与 dpkg-deb 均为标准包

## 集成架构

```text
checkout 项目代码 → 安装 fspack + 编译器 → fsp b 打包 → 测试打包结果 → 上传 artifact/release
```

**前置条件**：

- 项目根目录有 `pyproject.toml`（fspack 可识别的入口）
- fspack 已发布到 PyPI，直接 `pip install fspack` 或 `uv pip install fspack --system` 安装
- CI runner 需安装编译器：Linux 目标需 `gcc`，Windows 目标需 `mingw-w64`（Linux 交叉编译）

## 三种集成模式

| 模式 | 触发时机 | 目的 | 模板文件 |
|------|---------|------|---------|
| A. 验证打包 | push/PR | 确保改动不破坏打包 | `templates/pack-check.yml` |
| B. 发布产物 | tag push | 生成可分发安装包附到 Release | `templates/release-pack.yml` |
| C. 矩阵打包 | release 分支 | 同时产出 Windows + Linux 安装包 | `templates/release-pack.yml` |

## 测试打包成功的反馈机制

采用三层验证，任一失败即 CI 失败并反馈：

### 层 1：构建阶段断言

`fsp b` 失败时退出码非零，CI 自然失败。成功后断言关键产物存在：

```bash
fspack b . --target windows
test -f dist/<name>.exe                    # loader exe 生成
test -f dist/runtime/python311.dll          # embed python 解压
test -f dist/runtime/python311._pth         # _pth 写入
test -d dist/runtime/Lib/site-packages      # site-packages 就绪
```

多入口项目断言每个入口 exe：

```bash
for ep in cli gui web; do test -f dist/${ep}.exe || exit 1; done
```

### 层 2：运行阶段断言

**Linux 目标**（原生运行）：

```bash
output=$(./dist/<name> 2>&1)
echo "$output" | grep -q "预期输出字符串" || { echo "运行断言失败"; exit 1; }
```

**Windows 目标**（Linux runner 用 wine 运行）：

```bash
export WINEDEBUG=-all                       # 屏蔽 wine 噪音
export WINEPREFIX="$RUNNER_TEMP/.wine"      # 隔离 prefix
wineboot --init                              # 初始化（首次运行）
output=$(wine ./dist/<name>.exe 2>&1 | tr -d '\r')   # 清理 CRLF
echo "$output" | grep -q "预期输出字符串" || { echo "运行断言失败: $output"; exit 1; }
```

**GUI 应用**（无头环境运行）：

```bash
export QT_QPA_PLATFORM=offscreen            # Qt 无头模式
export SDL_VIDEODRIVER=dummy                # pygame 无头模式
export SDL_AUDIODRIVER=dummy
# 用 --debug 绕过 GUI loader，使 print 输出可见
fspack r . --entry gui --debug 2>&1 | grep -q "预期输出"
```

### 层 3：失败反馈

失败时上传构建产物供调试：

```yaml
- name: 上传失败现场
  if: failure()
  uses: actions/upload-artifact@v4
  with:
    name: build-failure-${{ matrix.target }}
    path: dist/
    retention-days: 3
```

### 反馈机制总结

| 阶段 | 成功反馈 | 失败反馈 |
|------|---------|---------|
| 构建 | `fsp b` 退出码 0 + 产物断言通过 | 退出码非零，CI job 失败，上传 dist/ 供调试 |
| 运行 | grep 命中预期字符串 | grep 失败，输出实际内容到日志，上传失败现场 |
| 安装包 | 文件存在 + 魔数校验（MZ/`!<arch>`/gzip） | 文件缺失或魔数错误 |
| Release | `gh release upload` 成功，artifact 附到 release | release job 失败，已产出 artifact 仍可下载 |

## 快速上手

### 1. 复制 workflow 模板

```bash
# 复制 PR 验模板
cp templates/pack-check.yml your-project/.github/workflows/

# 复制 Release 发布模板
cp templates/release-pack.yml your-project/.github/workflows/
```

### 2. 配置 GitHub Variables

在仓库 **Settings → Secrets and variables → Actions → Variables** 配置：

| 变量名 | 必填 | 说明 | 示例值 |
|--------|------|------|--------|
| `PROJECT_NAME` | 是 | 项目名（与 `pyproject.toml` 的 `name` 字段一致） | `my_app` |
| `EXPECTED_OUTPUT` | 是 | 运行打包后 exe 应输出的预期字符串 | `hello from my_app` |
| `ENTRY_NAMES` | 否 | 多入口项目的入口名列表（逗号分隔，与 `[tool.fspack.entries]` 键匹配），未设置时按单入口处理 | `cli,gui,web` |

### 3. 触发验证

```bash
# PR 验证（push 到 main 或 PR 自动触发）
git push origin main

# Release 发布（打 tag 触发）
git tag v0.1.0
git push origin v0.1.0
```

## 缓存策略

`~/.fspack/cache/` 含三类缓存（embed python ~10MB、wheel 缓存、loader 缓存），缓存命中后构建从分钟级降到秒级：

```yaml
- name: Cache fspack assets
  uses: actions/cache@v4
  with:
    path: ~/.fspack/cache/
    # key 含 target 隔离 Windows embed 与 Linux standalone（不通用）
    # hashFiles 锁文件变化时刷新，restore-keys 兜底复用旧缓存补差下载
    key: ${{ runner.os }}-fspack-${{ matrix.target }}-${{ hashFiles('pyproject.toml', 'uv.lock') }}
    restore-keys: |
      ${{ runner.os }}-fspack-${{ matrix.target }}-
      ${{ runner.os }}-fspack-
```

## 多入口项目的 CI 验证

多入口项目（`[tool.fspack.entries]`）在 CI 中应循环验证每个入口：

```bash
# 构建一次生成所有入口 exe
fspack b . --target windows

# 循环运行验证每个入口
for ep in cli gui web; do
  if [ "$ep" = "gui" ]; then
    export QT_QPA_PLATFORM=offscreen
    fspack r . --entry $ep --debug 2>&1 | grep -q "hello from multi_entry $ep" || exit 1
  else
    wine ./dist/${ep}.exe 2>&1 | tr -d '\r' | grep -q "hello from multi_entry $ep" || exit 1
  fi
done
```

## 关键注意事项

1. **wine CRLF**：wine 输出含 `\r\n`，必须 `tr -d '\r'` 清理，否则 grep 失败
2. **WINEDEBUG=-all**：屏蔽 wine 噪音日志，否则输出被淹没
3. **WINEPREFIX 隔离**：用 `${{ runner.temp }}/.wine` 避免污染 runner 全局
4. **wineboot --init**：首次运行需初始化 prefix，否则 wine 报错
5. **GUI 应用 wine 缺系统 DLL**：PySide6/PyQt5 的 Qt DLL 可能依赖 `icuuc.dll` 等 Windows 10+ 系统 DLL，wine 默认不提供。CI 中检测到 `DLL load failed` 时应 skip 运行断言（仅验证构建成功），参考 fspack 自身 slow 测试的 skip 逻辑
6. **缓存 key 含 target**：Windows embed python 与 Linux python-build-standalone 不通用，缓存键必须含 target 隔离
7. **fspack 版本固定**：建议其他项目 `uv pip install fspack==0.1.0` 固定版本，避免 fspack 升级破坏 CI

## 完整示例

`examples/multi_entry` 是多入口项目示例，以下 workflow 片段展示如何在 CI 中验证其三个入口：

```yaml
- name: Build multi-entry project
  run: fspack b examples/multi_entry --target windows

- name: Verify all entries
  env:
    WINEDEBUG: -all
    WINEPREFIX: ${{ runner.temp }}/.wine
    QT_QPA_PLATFORM: offscreen
  run: |
    wineboot --init
    cd examples/multi_entry
    # CLI 与 Web 入口用 wine 运行
    for ep in cli web; do
      output=$(wine ./dist/${ep}.exe 2>&1 | tr -d '\r')
      echo "$output" | grep -q "hello from multi_entry $ep" || {
        echo "::error::入口 $ep 运行失败: $output"; exit 1
      }
    done
    # GUI 入口用 --debug 绕过 GUI loader
    fspack r . --entry gui --debug 2>&1 | grep -q "hello from multi_entry gui" || {
      echo "::error::GUI 入口运行失败"; exit 1
    }
```

## 模板文件

- [`templates/pack-check.yml`](https://github.com/gooker_young/fspack/blob/main/templates/pack-check.yml) — PR 验证打包模板
- [`templates/release-pack.yml`](https://github.com/gooker_young/fspack/blob/main/templates/release-pack.yml) — Release 发布安装包模板

## 参考资源

- [GitHub Actions 文档](https://docs.github.com/actions)
- [actions/upload-artifact](https://github.com/actions/upload-artifact)
- [actions/cache](https://github.com/actions/cache)
- [setup-uv](https://github.com/astral-sh/setup-uv)
- [fspack 自身 CI 配置](https://github.com/gooker_young/fspack/blob/main/.github/workflows/ci.yml)
