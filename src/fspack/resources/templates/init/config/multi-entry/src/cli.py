"""$project_name CLI 入口：argparse 参数解析."""

import argparse


def main() -> None:
    """CLI 入口：解析参数并打印问候."""
    parser = argparse.ArgumentParser(prog="$project_name", description="$description")
    parser.add_argument("name", help="要问候的名字")
    args = parser.parse_args()
    print(f"hello, {args.name}!")


if __name__ == "__main__":
    main()
