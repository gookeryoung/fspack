"""$project_name 入口：rich 终端美化示例."""

from rich.console import Console
from rich.table import Table


def main() -> None:
    """用 rich 输出彩色表格."""
    console = Console()
    console.print("[bold green]$project_name[/bold green]")

    table = Table(title="项目信息")
    table.add_column("字段", style="cyan", no_wrap=True)
    table.add_column("值", style="magenta")
    table.add_row("name", "$project_name")
    table.add_row("version", "0.1.0")
    table.add_row("type", "rich CLI")
    console.print(table)


if __name__ == "__main__":
    main()
