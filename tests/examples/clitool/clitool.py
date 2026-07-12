"""有库 CLI 示例：用 requests 打印版本号。."""


def main() -> None:
    """打印 requests 版本号。."""
    import requests

    print(f"requests {requests.__version__}")


if __name__ == "__main__":
    main()
