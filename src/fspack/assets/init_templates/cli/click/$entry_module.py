"""$project_name 入口：click CLI 框架示例."""

import click


@click.group()
def cli() -> None:
    """$project_name 命令行工具."""


@cli.command()
@click.argument("name")
@click.option("-c", "--count", default=1, help="重复次数")
def hello(name: str, count: int) -> None:
    """问候指定名字."""
    for _ in range(count):
        click.echo(f"hello, {name}!")


@cli.command()
def version() -> None:
    """显示版本号."""
    click.echo("$project_name v0.1.0")


if __name__ == "__main__":
    cli()
