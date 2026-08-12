# 需求：sdist 排除开发内部目录

## 背景

`pyproject.toml` 未配置 `[tool.hatch.build.targets.sdist]`，hatchling 默认包含
所有 git 跟踪的文件。导致 sdist（`dist/fspack-*.tar.gz`）包含 245 个文件，
其中混入大量开发内部目录与文件：

- `.benchmarks/` — 性能测试基线数据
- `.github/` — CI 工作流配置
- `.trae/` — 开发流程文档（rules/docs/req/skills）
- `.vscode/` — IDE 配置
- `templates/` — GitHub Actions 打包模板
- `Makefile` — 开发脚本
- `.copier-answers.yml` — 项目模板生成记录
- `.python-version` — pyenv 版本锁
- `.bumpversion.toml` — 版本管理工具配置
- `.pre-commit-config.yaml` — pre-commit 钩子配置
- `.readthedocs.yaml` — ReadTheDocs 托管配置

这些文件对最终用户安装/使用 fspack 无价值，且暴露内部开发流程结构。

## 需求

- [x] 1. 配置 `[tool.hatch.build.targets.sdist].exclude`，排除 11 项开发内部
      目录与文件
- [x] 2. 保留源码分发必需内容：`src/` / `tests/` / `docs/` / `examples/` +
      配置（`ruff.toml` / `pyrefly.toml` / `pytest.ini` / `.coveragerc` /
      `tox.ini`）+ `README.md` / `LICENSE` / `pyproject.toml` / `uv.lock`
- [x] 3. 全套门禁通过（ruff/format/pyrefly/pytest/coverage ≥ 95%）

## 验收标准

- `uv build --sdist` 生成的 tar.gz 不包含上述 11 项开发内部目录与文件
- sdist 文件数从 245 降至 ~163（仅保留源码分发必需内容）
- 现有 1083 个测试全部通过，覆盖率 98.56% 无回归

## 关键决策

- **保留 `tox.ini` / `.coveragerc` / `ruff.toml` / `pyrefly.toml` / `pytest.ini`**：
  这些是工具链配置，让 sdist 用户可以本地运行测试、lint、覆盖率检查
- **保留 `docs/` / `examples/`**：文档源码与示例对用户理解 fspack 有价值
- **保留 `.gitignore` / `.gitattributes`**：对 VCS 操作有用
- **排除 `.bumpversion.toml` / `.pre-commit-config.yaml` / `.readthedocs.yaml`**：
  这些是开发工具配置，sdist 用户不需要
