---
name: "python-cli"
description: "Python CLI 开发技能：Click/Typer 命令行应用开发，涵盖命令结构、参数选项、子命令组、输出格式化、交互确认、配置管理、错误处理、测试与打包入口。当需要开发命令行工具、定义子命令、处理参数选项、构建交互式 CLI、测试 CLI 命令时调用。"
---

# Python CLI 开发

自包含的命令行应用开发指南：命令结构、参数选项、子命令组、输出格式化、交互确认、配置管理、错误处理、测试与打包入口。新项目推荐 Typer（类型注解驱动、API 简洁）；复杂命令树/自定义补全用 Click。所有示例遵循 `rule-11-python-standards.md`（类型注解、中文 docstring、`from __future__ import annotations`）。

## 何时调用

- 需要开发命令行工具（CLI）或为库添加命令行入口
- 需要定义主命令 + 子命令结构（如 `git status`、`pip install`）
- 需要处理参数、选项、标志、环境变量、多值参数
- 需要格式化输出（颜色、表格、进度条）
- 需要交互式确认、提示输入、选择菜单
- 需要加载配置文件（TOML/JSON）并与命令行参数合并
- 需要自定义退出码、友好错误消息
- 需要测试 CLI 命令（CliRunner、stdout 捕获、exit code 断言）
- 需要配置打包入口（`[project.scripts]`）

## 选型决策

| 维度 | argparse | Click | Typer |
|------|----------|-------|-------|
| API 风格 | 命令式（add_argument） | 装饰器（@command） | 类型注解（函数签名） |
| 类型转换 | 手动 `type=` | `click.Type` 子类 | 自动从注解推断 |
| 子命令 | `add_subparsers` 较繁琐 | `@group` 天然支持 | `app.command()` 天然支持 |
| 帮助文本 | 自动生成 | 自动生成 + 富文本 | 自动生成 + Markdown |
| 依赖 | 标准库无依赖 | 需 click | 需 typer（含 click） |
| 学习成本 | 低 | 中 | 低（懂类型注解即可） |
| 适合场景 | 简单脚本 | 复杂命令树、自定义补全 | 新项目、类型驱动 |
| 测试支持 | 手动调用 | `CliRunner` | `CliRunner`（基于 click） |

决策准则：
- **新项目首选 Typer**：类型注解驱动、代码量少、自动生成帮助，与 `rule-11` 类型注解要求天然契合。
- **复杂命令树/嵌套 group/自定义补全** 用 Click：Typer 底层即 Click，复杂场景直接用 Click 更可控。
- **零依赖脚本** 用 argparse：标准库自带，仅适合单文件简单工具。
- 现有 `fspack` 的 `cli.py` 默认用 argparse；引入 Typer 时在 `pyproject.toml` 加 `typer>=0.12.0` 依赖。

## 命令结构

主命令 + 子命令模式：一个 `group`/`app` 作为入口，子命令挂载其下。

### Typer：app + command

```python
"""fspack CLI 入口."""

from __future__ import annotations

import typer

from fspack import __version__

__all__ = ["app", "main"]

app = typer.Typer(
    name="fspack",
    help="极速 Python 项目打包器。",
    no_args_is_help=True,  # 无参数时显示帮助
)


@app.callback(invoke_without_command=True)
def _version_callback(
    version: bool = typer.Option(False, "--version", "-V", help="显示版本号并退出"),
) -> None:
    """显示版本号."""
    if version:
        typer.echo(f"fspack ")
        raise typer.Exit


@app.command()
def greet(name: str = typer.Argument(..., help="目标名称")) -> None:
    """向指定名称问好."""
    typer.echo(f"你好，{name}！")


def main() -> None:  # pragma: no cover
    """CLI 主入口（需手动测试）."""
    app()
```

### Click：group + command

```python
from __future__ import annotations

import click

from fspack import __version__

__all__ = ["cli", "main"]


@click.group(help="极速 Python 项目打包器。")
@click.version_option(version=__version__, prog_name="fspack")
def cli() -> None:
    """fspack 命令组."""


@cli.command(help="向指定名称问好")
@click.argument("name")
def greet(name: str) -> None:
    """向指定名称问好."""
    click.echo(f"你好，{name}！")


def main() -> None:  # pragma: no cover
    """CLI 主入口（需手动测试）."""
    cli()
```

要点：
- 入口函数 `main()` 加 `# pragma: no cover`，覆盖率工具自动排除（见 `pyproject.toml` `exclude_lines`）。
- `no_args_is_help=True`（Typer）/ `invoke_without_command` 避免无参数时空跑。
- `__all__` 显式导出 `app`/`cli` 与 `main`。
- 帮助文本用中文，与 `rule-11` 一致。

## 参数与选项

Typer 通过类型注解表达；Click 通过装饰器声明。

```python
from __future__ import annotations

from pathlib import Path
from typing import Optional

import click
import typer

# --- Typer：类型注解驱动 ---
@app.command()
def deploy(
    target: str = typer.Argument(..., help="部署目标（必填）"),
    env: str = typer.Option("dev", "--env", "-e", help="环境名称", envvar="DEPLOY_ENV"),
    replicas: int = typer.Option(3, "--replicas", "-r", min=1, max=10, help="副本数"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅模拟不实际执行"),
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径", exists=True, dir_okay=False
    ),
    tags: list[str] = typer.Option([], "--tag", "-t", help="标签（可多次指定）"),
) -> None:
    """部署服务到指定目标."""
    typer.echo(f"部署 {target} 到 {env}，副本 {replicas}，dry_run={dry_run}")


# --- Click：装饰器声明 ---
@cli.command()
@click.argument("target")  # 位置参数（必填）
@click.option("--env", "-e", default="dev", help="环境名称", envvar="DEPLOY_ENV", show_default=True)
@click.option("--replicas", "-r", type=click.IntRange(1, 10), default=3, help="副本数")
@click.option("--dry-run", is_flag=True, default=False, help="仅模拟不实际执行")
@click.option(
    "--config", "-c",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="配置文件路径",
)
@click.option("--tag", "-t", "tags", multiple=True, help="标签（可多次指定）")
def deploy_click(
    target: str,
    env: str,
    replicas: int,
    dry_run: bool,
    config: Path | None,
    tags: tuple[str, ...],
) -> None:
    """部署服务到指定目标."""
    click.echo(f"部署 {target} 到 {env}，副本 {replicas}")
```

要点：
- **必填**：Typer 用 `...`（Ellipsis）；Click 省略 `default` 即必填。
- **类型转换**：Typer 从注解自动推断（`int`/`Path`/`bool`）；Click 用 `type=`（`click.IntRange`/`click.Path`）。
- **默认值**：直接赋值；`show_default=True` 在帮助中显示默认值。
- **环境变量**：`envvar=` 读取环境变量作为默认值；命令行显式传入时覆盖。
- **flag（布尔标志）**：Typer `bool` 注解 + `--flag`；Click `is_flag=True`。
- **多值**：Typer `list[str]` + 重复 `--tag`；Click `multiple=True` 返回 tuple。
- **路径校验**：`exists=True`/`dir_okay=False` 在解析阶段校验，避免运行时 FileNotFoundError。
- 选项用 `--long`/`-short` 双形式；位置参数仅用于最核心的必填项。

## 子命令与命令组

复杂工具拆分为多模块，通过 `add_typer`/`add_command` 合并到主入口。

### 嵌套命令组（Typer）

```python
# src/fspack/cli_db.py
from __future__ import annotations

import typer

app = typer.Typer(help="数据库管理子命令")


@app.command()
def migrate(direction: str = typer.Argument("up", help="迁移方向 up/down")) -> None:
    """执行数据库迁移."""
    typer.echo(f"迁移：{direction}")


@app.command()
def backup(archive: bool = typer.Option(False, "--archive", help="归档备份")) -> None:
    """备份数据库."""
    typer.echo(f"备份（归档={archive}）")
```

```python
# src/fspack/cli.py
from __future__ import annotations

import typer

from fspack import cli_db

app = typer.Typer(name="fspack", help="极速 Python 项目打包器。", no_args_is_help=True)

# 合并子模块命令组：fspack db migrate / fspack db backup
app.add_typer(cli_db.app, name="db", help="数据库管理")


def main() -> None:  # pragma: no cover
    """CLI 主入口."""
    app()
```

### 合并多模块命令（Click）

```python
from __future__ import annotations

import click

# 子模块定义独立 group
db_group = click.Group(name="db", help="数据库管理")


@db_group.command()
@click.argument("direction", default="up")
def migrate(direction: str) -> None:
    """执行数据库迁移."""
    click.echo(f"迁移：{direction}")


# 主入口合并
@click.group()
def cli() -> None:
    """fspack 命令组."""


cli.add_command(db_group, name="db")


def main() -> None:  # pragma: no cover
    """CLI 主入口."""
    cli()
```

要点：
- 每个子领域独立模块（`cli_db.py`/`cli_user.py`），主入口只负责合并。
- 命令名用动词（`migrate`/`backup`/`deploy`），命令组名用名词（`db`/`user`）。
- 嵌套层级 ≤ 2（`fspack db migrate`）；超过说明拆分不合理。
- `add_typer`/`add_command` 显式指定 `name`，避免模块名泄漏。

## 输出格式化

优先 `click.echo`（自动处理编码、终端检测）；`print` 仅用于调试残留（须删除）。

### 基础输出与颜色

```python
from __future__ import annotations

import click


def report_status(ok: bool, message: str) -> None:
    """输出带颜色的状态消息（自动检测终端是否支持颜色）."""
    color = "green" if ok else "red"
    prefix = "成功" if ok else "失败"
    # color 参数自动检测：非 TTY/重定向时自动剥离颜色码
    click.echo(click.style(f"[{prefix}] {message}", fg=color))


def report_warning(message: str) -> None:
    """输出警告消息到 stderr."""
    # err=True 输出到 stderr，便于 2>/dev/null 过滤
    click.secho(f"[警告] {message}", fg="yellow", err=True)
```

### 表格输出

简单表格用 `tabulate`；富文本用 `rich.table`。

```python
from __future__ import annotations

from typing import Sequence

from rich.console import Console
from rich.table import Table


def print_services(services: Sequence[dict]) -> None:
    """用 rich.table 输出服务列表."""
    console = Console()
    table = Table(title="服务列表")
    table.add_column("名称", style="cyan")
    table.add_column("状态", style="green")
    table.add_column("副本", justify="right")
    for svc in services:
        table.add_row(svc["name"], svc["status"], str(svc["replicas"]))
    console.print(table)
```

### 进度条

长任务用进度条反馈；`click.progress_bar` 轻量，`rich.progress` 功能丰富。

```python
from __future__ import annotations

import time
from pathlib import Path

import click
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn


def copy_files_progress(src: list[Path]) -> None:
    """用 click.progress_bar 复制文件（轻量）."""
    with click.progressbar(src, label="复制文件") as bar:
        for path in bar:
            time.sleep(0.01)  # 模拟 IO


def process_with_rich(tasks: list[str]) -> None:
    """用 rich.progress 处理任务（带 spinner + 进度条）."""
    columns = (SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn())
    with Progress(*columns, console=Console()) as progress:
        task = progress.add_task("处理任务", total=len(tasks))
        for name in tasks:
            progress.update(task, description=f"处理 {name}", advance=1)
            time.sleep(0.01)
```

要点：
- `click.echo` 优于 `print`：自动处理 Windows 编码、`sys.stdout`/`err` 切换。
- 颜色输出用 `click.style`/`secho` 的 `fg`/`bg`；`color` 参数自动检测终端，重定向时不输出颜色码。
- 错误/警告输出到 stderr：`err=True`，便于 `2>/dev/null` 过滤。
- 进度条仅用于可预估总量的长任务；不可预估用 spinner。
- `rich` 引入额外依赖，需在 `pyproject.toml` 声明；轻量场景用 `click.progress_bar`。

## 确认与交互

危险操作前确认；输入用 prompt。

```python
from __future__ import annotations

import click
import typer


@app.command()
def destroy(target: str = typer.Argument(..., help="销毁目标")) -> None:
    """销毁指定目标（危险操作，需二次确认）."""
    # abort=True 时拒绝则自动退出非零码
    typer.confirm(f"确认销毁 {target}？此操作不可逆", default=False, abort=True)
    typer.echo(f"已销毁 {target}")


@cli.command()
@click.argument("target")
def destroy_click(target: str) -> None:
    """销毁指定目标."""
    if not click.confirm(f"确认销毁 {target}？", default=False):
        click.echo("已取消")
        raise click.Abort
    click.echo(f"已销毁 {target}")


@app.command()
def init_config() -> None:
    """交互式初始化配置."""
    name = typer.prompt("项目名称")
    port = typer.prompt("端口", default=8080, type=int)
    # 隐藏输入 + 二次确认（用于密码/令牌）
    token = typer.prompt("访问令牌", hide_input=True, confirmation_prompt=True)
    env = typer.prompt("环境", default="dev")
    typer.echo(f"已初始化：{name} @ {port}")


@cli.command()
def init_config_click() -> None:
    """交互式初始化配置."""
    name = click.prompt("项目名称", type=str)
    port = click.prompt("端口", default=8080, type=int)
    token = click.prompt("访问令牌", hide_input=True, confirmation_prompt=True)
    # click.choice 限定取值
    env = click.prompt("环境", type=click.Choice(["dev", "staging", "prod"]), default="dev")
    click.echo(f"已初始化：{name} @ {port}")
```

要点：
- 危险操作（删除、覆盖、销毁）必须 `confirm`，`abort=True` 拒绝时自动退出非零码。
- `default=False` 默认拒绝，避免误回车导致破坏。
- 敏感输入（密码/令牌）用 `hide_input=True` + `confirmation_prompt=True`。
- 限定取值用 `click.Choice`（Click）或 `Enum` 注解（Typer）。
- 交互式提示仅用于首次初始化；日常命令应支持全参数化（便于脚本化）。

## 配置管理

配置来源优先级：命令行 > 环境变量 > 配置文件 > 默认值。

```python
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click


@dataclass(frozen=True)
class CliConfig:
    """CLI 配置（不可变，合并后冻结）."""

    env: str = "dev"
    timeout: float = 30.0
    verbose: bool = False


def load_config_file(path: Path) -> dict[str, Any]:
    """加载配置文件（按扩展名选 TOML/JSON 解析器）."""
    if not path.exists():
        return {}
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".toml":
        return tomllib.loads(text)
    if suffix in (".json", ".jsonc"):
        return json.loads(text)
    raise ValueError(f"不支持的配置格式: {suffix}")


def merge_config(
    defaults: CliConfig,
    file_data: dict[str, Any],
    env: dict[str, str],
    cli_overrides: dict[str, Any],
) -> CliConfig:
    """按优先级合并配置：cli > env > file > defaults."""
    merged: dict[str, Any] = dict(defaults.__dict__)
    for field_name in defaults.__dataclass_fields__:
        env_key = field_name.upper()
        if env_key in env:
            merged[field_name] = env[env_key]
        if field_name in file_data:
            merged[field_name] = file_data[field_name]
        if cli_overrides.get(field_name) is not None:
            merged[field_name] = cli_overrides[field_name]
    return CliConfig(**merged)


@click.group()
@click.option(
    "--config", "-c", "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.toml"),
    help="配置文件路径",
)
@click.option("--env", "-e", default=None, help="环境（覆盖配置文件）")
@click.option("--verbose", "-v", is_flag=True, default=None, help="详细输出")
@click.pass_context
def cli(ctx: click.Context, config_path: Path, env: str | None, verbose: bool | None) -> None:
    """fspack 命令组，支持配置文件与环境变量."""
    defaults = CliConfig()
    file_data = load_config_file(config_path)
    config = merge_config(defaults, file_data, dict(os.environ), {"env": env, "verbose": verbose})
    ctx.obj = config  # 通过 ctx.obj 向子命令传递配置


@cli.command()
@click.pass_obj
def status(config: CliConfig) -> None:
    """显示当前配置状态."""
    click.echo(f"环境={config.env}，超时={config.timeout}，详细={config.verbose}")
```

要点：
- 优先级链：命令行 > 环境变量 > 配置文件 > 代码默认值；合并后冻结为 `@dataclass(frozen=True)`。
- TOML 用标准库 `tomllib`（3.11+）；低版本用 `tomli`，按版本守卫引入。
- `ctx.obj` 在 group 与子命令间传递合并后的配置对象。
- 配置路径用 `click.Path(path_type=Path)` 直接得 `Path`，避免字符串拼接。
- 环境变量命名 `UPPER_SNAKE`，与字段名映射（`env` → `ENV`）。

## 错误处理

自定义异常转友好消息；退出码语义化。

```python
from __future__ import annotations

import click


class ConfigError(click.ClickException):
    """配置错误，退出码 2."""

    exit_code = 2


class NetworkError(click.ClickException):
    """网络错误，退出码 3."""

    exit_code = 3


@cli.command()
@click.option("--url", required=True, help="目标 URL")
def fetch(url: str) -> None:
    """获取远程资源."""
    try:
        data = _do_request(url)
    except TimeoutError as exc:
        # 网络异常转友好消息，保留因果链
        raise NetworkError(f"请求超时：{url}") from exc
    except ValueError as exc:
        raise ConfigError(f"配置无效：{exc}") from exc
    click.echo(data)


def _do_request(url: str) -> str:
    """模拟请求（实际调用 httpx 等）."""
    raise TimeoutError(url)


# 退出码约定
# 0  成功
# 1  通用错误（未捕获异常）
# 2  配置错误（参数/配置文件无效）
# 3  网络错误（超时/连接失败）
# 4  数据错误（文件不存在/格式错误）
```

Typer 中用 `typer.BadParameter`（参数错误）与 `typer.Exit(code=N)`（主动退出）：

```python
from __future__ import annotations

import typer


@app.command()
def fetch(url: str = typer.Argument(..., help="目标 URL")) -> None:
    """获取远程资源."""
    if not url.startswith(("http://", "https://")):
        # 参数校验失败，退出码 2
        raise typer.BadParameter("URL 必须以 http:// 或 https:// 开头")
    try:
        _do_request(url)
    except TimeoutError as exc:
        raise typer.Exit(code=3) from exc


def _do_request(url: str) -> str:
    """模拟请求."""
    raise TimeoutError(url)
```

要点：
- 自定义异常继承 `click.ClickException`，`show()` 自动格式化输出到 stderr。
- `exit_code` 类属性定义语义化退出码；脚本可据此分支处理。
- `raise NewError(...) from exc` 保留因果链，调试时可见原始异常。
- 业务异常转 `ClickException`；`KeyboardInterrupt` 由 Click/Typer 自动处理为退出码 1。
- 禁止 `except Exception: pass`（见 `rule-11`）；捕获后必须记录/包装/重抛。
- 退出码约定在模块文档注释中声明，便于调用方脚本判断。

## 测试 CLI

用 `CliRunner` 调用命令，捕获 stdout/stderr 与退出码。

```python
"""fspack CLI 测试."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fspack.cli import app

runner = CliRunner()


def test_greet_outputs_name() -> None:
    """greet 命令应输出问候语."""
    result = runner.invoke(app, ["greet", "张三"])
    assert result.exit_code == 0
    assert "你好，张三" in result.stdout


def test_version_flag_exits_zero() -> None:
    """--version 应输出版本号并退出 0."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "fspack" in result.stdout


def test_missing_required_arg_exits_nonzero() -> None:
    """缺少必填参数应非零退出."""
    result = runner.invoke(app, ["greet"])
    assert result.exit_code != 0
    assert "Missing argument" in result.stdout


def test_dry_run_flag_default_false() -> None:
    """--dry-run 默认 False."""
    result = runner.invoke(app, ["deploy", "prod", "--dry-run"])
    assert result.exit_code == 0
    assert "dry_run=True" in result.stdout


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量应覆盖默认值."""
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    result = runner.invoke(app, ["deploy", "prod"])
    assert result.exit_code == 0
    assert "staging" in result.stdout


def test_interactive_input_simulated() -> None:
    """交互式命令可通过 input 模拟 stdin."""
    result = runner.invoke(app, ["init-config"], input="myname\n8080\nsecret\nsecret\ndev\n")
    assert result.exit_code == 0
    assert "已初始化：myname @ 8080" in result.stdout
```

Click 测试同理（`from click.testing import CliRunner`，`runner.invoke(cli, [...])`）。

要点：
- `CliRunner.invoke(app, [args])` 在进程内调用，不启动子进程，速度快。
- `result.exit_code` 断言退出码；`result.stdout` 捕获标准输出。
- `result.exception` 捕获抛出的异常（配合 `catch_exceptions=False` 可不捕获）。
- 环境变量用 `monkeypatch.setenv`（见 `rule-11` Mock 优先级）。
- 交互式命令测试：`input="值\n值\n"` 模拟 stdin 逐行输入。
- `main()` 入口加 `# pragma: no cover`，通过 `app`/`cli` 对象测试覆盖逻辑。

## 打包入口

`pyproject.toml` 声明 `[project.scripts]`，安装后生成可执行命令。

```toml
[project]
name = "fspack"

[project.scripts]
fspack = "fspack.cli:main"

[project.optional-dependencies]
cli = ["typer>=0.12.0"]
# 若用 rich 输出：cli = ["typer>=0.12.0", "rich>=13.0.0"]
```

安装后即可使用：

```bash
uv pip install -e ".[cli]"
fspack --version
fspack greet 张三
fspack db migrate
```

调用子进程时须包含 `check=True`（ruff `PLW1510` 强制），失败时自动抛 `CalledProcessError`：

```python
from __future__ import annotations

import subprocess


def run_external(cmd: list[str]) -> str:
    """调用外部命令并返回 stdout（check=True 确保失败时抛异常）."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603
    return result.stdout
```

要点：
- `[project.scripts]` 键名即命令名，值 `包.模块:函数` 指向入口函数（即 `console_scripts` 等价物）。
- 入口函数必须是零参数可调用对象（`def main() -> None`）。
- Typer/Click/rich 作为 `optional-dependencies` 的 `cli` extra，按需安装；核心库不强制依赖 CLI。
- 开发时 `uv pip install -e ".[dev]"` 安装全部 extras。
- `subprocess.run` 必须加 `check=True`（ruff `PLW1510`）；禁用 `shell=True`（见 `rule-11` 安全章节）。
- `main()` 加 `# pragma: no cover`；逻辑通过测试 `app`/`cli` 对象覆盖。

## 常见陷阱

1. **用 `print` 代替 `click.echo`**：`print` 不处理 Windows 编码与终端检测，重定向时颜色码泄漏。一律用 `click.echo`。
2. **颜色码污染重定向输出**：未检测终端直接输出 ANSI 码，`fspack > log.txt` 含乱码。用 `click.echo` 的 `color` 参数或 `click.style` 自动检测。
3. **必填参数误设为可选**：Typer 中 `Optional[str] = None` 会让必填变为可选。必填用 `typer.Argument(...)`；Click 省略 `default` 即必填。
4. **可变默认值**：`def f(tags=[])` 多次调用共享列表。Typer/Click 用 `list[str]` + `Option`/`multiple=True`，避免裸可变默认。
5. **`subprocess.run` 缺 `check=True`**：失败时静默继续，错误被吞没。ruff `PLW1510` 强制要求 `check=True`；同时禁用 `shell=True`。
6. **环境变量优先级错误**：配置文件覆盖了命令行参数。严格按 cli > env > file > defaults 合并，命令行显式传入时必须最终生效。
7. **入口函数带参数**：`def main(args)` 无法被 `[project.scripts]` 调用（需零参数）。用 `app()`/`cli()` 内部解析 `sys.argv`。
8. **`KeyboardInterrupt` 未处理**：Ctrl+C 时栈跟踪泄露。Click/Typer 自动捕获为退出码 1；裸 argparse 需 `try/except KeyboardInterrupt`。
9. **子命令名冲突**：多个模块注册同名命令，后者覆盖前者。`add_typer`/`add_command` 显式指定 `name`，命名前缀隔离。
10. **进度条写入 stdout 干扰管道**：`fspack process | grep` 时进度条混入数据流。进度条用 stderr（`rich.Console(stderr=True)`），数据输出用 stdout。
