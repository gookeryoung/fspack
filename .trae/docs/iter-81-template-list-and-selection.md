# iter-81：模板清单与选择界面

## 需求清单

- [ ] req-46：缓存环境变量配置 + 离线环境支持 + init 新建项目命令
  - [x] 10. 模板清单与选择界面（fsp init --list / --template / rich 交互）

## 迭代目标

实现 `fsp init` 命令的交互式模板选择界面：当 `--template` 未指定且 stdin
是 TTY 时，弹出 rich 渲染的分类编号列表供用户选择；非 TTY 环境（CI/管道）
自动回退到 helloworld 默认值。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/fspack/cli_init.py` | 新增 `prompt_template_selection()` 交互式选择函数 |
| `src/fspack/cli.py` | `_run_init` 分发逻辑：`--template` 未指定时调用交互选择 |
| `tests/test_init_list.py` | 新增 11 个测试，覆盖交互选择与 CLI 分发 |

## 关键决策与依据

### 交互式选择实现：rich + IntPrompt

**问题**：需要交互式选择界面，可选方案有 `inquirer`/`questionary`（额外依赖）、
`input()` 原生（无美化）、`rich.prompt.IntPrompt`（零额外依赖）。

**方案**：选 `rich.prompt.IntPrompt`：

- rich 已是 fspack 依赖，零新增依赖
- 用 rich 渲染分类编号列表，美观一致
- IntPrompt 内置 choices 校验与默认值提示
- 非 TTY 环境自动跳过交互，CI 友好

### TTY 检测与非交互回退

```python
def prompt_template_selection() -> str:
    if not sys.stdin.isatty():
        _logger.info("非交互式环境，使用默认模板 helloworld")
        return "helloworld"
    # ... 交互式选择
```

**理由**：

- CI/管道环境 stdin 非 TTY，调用 `IntPrompt.ask` 会阻塞等待输入或抛 EOFError
- 自动回退到 helloworld 默认值，保证 CI 中 `fsp init <name>` 可用
- 用户在 CI 中需指定模板时显式用 `--template <id>`

### CLI 分发逻辑

```python
def _run_init(ns):
    if ns.list:
        print_template_list()
        return
    template_id = ns.template
    if template_id is None:
        try:
            template_id = prompt_template_selection()
        except KeyboardInterrupt:
            console.rich.print("\n[yellow]已取消[/]")
            sys.exit(1)
    init_project(ns.project_name, template_id=template_id, ...)
```

`--template` 默认值从 `helloworld` 改为 `None`，区分「显式指定」与「未指定需交互」。

## 代码实现情况

### prompt_template_selection 输出格式

```text
> 可用项目模板（共 22 个）：

  [cli]
     1. helloworld — Hello World
        最小 Hello World 示例，验证基础流水线
     2. args — argparse 命令行参数
        ...
  [gui]
     7. pyside2 — PySide2 桌面 GUI
        ...

请选择模板 [1-22] (默认 1):
```

### 显式 int 转换修复 pyrefly 类型推断

`IntPrompt.ask` 签名返回 `Any`，pyrefly 因 `default="1"` 推断返回 `str`，
导致 `choice - 1` 报错。用 `int(...)` 显式转换修复：

```python
choice = int(IntPrompt.ask(..., default="1", ...))
selected = templates[choice - 1]
```

## 测试验证结果

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：79 files already formatted
- `uv run pyrefly check`：0 errors
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：
  1169 passed, 1 skipped, 30 deselected，覆盖率 98.38%

### 新增测试（11 个）

| 测试 | 验证点 |
|------|--------|
| `test_prompt_template_selection_non_tty_returns_helloworld` | 非 TTY → 返回 helloworld 默认值 |
| `test_prompt_template_selection_tty_returns_selected` | TTY + 输入 1 → 返回 helloworld |
| `test_prompt_template_selection_tty_returns_second_template` | TTY + 输入 2 → 返回第 2 个模板 |
| `test_prompt_template_selection_empty_registry_returns_helloworld` | 空注册表 → 防御性回退 helloworld |
| `test_cli_init_list_prints_and_exits` | `--list` 打印列表后退出 |
| `test_cli_init_list_with_alias_i` | 别名 `i --list` 同样支持 |
| `test_cli_init_no_template_non_tty_uses_helloworld` | 未指定 --template + 非 TTY → 用 helloworld |
| `test_cli_init_explicit_template_skips_prompt` | 显式 --template → 跳过交互选择 |
| `test_cli_init_unknown_template_errors` | 未知模板 id → 错误退出码 1 |
| `test_cli_init_description_passed_to_pyproject` | --description 透传到 pyproject.toml |
| `test_cli_init_directory_option` | --directory 指定父目录 |

## 整合优化情况

- 交互式选择函数 `prompt_template_selection` 与 `init_project` 解耦，
  便于独立测试与未来扩展（如支持模糊搜索）
- 非 TTY 回退保证 CI 友好，无需在 CI 脚本中显式指定 `--template`
- KeyboardInterrupt 优雅处理，打印"已取消"后退出码 1
- 模板列表渲染复用 `print_template_list` 的分类分组逻辑，保持视觉一致

## 遗留事项

- iter-82：CLI 模板 6 项（helloworld/args/rich/requests/click/typer）

## 下一轮计划

iter-82：CLI 模板 6 项

- `helloworld`（已存在，iter-80 骨架）
- `args` — argparse 命令行参数
- `rich` — rich 终端美化
- `requests` — HTTP 请求客户端
- `click` — Click CLI 框架
- `typer` — Typer CLI 框架

每个模板含 pyproject.toml + 入口脚本 + 必要的依赖声明，验证 init→build 端到端流程。
