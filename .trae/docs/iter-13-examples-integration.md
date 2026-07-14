# 迭代 13：示例目录整合

## 迭代目标

1. 将 `tests/examples/` 下 8 个测试示例移到 `examples/`，删除 `tests/examples/`
2. 按 `<类型>_<名称>` 下划线风格统一重命名，`name` 字段 = 目录名
3. 更新所有测试引用与文档

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `examples/cli_helloworld/` | 从 tests/examples/helloworld 移入，name 改为 cli_helloworld |
| `examples/cli_tool/` | 从 tests/examples/clitool 移入，name 改为 cli_tool |
| `examples/gui_calc/` | 从 tests/examples/guicalc 移入，name 改为 gui_calc |
| `examples/pygame_demo/` | 从 tests/examples/pygamedemo 移入，name 改为 pygame_demo |
| `examples/pyside2_app/` | 从 tests/examples/pyside2app 移入，name 改为 pyside2_app（含 .python-version + README.md） |
| `examples/pyqt5_app/` | 从 tests/examples/pyqt5app 移入，name 改为 pyqt5_app |
| `examples/pygame_snake/` | 从 tests/examples/snake 移入替换展示版，name 改为 pygame_snake（删除展示版 assets/README/__init__） |
| `examples/web_app/` | 从 tests/examples/webapp 移入，name 改为 web_app |
| `tests/test_project.py` | _EXAMPLES 路径改为 parent.parent；helloworld→cli_helloworld、pyside2app→pyside2_app |
| `tests/test_builder.py` | _EXAMPLES 路径改为 parent.parent；helloworld→cli_helloworld（路径/name/exe 名） |
| `tests/test_e2e_slow.py` | _EXAMPLES 路径改为 parent.parent；8 个示例名全部更新（_build_and_run 参数、tmp_path、copytree、exe 名、installer 产出名、docstring） |
| `pyproject.toml` | pyrefly project-excludes 删除 tests/examples/** |
| `README.md` | 示例章节更新为 examples/ 路径，表格补全 11 个示例 |

## 关键决策与依据

### name 字段 = 目录名（下划线）

- `exe_name = f"{name}.exe"`（config.py），slow 测试用 `f"{proj_name}.exe"`（proj_name=目录名）定位 exe
- 若 name 用连字符而目录名用下划线，exe 名含连字符，测试 `f"{proj_name}.exe"` 失配
- 决策：name 字段 = 目录名（下划线），保证 exe 名 = 目录名，测试仅字符串替换
- 展示示例（cli-complex 等）不在本次统一范围，保持连字符

### 入口 .py 文件不重命名

- 目录改名但入口文件保留原名（如 helloworld.py），减少改动量
- `detect_entry`（project.py）兜底扫描顶层 `*.py` 处理 name≠文件名情况
- `entry_module` 断言仍用原文件名（如 `"helloworld"`），无需改

### 测试函数名不改

- `test_build_and_run_helloworld` 等函数名保留旧名
- 与 test_project.py/test_builder.py 一致（这两个文件也只改路径和断言，不改函数名）
- 函数名仅影响 `-k` 选择性运行，不影响测试逻辑

### 删除展示版 pygame_snake

- 展示版 pygame_snake（256 行，含 assets/music/README）与测试版 snake 重叠
- 用户确认只保留 snake（测试版），删除展示版
- 测试版 snake 改名为 pygame_snake 移入 examples/，复用目录名

## 验证结果

### 门禁

- `ruff check src tests`：All checks passed
- `ruff format --check src tests`：45 files already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack --cov-fail-under=95`：296 passed, coverage 98.42%

### 实测

- `fspack b examples/cli_helloworld` 构建成功，产出 `cli_helloworld.exe`
- 运行 exe 输出 "hello, world"
- 构建产物已清理

## 遗留事项

- 无
