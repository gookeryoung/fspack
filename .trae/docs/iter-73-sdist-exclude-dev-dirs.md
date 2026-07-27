# iter-73：sdist 排除开发内部目录

## 需求清单

- [x] req-44：sdist 排除开发内部目录

## 迭代目标

配置 hatchling sdist exclude，排除 `.benchmarks` / `.github` / `.trae` 等
11 项开发内部目录与文件，精简源码分发体积（245 → 163 文件）。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `pyproject.toml` | 新增 `[tool.hatch.build.targets.sdist].exclude`，排除 11 项开发内部目录与文件 |

## 关键决策与依据

### 排除范围

| 排除项 | 理由 |
|--------|------|
| `.benchmarks` | 性能测试基线数据，开发内部 |
| `.github` | CI 工作流配置，开发内部 |
| `.trae` | 开发流程文档（rules/docs/req/skills），开发内部 |
| `.vscode` | IDE 配置，开发内部 |
| `templates` | GitHub Actions 打包模板，开发内部 |
| `Makefile` | 开发脚本，sdist 用户用不到 |
| `.copier-answers.yml` | 项目模板生成记录，开发内部 |
| `.python-version` | pyenv 版本锁，开发内部 |
| `.bumpversion.toml` | 版本管理工具配置，开发内部 |
| `.pre-commit-config.yaml` | pre-commit 钩子配置，开发内部 |
| `.readthedocs.yaml` | ReadTheDocs 托管配置，开发内部 |

### 保留内容

- `src/` / `tests/` / `docs/` / `examples/` — 源码 / 测试 / 文档 / 示例
- `ruff.toml` / `pyrefly.toml` / `pytest.ini` / `.coveragerc` / `tox.ini` — 工具链配置（让 sdist 用户可运行测试/lint/覆盖率）
- `.gitignore` / `.gitattributes` — VCS 配置
- `README.md` / `LICENSE` / `pyproject.toml` / `uv.lock` — 项目元数据与锁文件

## 代码实现情况

### `pyproject.toml`

```toml
[tool.hatch.build.targets.sdist]
exclude = [
    ".benchmarks",
    ".github",
    ".trae",
    ".vscode",
    "templates",
    "Makefile",
    ".copier-answers.yml",
    ".python-version",
    ".bumpversion.toml",
    ".pre-commit-config.yaml",
    ".readthedocs.yaml",
]
```

## 测试验证结果

- `uv build --sdist`：成功构建，文件数 245 → 163
- sdist 内容验证：11 项开发内部目录/文件已排除
- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：69 files already formatted
- `uv run pyrefly check`：0 errors
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：
  1083 passed, 1 skipped, 30 deselected，覆盖率 98.56%

## 整合优化情况

无（仅构建配置调整，不涉及代码逻辑）。

## 遗留事项

无。

## 下一轮计划

无（修复完成，等待用户下一需求）。
