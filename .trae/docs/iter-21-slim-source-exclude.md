# 迭代 21 - 精简 dist/src 复制内容

## 需求清单

参见 `req-20-slim-source-exclude.md`。

## 迭代目标

扩展 `builder._EXCLUDE` 模式列表，将 `copy_source` 复制到 `dist/src` 时剥离所有开发期文件（元数据、工具配置、凭证、文档、测试代码、缓存），仅保留应用运行所需源码与资源。同时符合 rule-11 安全要求（`.env` 不泄漏到 dist）。

## 改动文件清单

- `src/fspack/builder.py`：扩展 `_EXCLUDE` 模式列表（13 类共 ~45 项），按类别分组并加注释说明原因；更新 `copy_source` docstring 描述精简范围
- `tests/test_builder.py`：新增 `test_copy_source_strips_dev_artifacts`（25+ 项开发文件剥离）与 `test_copy_source_keeps_runtime_resources`（LICENSE/.py/资源/子包保留，含子包内开发文件剥离）

## 关键决策与依据

### 1. 显式排除策略（向后兼容）

采用「显式排除已知开发文件」而非「白名单保留」策略。原因：

- **向后兼容**：未知文件默认保留，不破坏特殊场景（如项目特有的运行时数据文件）
- **安全性**：所有已知开发元数据与凭证都排除，避免泄漏
- **可读性**：模式列表清晰列出每类排除项及原因

### 2. LICENSE 保留

不排除 `LICENSE`。原因：MIT/GPL/Apache 等开源协议要求「分发时随附 LICENSE 文件」。fspack 产物（exe + dist/src）属于分发，保留 LICENSE 是合规做法。fspack 的 NSIS 安装包（installer.py）不显式处理 LICENSE，src 中的 LICENSE 作为分发载体。

### 3. `*.md` / `*.rst` 全局排除

应用运行时不需要 README.md/CHANGELOG.rst 等文档。某些项目可能把 .md 作为资源文件，但属罕见情况，rule-02「精简产物」优先。子包内的 .md 同样剥离（测试 `test_copy_source_keeps_runtime_resources` 验证）。

### 4. `tests` 目录排除

仅排除 `tests/` 目录（复数），不排除 `test/`（单数）或 `test_*.py`。原因：避免误伤名为 `test` 的运行时模块或入口。开发期测试约定放在 `tests/` 目录（rule-11 测试规范）。

### 5. `.env` 安全排除（rule-11）

rule-11 安全要求「凭证放 .env/环境变量，.gitignore 须含 .env」。但 `_EXCLUDE` 此前未显式排除 `.env`，存在凭证泄漏到 dist 的风险。本次扩展显式排除 `.env` 与 `.env.*`。

## 代码实现情况

### `builder._EXCLUDE` 扩展

```python
_EXCLUDE = shutil.ignore_patterns(
    # 构建产物与 Python 缓存
    "dist", "build", "__pycache__", "*.egg-info", "*.pyc", "*.pyo",
    # 虚拟环境、测试与覆盖率
    ".venv", ".tox", ".pytest_cache", "htmlcov", ".coverage", ".coverage.*", "coverage.xml", "tests",
    # 工具缓存
    ".ruff_cache", ".pyrefly_cache", ".mypy_cache", ".uv-cache",
    # 版本控制
    ".git", ".gitignore", ".gitattributes",
    # IDE 与编辑器
    ".idea", ".vscode", "*.code-workspace",
    # fspack 自身目录
    ".fspack", ".trae",
    # 凭证与敏感信息
    ".env", ".env.*",
    # Python 项目元数据
    ".python-version", "pyproject.toml", "uv.lock", "uv.toml", "setup.py", "setup.cfg", "MANIFEST.in", "requirements*.txt",
    # 工具链配置文件
    "ruff.toml", ".ruff.toml", "pyrefly.toml", "pytest.ini", "tox.ini", ".bumpversion.toml", ".pre-commit-config.yaml", ".coveragerc", ".readthedocs.yaml", "Makefile", ".copier-answers.yml",
    # CI/CD
    ".github",
    # 文档
    "*.md", "*.rst", "docs",
)
```

### `copy_source` docstring 更新

明确描述保留范围（`.py`/数据文件/`LICENSE`）与排除类别（元数据/工具配置/凭证/文档/测试代码），并指向 `_EXCLUDE` 模式列表。

## 整合优化情况

无重复代码引入。新增测试与既有 `test_copy_source_*` 风格一致，复用 `tmp_path` fixture。

## 测试验证结果

- `test_copy_source_excludes_dist`：通过（既有）
- `test_copy_source_overwrites_existing`：通过（既有）
- `test_copy_source_strips_dev_artifacts`：通过（新增，覆盖 25+ 项开发文件剥离 + 8 个剥离目录）
- `test_copy_source_keeps_runtime_resources`：通过（新增，验证 LICENSE/.py/资源/子包保留，子包内开发文件同样剥离）
- 全套：463 passed, 13 deselected
- 覆盖率：98.10%（builder.py 97%，新增 `_EXCLUDE` 模式已被新测试覆盖）
- pyrefly: 0 errors
- ruff format: 通过

## 遗留事项

- ~~**ruff.toml ARG005 配置漂移**~~：已修复。memory iter-17 记录「ruff.toml 的 `**/tests/**` 须含 ARG005」，但实际 ruff.toml 仅配置 `ARG001`/`ARG002`，导致 ruff check 报 108 个 ARG005 错误。经用户授权后，在 `[lint.per-file-ignores]` 的 `"**/tests/**"` 中追加 `ARG005`，ruff check 现已通过（All checks passed!）。

## 下一轮计划

无。本次迭代需求已全部交付，所有门禁通过（ruff check / ruff format / pyrefly / pytest 覆盖率 98.10%）。
