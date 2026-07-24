# 示例全量 slow 测试覆盖

## 需求清单

- [x] 为所有 examples 示例设计 slow 测试，验证真实构建与运行
- [x] 修复 cli_complex 示例 bug：module_d.py 用属性访问 core.module_g 但未显式导入
- [x] 修复 pygame_conway 示例：pyproject.toml 依赖声明缺失（numpy/pygame/attrs），主循环无 dummy 退出机制
- [x] 给 pygame_gktetris 示例加 dummy 退出机制
- [x] 补充 4 个 slow 测试：cli_complex/cli_office/pygame_conway/pygame_gktetris
- [x] 修改 rule-11 验证命令章节，明确「每 5 次开发循环至少跑一次 slow 全量测试」
- [x] 修复 pyrefly.toml 配置漂移：project-excludes 补 examples/** 与 ref/**，search-path 补 src

## 验收标准

- 所有 examples 示例均有对应的 slow 测试
- 示例代码 bug 已修复，`fsp r --debug` 可正常运行
- rule-11 明确 slow 全量测试频率
- 全套门禁通过（ruff format / pyrefly / pytest 覆盖率 ≥ 95%）
