"""$project_name 入口：argparse 命令行参数示例."""

import argparse


def main() -> None:
    """解析命令行参数并打印问候."""
    parser = argparse.ArgumentParser(prog="$project_name", description="$description")
    parser.add_argument("name", help="要问候的名字")
    parser.add_argument("-c", "--count", type=int, default=1, help="重复次数")
    args = parser.parse_args()

    for _ in range(args.count):
        print(f"hello, {args.name}!")


if __name__ == "__main__":
    main()
