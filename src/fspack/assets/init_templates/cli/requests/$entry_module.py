"""$project_name 入口：requests HTTP 请求示例."""

import requests


def main() -> None:
    """发送 HTTP GET 请求并打印响应."""
    print("$project_name: 发送 HTTP 请求...")
    response = requests.get("https://httpbin.org/get", params={"name": "$project_name"}, timeout=10)
    response.raise_for_status()

    data = response.json()
    print(f"请求 URL: {data['url']}")
    print(f"响应参数: {data['args']}")


if __name__ == "__main__":
    main()
