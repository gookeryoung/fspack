# PyWebApp Demo

一个基于 pywebview 和 Vue 3 + Element Plus 的桌面应用示例项目，展示现代前端框架与 Python 桌面开发的结合。

## 功能特性

- **桌面应用**: 使用 pywebview 创建原生桌面窗口
- **现代UI**: 基于 Vue 3 + TypeScript + Vite + Element Plus 的前端界面
- **窗口控制**: 最小化、最大化、关闭等窗口操作
- **系统信息**: 获取系统平台和版本信息
- **Web兼容**: 前端可脱离桌面环境直接在浏览器中运行

## 技术栈

- **后端**: Python 3.8+ / pywebview
- **前端**: Vue 3 / TypeScript / Vite / Element Plus / UnoCSS / vue-router

## 安装依赖

```bash
# Python 依赖 (推荐 uv)
uv sync

# 前端依赖
cd src/webview_app/frontend
pnpm install
```

## 快速开始

```bash
# 1. 构建前端
cd src/webview_app/frontend
pnpm run build

# 2. 返回项目根目录运行应用
cd ../../..
python -m webview_app.cli

# 前端开发模式 (热更新)
python -m webview_app.cli --dev
```

## 项目结构

```bash
webview_app/
├── src/webview_app/
│   ├── __init__.py
│   ├── api.py                    # 暴露给前端的 Python API
│   ├── cli.py                    # 主程序入口
│   ├── server.py                 # 窗口服务与前端构建编排
│   └── frontend/                 # Vue 前端项目
│       ├── src/
│       │   ├── api.ts            # PyWebView API 封装
│       │   ├── App.vue           # 主应用组件 (侧边栏布局)
│       │   ├── main.ts           # 前端入口
│       │   └── views/            # 页面组件
│       ├── deploy/               # 构建输出 (运行 build 后生成)
│       └── package.json
├── pyproject.toml                # Python 项目配置
└── README.md
```

## API 接口

应用提供了以下 JavaScript API，供前端调用：

### 系统相关

- `get_system_info()` - 获取系统信息

### 窗口控制

- `minimize_window()` - 最小化窗口
- `maximize_window()` - 最大化/还原窗口
- `close_window()` - 关闭应用

### 应用信息

- `get_app_version()` - 获取应用版本

前端通过 `frontend/src/api.ts` 中的 `api` 单例调用上述接口，
未运行在桌面环境时自动回退到浏览器实现。

## Web 版本兼容性

前端应用同时支持在浏览器中运行，会自动检测运行环境：

- **桌面应用**: 完整功能支持
- **Web浏览器**: 基础界面展示，桌面功能不可用

## 自定义配置

- 窗口参数: `src/webview_app/server.py` 中的 `create_window` 调用
- 前端构建: `src/webview_app/frontend/vite.config.ts`

## 开发调试

```bash
# 前端开发服务
cd src/webview_app/frontend
pnpm run dev

# 启用 pywebview 开发者工具
python -m webview_app.cli --debug
```
