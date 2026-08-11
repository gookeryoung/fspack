"""$project_name 入口：FastAPI + React 前后端分离示例.

静态文件 serve 由 fspack wrapper 在 uvicorn.run() 时自动挂载（monkey-patch
uvicorn.run 注入 StaticFiles），入口仅需定义 API 路由。
"""

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="$project_name", version="0.1.0")


@app.get("/api/hello")
def hello() -> dict[str, str]:
    """API 路由返回问候信息."""
    return {"message": "hello from $project_name!"}


def main() -> None:
    """启动 FastAPI 服务（前端由 wrapper 自动挂载到 dist 目录）."""
    print("$project_name: 启动 FastAPI 服务 http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
