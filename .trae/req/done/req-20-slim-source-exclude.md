# 精简 dist/src 复制内容

## 需求清单

- [x] dist/src 仅保留应用运行所需源码与资源，剥离所有开发期文件
- [x] 排除 Python 项目元数据：`.python-version`、`pyproject.toml`、`uv.lock`、`uv.toml`、`setup.py`、`setup.cfg`、`MANIFEST.in`、`requirements*.txt`
- [x] 排除工具链配置文件（rule-11 独立配置文件）：`ruff.toml`、`.ruff.toml`、`pyrefly.toml`、`pytest.ini`、`tox.ini`、`.bumpversion.toml`、`.pre-commit-config.yaml`、`.coveragerc`、`.readthedocs.yaml`、`Makefile`、`.copier-answers.yml`
- [x] 排除凭证与敏感信息（rule-11 安全要求）：`.env`、`.env.*`
- [x] 排除文档：`*.md`、`*.rst`、`docs/`
- [x] 排除测试代码：`tests/`
- [x] 排除 IDE/版本控制/CI：`.git`、`.gitignore`、`.gitattributes`、`.idea`、`.vscode`、`*.code-workspace`、`.github`
- [x] 排除覆盖率/缓存：`.coverage`、`.coverage.*`、`coverage.xml`、`htmlcov/`、`.ruff_cache/`、`.pyrefly_cache/`、`.mypy_cache/`、`.uv-cache/`、`.pytest_cache/`
- [x] 保留 LICENSE：满足 MIT/GPL 等开源协议「随附 LICENSE」分发要求
- [x] 保留运行时资源：`*.py`、数据文件（`*.json`/`*.csv` 等）、`assets/`、子包
- [x] 向后兼容：未显式列出的文件默认保留，避免误删项目特有运行时资源

## 验收标准

- 现有 `test_copy_source_excludes_dist` / `test_copy_source_overwrites_existing` 测试通过
- 新增 `test_copy_source_strips_dev_artifacts` 覆盖 25+ 项开发文件剥离
- 新增 `test_copy_source_keeps_runtime_resources` 覆盖运行时资源保留（含子包内开发文件剥离）
- 全套门禁通过（ruff format / pyrefly / pytest 覆盖率 ≥ 95%）
