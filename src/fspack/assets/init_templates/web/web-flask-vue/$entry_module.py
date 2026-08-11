"""$project_name 入口：Flask + Vue 前后端分离示例.

静态文件 serve 由 fspack wrapper 在 app.run() 时自动挂载（monkey-patch
Flask.run 注入 static_folder），入口仅需定义 API 路由。
"""

from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/api/hello")
def hello() -> object:
    """API 路由返回问候信息."""
    return jsonify({"message": "hello from $project_name!"})


def main() -> None:
    """启动 Flask 服务（前端由 wrapper 自动挂载到 dist 目录）."""
    print("$project_name: 启动 Flask 服务 http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000)


if __name__ == "__main__":
    main()
