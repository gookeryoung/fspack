# 需求：fsp init 支持指定 Python 版本 + Win7 下 fastapi 模板拦截

## 背景

`fsp init` 创建项目时，模板默认的 `requires-python` 约束固定（如 `>=3.8,<3.12`），
用户无法按目标 Python 版本定制。同时 FastAPI 模板在 Win7 下因 pydantic-core
依赖链调用 Win8+ API（PathCchSkipRoot）无法运行，但当前无前置拦截，用户在
Win7 选 fastapi 模板创建项目后，到打包阶段才会遇到依赖安装失败，体验差。

## 需求清单

- [x] `fsp init --python-version <X.Y>` 覆盖模板默认 `requires-python` 下限
- [x] Win7 下选择 fastapi 模板时前置报错，提示 Win7 不支持并建议升级或换用 flask

## 验收标准

- `--python-version 3.10` 生成的 `pyproject.toml` 含 `requires-python = ">=3.10"`
- `--python-version` 接受 `X.Y` 格式（3.8/3.10/3.11），非法格式退出码 1 并打印错误
- Win7（NT 6.1）下 `fsp init --template fastapi` 退出码 1，错误信息含 "Win7" 与原因
- 非 Win7 或其他模板不受影响
- 全套门禁通过（ruff/format/pyrefly/pytest/coverage ≥ 95%）
