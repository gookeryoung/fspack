# 需求 13：示例目录整合

## 背景

项目原有两处示例目录：
- `examples/`：展示型示例（cli-complex 等连字符命名）
- `tests/examples/`：测试用示例（helloworld/clitool/guicalc 等）

两处目录存在重叠（snake/pygame_snake、helloworld 等），维护成本高，命名不统一。

## 需求

- [x] 将 `tests/examples/` 下 8 个测试示例移到 `examples/`，删除 `tests/examples/`
- [x] 按 `<类型>_<名称>` 下划线风格统一重命名测试示例目录
- [x] pyproject.toml 的 `name` 字段与目录名一致（下划线），保证 exe 名 = 目录名
- [x] 删除展示版 pygame_snake（与测试版 snake 重叠，仅保留测试版并改名 pygame_snake）
- [x] 更新 3 个测试文件（test_project.py/test_builder.py/test_e2e_slow.py）的示例路径与断言
- [x] 更新 pyproject.toml pyrefly 排除项（删除 tests/examples/**）
- [x] 更新 README.md 示例章节
- [x] 验证：ruff/pyrefly/pytest --cov ≥ 95% 全部通过 + 手动构建示例

## 验收标准

1. `tests/examples/` 目录不存在，所有示例在 `examples/` 下
2. 测试示例目录名为下划线风格（cli_helloworld/pyside2_app 等）
3. `name` 字段 = 目录名，构建产出 exe 名 = 目录名
4. 全套门禁通过，覆盖率不降
