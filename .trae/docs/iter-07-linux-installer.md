# P7 Linux 安装包迭代记录

## 迭代目标

实现 Linux 安装包（.deb + tar.gz 便携包），对标 Windows NSIS 安装包，补齐 Linux 平台分发能力。

## 改动文件清单

- `src/fspack/linux_installer.py`（新建）：`build_tarball`（shutil.make_archive 打包 dist 为 tar.gz）、`build_deb`（dpkg-deb 构造 DEBIAN/control + usr/lib/ + usr/bin/ wrapper）、`build_linux_installer`（编排：可选 build → tarball → deb）
- `src/fspack/commands/package.py`：按 target 分发（Windows → build_installer NSIS，Linux → build_linux_installer），平台感知默认版本
- `src/fspack/cli.py`：`fsp p` 加 `--target` 选项，help 改为"生成安装包"
- `tests/test_linux_installer.py`（新建）：9 个单元测试（tarball 产出 + 清理旧 staging、deb control/wrapper/exe 校验 + 清理旧 staging、dpkg-deb 缺失/失败、build_linux_installer no_build 缺 dist/exe/成功/with build 成功）
- `tests/test_commands.py`：3 个 package 测试（默认/显式 Windows/显式 Linux 分发）
- `tests/test_e2e_slow.py`：新增 `test_build_linux_installer_helloworld_slow`（真实 gcc 编译 + dpkg-deb 构造 helloworld，校验 .deb ar 归档 magic 与 tar.gz gzip magic）
- `tests/test_cli.py`：test_package_dispatch 的 fake_run 加 target 参数
- `README.md`：描述/特性/快速上手/命令参考/dist 布局/平台支持矩阵全面更新，反映双平台安装包

## 关键决策与依据

1. **格式选择 .deb + tar.gz**：用户从 .deb + tar.gz / 仅 .deb / AppImage 三选项中选 .deb + tar.gz。tar.gz 提供便携分发（解压即用，无 dpkg 依赖），.deb 提供 apt/dpkg 集成。系统无 rpmbuild/appimagetool，AppImage 需额外下载工具链，跳过。

2. **.deb 布局**：`usr/lib/<name>/`（dist 内容，含 exe + runtime + src）+ `usr/bin/<name>`（shell wrapper 调用 `exec /usr/lib/<name>/<name> "$@"`）。符合 Debian 约定（应用数据在 /usr/lib，可执行入口在 /usr/bin）。

3. **staging 清理**：`build_tarball`/`build_deb` 在打包前检查 staging 是否存在并 rmtree，支持重复打包（避免旧文件残留混入新包）。staging 用完即删，仅保留最终归档。

4. **Python 3.8 shutil.copytree 限制**：不支持 `dirs_exist_ok` 参数，目标目录不能预先存在。`copytree` 内部 `os.makedirs(dst)` 创建整个目录树，不需预 mkdir pkg_dir。

5. **target 分发**：`commands/package.py` 按 `resolved_target` 分发到 NSIS 或 Linux installer，平台感知默认版本（Windows 3.11.9 / Linux 3.11.10 匹配 python-build-standalone release）。

6. **dpkg-deb 缺失处理**：`FileNotFoundError` 包装为 `InstallerError` 提示安装 dpkg-dev，`CalledProcessError` 包装 stderr 信息。

## 验证结果

- 非 slow 门禁：`ruff check` / `ruff format --check` / `pyrefly check` / `pytest --cov` 全过
- 测试统计：136 passed, 9 deselected, coverage 99.89%（linux_installer.py 100% 覆盖）
- slow 测试：8 passed 1 skipped（Windows 4 + 1skip, Linux 2, NSIS 1, Linux 安装包 1）
- Linux 安装包 slow 测试校验：
  - `.deb` 文件 magic = `!<arch>\n`（ar 归档格式）
  - `tar.gz` 文件 magic = `\x1f\x8b`（gzip 格式）
  - 两包均 >1MB，含完整 dist 内容

## 遗留事项

- 仅剩 `cli.py:67->exit`（argparse choices 限制，command 非已知子命令分支不可达），结构性不可达，接受现状
- 未支持 .rpm / AppImage（需 rpmbuild / appimagetool，后续按需扩展）
