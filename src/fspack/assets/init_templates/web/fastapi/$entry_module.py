"""$project_name 入口：FastAPI Web 服务示例."""

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="$project_name", version="0.1.0")


@app.get("/")
def index() -> dict[str, str]:
    """根路由返回项目信息."""
    return {"name": "$project_name", "version": "0.1.0"}


@app.get("/hello/{name}")
def hello(name: str) -> dict[str, str]:
    """问候路由."""
    return {"message": f"hello, {name}!"}


def main() -> None:
    """启动 FastAPI 服务（uvicorn）."""
    print("$project_name: 启动 FastAPI 服务 http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
