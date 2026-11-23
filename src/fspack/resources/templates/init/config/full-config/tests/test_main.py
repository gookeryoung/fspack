"""$project_name 测试骨架."""

from $entry_module import main


def test_main(capsys: object) -> None:
    """测试 main 函数输出含 hello, world."""
    main()
    # capsys 是 pytest fixture，捕获 stdout/stderr
    # 此处仅作骨架演示，实际项目按需补充断言
