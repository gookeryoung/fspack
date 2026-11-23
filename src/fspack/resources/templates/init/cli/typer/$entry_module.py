"""$project_name 入口：typer CLI 框架示例."""

import typer

app = typer.Typer(help="$project_name 命令行工具")


@app.command()
def hello(
    name: str = typer.Argument("world", help="要问候的名字"),
    count: int = typer.Option(1, "-c", "--count", help="重复次数"),
) -> None:
    """问候指定名字."""
    for _ in range(count):
        typer.echo(f"hello, {name}!")


@app.command()
def version() -> None:
    """显示版本号."""
    typer.echo("$project_name v0.1.0")


if __name__ == "__main__":
    app()
