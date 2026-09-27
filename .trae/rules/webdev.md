---
alwaysApply: false
globs: frontend/**/*.ts, frontend/**/*.tsx, src/**/*.py, frontend/**/*.d.ts
---
# Web 全栈开发规范

## 技术栈

- 包管理：前端 pnpm，后端 uv（**禁止** npm/yarn/pip 混用）。
- 前端：Vite + React + TypeScript strict；UI 用 Ant Design；状态用 zustand + React Query。
- 后端：FastAPI, Python 3.12+, Pydantic v2, SQLAlchemy 2.0, Alembic 迁移；DB: SQLite / PostgreSQL。
- 服务：uvicorn[standard] 启动（自带 uvloop）；打包分发用 fspack。

## 前端规则

- AntD 组件优先；自定义样式走 CSS 专用类 + 主题变量（`var(--*)`），禁止硬编码颜色，避免与 AntD 样式体系冲突。
- Vite dev server 用 proxy 代理 `/api` 与静态资源到后端；生产构建产物同步到后端 static 目录同源部署，无需 CORS。

## FastAPI 后端规则

- 全部使用 `async def`；必须声明 `response_model`；使用 `lifespan`，禁止 `@on_event`。
- 分层：api/routers/* → services/* → models/*；路由层禁止写 SQL / ORM 逻辑。
- 请求 / 响应全部用 Pydantic v2；配置用 `pydantic-settings`；字段名 / 必填 / 枚举以 FastAPI 为源。
- 鉴权：依赖注入认证后写入 `request.state.user_id`；支持 JWT / API Token（仅创建时明文展示，存储 SHA-256 哈希）。
- 日志用 `logging`，禁止 `print`；禁止裸 `except:`。
- 错误响应：业务校验失败返回 400 + 出错明细；服务层抛出的异常必须在路由层完成映射（避免 500）。
- 性能：DB 连接池；长任务用 BackgroundTasks / 任务表模式。

## 类型契约

- FastAPI 是类型真理源；修改 Pydantic schema 后必须同步更新前端 `src/types/` 的接口类型定义。
- 新增接口必须补：Pydantic schema + 前端类型 + API 测试。

> 目录约定：后端位于 `backend/`（兼容 `api/`、`src/` 布局），前端位于 `frontend/`。若实际项目布局不同，以项目内实际结构调整 glob。
