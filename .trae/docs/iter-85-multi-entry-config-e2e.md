# iter-85：多入口/完整配置模板 2 项 + 端到端测试 + 文档收尾

## 需求清单

- [x] req-46：缓存环境变量配置 + 离线环境支持 + init 新建项目命令（全部完成）

## 迭代目标

完成 10 轮迭代计划最后一轮：添加 multi-entry 与 full-config 模板（模板总数达 22），
新增 init→ProjectInfo 解析端到端测试，更新 README 文档。10 轮迭代全部交付。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/fspack/templates/registry.py` | 新增 2 个模板（multi-entry/full-config） |
| `tests/test_init_templates.py` | 新增 iter-85 模板测试 11 项 + 模板总数验证 = 22 |
| `tests/test_init_e2e.py` | 新增：init → ProjectInfo.from_dir 解析端到端测试 15 项 |
| `README.md` | 新增"从模板开始：fsp init"章节 + 命令速查表添加 init + fsp init 详细说明 |

## 关键决策与依据

### multi-entry 模板：演示 [tool.fspack.entries] 多入口声明

此模板生成 3 个文件：
- `pyproject.toml`：含 `[tool.fspack.entries]` 声明 cli 与 gui 两个入口
- `src/cli.py`：argparse CLI 入口（app_type 自动推断为 cli）
- `src/gui.py`：tkinter GUI 入口（app_type 自动推断为 gui）

入口名（cli/gui）用作生成的 exe 名，fspack 会为每个入口生成独立 exe，共享运行时与依赖。
多入口模式下 `infer_app_type` 按各脚本自身 import 独立推断，不读项目级 declared。

### full-config 模板：完整项目结构最佳实践

此模板生成 5 个文件，展示实际项目起步结构：
- `pyproject.toml`：含 `[tool.fspack]` 实际配置（pyc_strip/pyc_optimize/no_stdlib_trim/exclude）
- `$entry_module.py`：入口脚本
- `README.md`：项目说明（含安装/运行/打包/测试命令）
- `.gitignore`：Python 项目 gitignore
- `tests/test_main.py`：测试骨架

与 pyinstaller 模板区别：pyinstaller 展示所有配置项（注释形式），full-config 展示实际
可用配置值 + 完整项目结构（含 README/tests/.gitignore），适合实际项目起步。

### 端到端测试设计

`tests/test_init_e2e.py` 验证 init → ProjectInfo.from_dir 解析流程：
- helloworld → 解析 name/version/app_type=cli
- pyside2 → 解析 app_type=gui（PySide2 import 触发）
- pyinstaller → 解析 build_defaults + exclude_dirs
- multi-entry → 解析 entries 含 cli 与 gui 两个入口
- full-config → 解析 build_defaults + exclude_dirs
- 10 个代表模板参数化验证可解析性

不实际执行 build（需 mingw/网络），仅验证 init → 项目元信息解析环节。

## 代码实现情况

### 新增模板内容

- `_MULTI_ENTRY_CLI` / `_MULTI_ENTRY_GUI`：多入口脚本
- `_MULTI_ENTRY_PYPROJECT`：含 [tool.fspack.entries] 声明
- `_FULL_CONFIG_ENTRY`：入口脚本
- `_FULL_CONFIG_README` / `_FULL_CONFIG_GITIGNORE` / `_FULL_CONFIG_TEST`：配套文件
- `_FULL_CONFIG_PYPROJECT`：含 [tool.fspack] 实际配置

### 文档更新

- README 新增"从模板开始：fsp init"章节（22 个模板分类表 + 用法示例）
- 命令速查表添加 `fsp init` / `fsp i` 行
- 新增 `### fsp init` 详细说明（参数表 + 模板分类统计）

## 测试验证结果

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：81 files already formatted
- `uv run pyrefly check`：0 errors
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：
  1274 passed, 1 skipped, 30 deselected，覆盖率 98.46%
- 新增测试：
  - `test_init_templates.py` iter-85 部分 11 项（注册/配置/语法/init_project）
  - `test_init_e2e.py` 15 项（init → 解析端到端）
- 模板总数验证 = 22（6 CLI + 6 GUI + 2 game + 3 sci + 2 web + 3 config）

## 10 轮迭代总结

| 轮次 | 主题 | 交付 |
|------|------|------|
| iter-76 | 缓存配置 + 计划 | 统一 cache 配置 + 10 轮路线图 |
| iter-77 | 下载层离线 | runtime/wheel/ccache/tkinter 离线 fail-fast |
| iter-78 | wheel 本地搜索 | --find-links + --no-index 离线增强 |
| iter-79 | 离线集成测试 + 文档 | 集成测试 + README/architecture 离线章节 |
| iter-80 | init 骨架 + 模板引擎 | string.Template 渲染引擎 + init 命令骨架 |
| iter-81 | 模板清单与选择 | --list + rich 交互式选择 |
| iter-82 | CLI 模板 6 项 | helloworld/args/rich/requests/click/typer |
| iter-83 | GUI 模板 6 项 | pyside2/pyside6/pyside2-qml/pyside6-qml/pyqt5/tkinter |
| iter-84 | 游戏/科学/Web 8 项 | pygame/snake/matplotlib/numpy/scipy/flask/fastapi/pyinstaller |
| iter-85 | 多入口/配置 2 项 + 收尾 | multi-entry/full-config + 端到端测试 + 文档 |

**模板总数 22**，满足"不少于 20 项"要求。

## 遗留事项

无。10 轮迭代计划全部交付。

## 下一轮计划

无。req-46 全部完成，可移动到 `.trae/req/done/`。
