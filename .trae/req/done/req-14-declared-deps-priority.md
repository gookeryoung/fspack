# 需求：声明依赖优先于 AST 导入名

## 背景

打包 `examples/cli_complex` 项目时，`pip download orderedset` 失败：
`No matching distribution found for orderedset`。

根因：`build` 函数（builder.py:146）用 `report.ast_third_party`（AST 扫描的
**导入名**）作为 pip 包名下载。但 Python 生态存在「导入名 ≠ PyPI 包名」的情况：

- `orderedset`（导入名）→ `ordered-set`（PyPI 包名）
- `PIL` → `Pillow`、`yaml` → `PyYAML`、`bs4` → `beautifulsoup4` 等

`pyproject.toml` 的 `dependencies` 字段才是权威的 PyPI 包名，但当前 `build`
完全没用它下载 wheel。

## 需求

- [x] `build` 函数优先用 `report.declared`（pyproject.toml 声明的 PyPI 包名）
      作为 pip 下载源；`declared` 为空时回退到 `report.ast_third_party`
      （导入名 best effort，导入名==包名时成功）。
- [x] 触发下载的条件改为 `report.declared or report.ast_third_party`
      （声明的依赖即使 AST 未发现 import 也应下载，覆盖条件依赖场景）。
- [x] `report.ast_submodules` 仍用于 `unpack_wheels` 的子模块分析（基于导入名，
      与 PyPI 包名无关，保持不变）。
- [x] 修改 `examples/cli_complex/pyproject.toml` 声明 `ordered-set` 和 `lxml`，
      验证打包成功。
- [x] 新增测试覆盖 declared 优先逻辑。
- [x] 全套门禁通过：ruff/pyrefly/pytest/coverage ≥ 95%。

## 验收标准

- `fspack b` 在 `examples/cli_complex` 下成功打包，不再报 `orderedset` 找不到。
- 现有测试全部通过，覆盖率不低于 95%。
