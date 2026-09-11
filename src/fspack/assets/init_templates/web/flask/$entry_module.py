"""$project_name 入口：Flask Web 服务示例."""

import os

from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/")
def index() -> object:
    """根路由返回项目信息."""
    return jsonify({"name": "$project_name", "version": "0.1.0"})


@app.route("/hello/<name>")
def hello(name: str) -> object:
    """问候路由."""
    return jsonify({"message": f"hello, {name}!"})


def main() -> None:
    """启动 Flask 开发服务器."""
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    print("$project_name: 启动 Flask 服务 http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=debug)


if __name__ == "__main__":
    main()
