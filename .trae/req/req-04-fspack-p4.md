# fspack P4 需求清单

P1/P2/P3 已交付三阶段完整链路。P4 聚焦代码质量改进与覆盖率提升，消除冗余字段并补齐未覆盖分支。

## P4 范围

[x] 1. 移除 `BuildConfig.arch` 冗余字段：实际平台标签由 `wheel_platform_tag(target)` 派生，arch 字段未被使用且默认值 "win_amd64" 对 Linux 不正确。同步清理 test_config.py 对应断言。
[x] 2. 补 `detect_platform` Windows 分支测试：monkeypatch `platform.system` 返回 "Windows"，覆盖 platform.py:21。
[x] 3. 补 `detect_entry` 重复候选测试：构造项目名与 .py 同名且该文件无 entry、另一文件有 entry 的场景，触发 `mod in seen` continue 分支（project.py:83）。
[x] 4. 补 `_parse_target` windows 分支测试：`fsp b --target windows` 分发，覆盖 cli.py:82 的 `return Platform.WINDOWS`。
[x] 5. 全套门禁通过：ruff check / ruff format --check / pyrefly check / pytest --cov≥95%（覆盖率不得低于 99.08%）。

## 不在 P4 范围

- macOS 支持、Linux 安装包（P5+ 候选）。
- slow 端到端验证（依赖外部环境安装）。

## 验收标准

- `BuildConfig` 无 arch 字段，所有引用清理干净。
- 四项门禁全过，覆盖率不低于 99.08%。
- 未覆盖分支数减少。

## 约束

- 遵循 rule-11 Python 规范。
- 测试用 monkeypatch，禁用 @patch 装饰器。
