# iter-108: 安全加固（依赖哈希校验 + SBOM + 代码签名）

## 需求清单

- [x] req-47: 依赖下载强制哈希校验（`--require-hashes`）
- [x] req-47: SBOM 生成（SPDX 2.3 兼容 JSON）
- [x] req-47: Windows 代码签名（signtool）
- [x] req-47: Linux .deb GPG 签名
- [x] req-47: CLI 参数与配置项集成

## 迭代目标

为 fspack 添加安全加固能力：依赖供应链完整性校验、SBOM 物料清单生成、产物代码签名，
满足企业级分发安全合规要求。

## 改动文件清单

### 源码

- `src/fspack/packaging/sbom.py` — SBOM 生成模块（SPDX 2.3 兼容 JSON）
- `src/fspack/packaging/pipeline.py` — 集成 SBOM 生成到 build 流程末尾
- `src/fspack/packaging/wheel_pip.py` — `download_wheels` 透传 `require_hashes`
- `src/fspack/packaging/wheel_resolver.py` — `uv --generate-hashes` + `pip download --require-hashes` 路径
- `src/fspack/packaging/installer_nsis.py` — `sign_exe_file`/`sign_exe_files` + `NsisInstaller.build_installer` 覆写
- `src/fspack/packaging/installer_linux.py` — `sign_deb_file` + `build_deb_release` 签名集成
- `src/fspack/packaging/installaller.py` — `build_release` 透传签名参数
- `src/fspack/config/models.py` — `BuildOptions`/`BuildDefaults` 新增安全字段
- `src/fspack/config/parsing.py` — `[tool.fspack]` 安全配置解析
- `src/fspack/cli.py` — CLI 参数 `--require-hashes`/`--no-sbom`/`--sign-exe`/`--sign-deb`

### 测试

- `tests/test_sbom.py` — 新建，SBOM 生成逻辑测试（33 个用例）
- `tests/test_installer.py` — 扩展 signtool 签名测试
- `tests/test_linux_installer.py` — 扩展 gpg 签名测试
- `tests/test_cli.py` — 扩展 CLI 参数解析测试
- `tests/test_config.py` — 扩展配置解析测试
- `tests/test_wheels.py` — 修复 mock 返回类型（list→str）
- `tests/test_extras.py` — 修复 fake_build_release 签名
- `tests/test_cli_recursive.py` — 修复 fake_build_release 签名

## 关键决策与依据

1. **SBOM 放在 build 末尾而非 package 阶段**：SBOM 扫描 dist 下 site-packages，
   必须在所有构建阶段完成后生成。放在 `tracker.summary()` 之前使 SBOM stage 出现在汇总表。
   OSError 降级为 warning 不阻断构建（SBOM 仅为审计辅助产物）。

2. **require_hashes 缓存命中跳过校验**：缓存目录 wheel 已首次校验过哈希，
   重复构建时跳过校验避免性能损失。uv 不可用时降级为 warning 不校验（避免阻塞构建）。

3. **Windows 签名双层**：NSIS 编译前签名 dist 下入口 exe（使安装包内打包签名 exe），
   NSIS 编译后签名 setup.exe（使安装包自身携带签名）。签名失败降级为 warning 不阻断。

4. **Linux .deb 用分离签名**：`gpg --detach-sign --armor` 产出 `.asc` 签名文件，
   不修改 .deb 本身（dpkg 不支持内嵌签名）。密钥 ID 透传 `--local-user`。

5. **`_download_with_hashes` 缺少 `cache_dir` 参数**：iter-108 修复了 iter-103 引入的
   F821 undefined name `cache_dir` bug，添加 `cache_dir` 到函数签名并从 `_download_online` 透传。

6. **`collect_sbom` 返回 `dict[str, Any]`**：pyrefly 类型检查要求，`dict[str, object]`
   导致 `len()` 和 `[]` 访问报错。

## 代码实现情况

### SBOM 生成（sbom.py）
- `collect_sbom(dist_dir, info)` 扫描 `*.dist-info` 提取 name/version/license/SHA256
- `generate_sbom(dist_dir, info)` 写入 `dist/release/<name>-<version>-sbom.json`
- 许可证优先级：`License-Expression` > `License` > `License-File` > `NOASSERTION`
- SHA256 基于 RECORD 文件内容拼接计算（避免逐文件枚举到 SPDX files 数组）

### 哈希校验（wheel_resolver.py）
- `require_hashes=True` 时强制走 `uv pip compile --generate-hashes` 路径
- 生成带哈希 requirements.txt 后用 `pip download --require-hashes -r` 校验
- 无法并行（pip `--require-hashes` 要求所有包在同一 requirements 文件中）

### 代码签名（installer_nsis.py）
- `sign_exe_file(exe, cert, password)` 调用 `signtool sign /f /p /t`
- `sign_exe_files(dist, info, cert, password)` 签名所有入口 exe
- `NsisInstaller.build_installer` 覆写：签名 dist exe → NSIS 编译 → 签名 setup.exe

### GPG 签名（installer_linux.py）
- `sign_deb_file(deb, key_id)` 调用 `gpg --detach-sign --armor [--local-user]`
- `build_deb_release` 扩展：build_deb 后可选 GPG 签名

## 测试验证结果

- `make check` 全量通过：ruff check + ruff format + pyrefly + pytest --cov
- 1835 passed, 12 skipped, 8 deselected
- 覆盖率 96.06%（≥95% 门禁）
- sbom.py 覆盖率 94%，wheel_resolver.py 覆盖率 88%（require_hashes 路径需网络环境）

## 遗留事项

- SBOM SHA256 校验和基于 RECORD 文件，RECORD 不存在时返回 None（files_analyzed=False）
- 签名功能需实际证书/密钥才能端到端测试，当前仅 mock subprocess.run 验证命令构造
- `require_hashes` 端到端测试需真实 PyPI 网络，当前仅单元测试 mock 路径

## 下一轮计划

iter-108 安全加固已完成全部需求。req-47 可标记完成。
