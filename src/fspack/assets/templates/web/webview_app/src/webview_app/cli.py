"""PyWebApp Demo - 一个基于pywebview的本地桌面应用."""

import argparse


def main() -> None:
    """主函数，启动webview应用."""
    from webview_app.api import SystemApi
    from webview_app.server import NativeServer

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug",
        "-d",
        action="store_true",
        help="启动调试模式",
    )
    parser.add_argument(
        "--build",
        "-b",
        action="store_true",
        help="启动构建模式",
    )
    parser.add_argument(
        "--dev",
        "-D",
        action="store_true",
        help="启动开发模式",
    )

    server = NativeServer()
    sys_api = SystemApi()

    args = parser.parse_args()
    if args.build:
        server.build()
        return

    if args.dev:
        server.development()
        return

    server.start(debug=args.debug, api_instance=sys_api)


if __name__ == "__main__":
    main()
