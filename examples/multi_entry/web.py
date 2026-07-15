"""Web 入口：flask test_client 验证路由响应。."""


def main() -> None:
    """用 flask test_client 验证路由响应。."""
    from flask import Flask

    app = Flask(__name__)

    @app.route("/")
    def hello() -> str:
        return "hello from multi_entry web"

    with app.test_client() as client:
        resp = client.get("/")
        print(resp.get_data(as_text=True))


if __name__ == "__main__":
    main()
