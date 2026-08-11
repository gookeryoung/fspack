# req-51: 前后端分离 Web 应用打包

## 背景

fspack 当前仅支持 CLI（保留控制台）与 GUI（关闭控制台）两类应用。Web 框架
（Flask/FastAPI 等）默认走 CLI 类型，打包后会保留黑色控制台窗口，与 Web
桌面应用形态不符。前后端分离项目（如 Flask 后端 + Vue 前端、FastAPI 后端 +
React 前端）打包时还需手动处理静态文件 serve、端口监听、自动打开浏览器等
细节，缺少开箱即用的方案。

## 需求清单

- [x] 新增 `[tool.fspack] web-static-dirs` 配置项，声明前端构建产物目录
      （相对项目目录的 POSIX 路径，如 `dist`），打包时原样保留目录树
      （与 `data-dirs` 同等保护：跳过元数据排除与 `.py` 剥离）。
- [x] 新增 `AppType.WEB` 枚举值。`infer_app_type` 检测入口脚本 import
      Flask/FastAPI/Sanic/Django/Tornado/Starlette/Uvicorn/Hypercorn/Quart
      时返回 `AppType.WEB`。
- [x] `AppType.WEB` 打包时与 GUI 一致：Windows loader 加 `-mwindows`
      关闭控制台窗口；runner 检测到 WEB 类型且非 debug 模式时同样提示
      "输出被吞"。
- [x] `default_entry` 排序键调整为 GUI 优先于 WEB 优先于 CLI：
      `(0 if GUI else 1 if WEB else 2, name)`。
- [x] `EntryWrapper.generate_wrapper_source` 新增 `web_static_dirs`/
      `open_browser` 参数，wrapper 在用户代码运行前注入
      monkey-patch：
      - 包装 `flask.Flask.run` 与 `uvicorn.run`，在用户调用 `app.run()`
        / `uvicorn.run()` 时挂载静态文件 serve（Flask `static_folder`、
        FastAPI `StaticFiles`），并启动 `threading.Timer` 调
        `webbrowser.open` 打开浏览器。
      - `web_static_dirs` 在打包时解析为 dist 内绝对路径，wrapper 直接
        使用，不依赖工作目录。
- [x] 新增 `BuildOptions.open_browser` 字段与 `[tool.fspack] open-browser`
      配置默认；CLI 新增 `--open-browser` 标志。WEB 类型默认启用，
      CLI 显式 `--open-browser` 强制启用、配置层 `open-browser = true`
      对非 WEB 类型也可启用（如 GUI 内嵌 WebView）。
- [x] 新增 `web-flask-vue` 与 `web-fastapi-react` 两套模板，演示前后端
      分离项目结构与 `web-static-dirs` 配置写法。模板入口仍含
      `app.run()` 供非打包开发使用，打包时由 wrapper monkey-patch 覆盖。
- [x] 配套单元测试与集成测试，覆盖率 ≥ 95%，全套门禁通过
      （ruff format/check、pyrefly、pytest）。

## 验收标准

- `fsp init -t web-flask-vue myapp && cd myapp && fsp b` 生成无控制台窗口
  的 exe，运行后自动打开浏览器访问 `http://127.0.0.1:5000/`，前端页面
  正常加载。
- `fsp init -t web-fastapi-react myapp && cd myapp && fsp b` 同上，端口
  为 8000。
- 既有 CLI/GUI 模板行为不变，门禁全通过。
