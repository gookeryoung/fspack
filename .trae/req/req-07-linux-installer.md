# P7 Linux 安装包需求

## 背景

P6 代码质量/文档改进已交付。项目进入 P7：Linux 安装包，对标 Windows NSIS 安装包，补齐 Linux 平台的打包分发能力。

## 格式选择

- **tar.gz 便携包**：shutil.make_archive 打包 dist，解压即用，无外部依赖
- **.deb 安装包**：dpkg-deb 构造 DEBIAN/control + 数据目录 + wrapper 脚本，apt/dpkg 安装

系统已有 dpkg-deb（Ubuntu 自带），无 rpmbuild/appimagetool。

## 需求清单

- [x] 新建 `src/fspack/linux_installer.py` 实现 tar.gz 便携包（shutil.make_archive）
- [x] `linux_installer.py` 实现 .deb 安装包（dpkg-deb 构造 DEBIAN/control + usr/lib/<name>/ + usr/bin/<name> wrapper）
- [x] `build_linux_installer` 编排函数（可选 build → tarball → deb），返回 .deb 路径
- [x] `commands/package.py` 按 target 分发（Windows → build_installer NSIS，Linux → build_linux_installer）
- [x] `cli.py` `fsp p` 加 `--target` 选项，help 改为"生成安装包"
- [x] 单元测试 `tests/test_linux_installer.py`（mock subprocess.run，覆盖 control/wrapper 生成与缺失工具场景）
- [x] slow 端到端测试（真实 dpkg-deb 编译 helloworld，校验 .deb 与 tar.gz 产出）
- [x] 更新 README 平台支持矩阵（Linux 安装包列 .deb + tar.gz）
- [x] 跑全套门禁确认无回归

## 验收标准

- `fsp p --target linux` 产出 dist/release/<name>_<version>_amd64.deb 与 <name>-<version>-linux.tar.gz
- .deb 含正确的 DEBIAN/control 与 usr/bin/<name> wrapper，dpkg-deb 构造成功
- tar.gz 解压后保留 dist 完整布局（exe + runtime + src）
- 非 slow 门禁全过（ruff/pyrefly/pytest cov≥95%）
- slow 测试通过真实 dpkg-deb 编译验证
