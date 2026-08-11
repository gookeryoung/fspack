# iter-148: 前后端分离 Web 打包功能

## 需求清单

- [x] 新增 `[tool.fspack] web-static-dirs` 配置项
- [x] 新增 `AppType.WEB` 枚举值，`infer_app_type` 识别 web 框架
- [x] `AppType.WEB` 打包关闭控制台（`-mwindows`），runner 提示输出被吞
- [x] `default_entry` 排序键 GUI > WEB > CLI
- [x] `EntryWrapper` 注入 Flask/FastAPI monkey-patch：静态文件 serve + 开浏览器
- [x] 新增 `BuildOptions.open_browser` + CLI `--open-browser` 标志
- [x] 新增 `web-flask-vue` 与 `web-fastapi-react` 两套模板
- [x] 配套测试 28 个，门禁全通过

## 迭代目标

为 fspack 新增前后端分离 Web 应用打包能力：自动识别 Flask/FastAPI 等框架
为 WEB 类型，关闭控制台窗口，打包时注入静态文件 serve 与自动开浏览器逻辑，
提供开箱即用的 web-flask-vue 与 web-fastapi-react 项目模板。

## 改动文件清单

### 源码（11 文件）

- `src/fspack/config/models.py`：`AppType.WEB` 枚举、`ProjectInfo.web_static_dirs`、
  `BuildDefaults.open_browser`、`BuildOptions.open_browser`、`default_entry` 排序键
- `src/fspack/config/parsing.py`：`_WEB_HINTS` 集合、`infer_app_type` WEB 分支、
  `_parse_web_static_dirs`、`_BUILD_DEFAULT_KEYS` 加 `open_browser`
- `src/fspack/packaging/entry.py`：`_WRAPPER_TEMPLATE` 注入 web 静态 serve 块、
  `generate_wrapper_source` 新增 `web_static_dirs`/`open_browser` 参数
- `src/fspack/packaging/pipeline/stages.py`：`_build_one_loader` 传 web 参数、
  `_compile_user_sources` 解析 `web_static_dirs` 为绝对路径
- `src/fspack/packaging/pipeline/__init__.py`：`copy_source` 透传 `web_static_dirs`
- `src/fspack/packaging/loader/compile.py`：`_build_command` 对 `AppType.WEB` 加 `-mwindows`
- `src/fspack/packaging/sync.py`：`copy_source` 新增 `web_static_dirs` 参数、
  `_build_ignore_fn` 合并 data_dirs + web_static_dirs 保护
- `src/fspack/packaging/pyc.py`：`_strip_py_sources`/`_strip_compiled_py` 新增
  `web_static_dirs` 参数，保护目录内 `.py` 不剥离
- `src/fspack/runner.py`：`AppType.WEB` 加入输出吞掉提示条件
- `src/fspack/cli_parser.py`：新增 `--open-browser` CLI 标志
- `src/fspack/cli.py`：`_run_build` 合并 `open_browser`（CLI or 配置）
- `src/fspack/templates/registry.py`：新增 `_WEB_FLASK_VUE_ENTRY`/
  `_WEB_FASTAPI_REACT_ENTRY`/`_WEB_FLASK_VUE_PYPROJECT`/
  `_WEB_FASTAPI_REACT_PYPROJECT`/`_WEB_VUE_INDEX`/`_WEB_REACT_INDEX`，
  注册 `web-flask-vue` 与 `web-fastapi-react` 模板（app_type="web"）

### 测试（7 文件，+28 测试）

- `tests/test_config.py` (+13)：AppType.WEB 枚举、infer_app_type flask/fastapi/uvicorn、
  GUI 优先于 WEB、declared 回退、web-static-dirs 解析、open_browser 配置/合并、
  default_entry WEB 优先于 CLI
- `tests/test_entry.py` (+3)：generate_wrapper_source 注入 _WEB_STATIC_DIRS/_OPEN_BROWSER、
  Flask/FastAPI monkey-patch 代码块、空 web_static_dirs 不注入
- `tests/test_loader.py` (+1)：_build_command 对 AppType.WEB 加 -mwindows
- `tests/test_builder.py` (+2)：copy_source 保护 web_static_dirs 元数据、
  _strip_py_sources 跳过 web_static_dirs 的 .py
- `tests/test_init_templates.py` (+7)：模板注册/元数据/文件列表/pyproject 配置/入口语法
- `tests/test_cli.py` (+2)：--open-browser 标志存在、CLI 解析为 BuildOptions
- `tests/test_runner.py` (+1)：WEB 类型非零退出码提示 --debug

### 文档

- `.trae/req/req-51-前后端分离web打包.md`：需求清单（已标记完成，移动到 done/）
- `.trae/docs/iter-148-前后端分离web打包.md`：本迭代记录

## 关键决策与依据

### 1. AppType.WEB 与 GUI 同等关闭控制台

WEB 类型（Flask/FastAPI 前后端分离）作为桌面应用分发时，黑色控制台窗口对
终端用户不友好。`_build_command` 对 `AppType.WEB` 与 `AppType.GUI` 一样加
`-mwindows`（Windows subsystem），`runner.py` 对 WEB 类型同样提示"输出被吞"。

### 2. infer_app_type 优先级：GUI > WEB > CLI

`_GUI_HINTS` 优先于 `_WEB_HINTS` 检查：matplotlib 等可视化库偶尔与 web 框架
共存，按 GUI 处理关闭控制台更合理。`_WEB_HINTS` 含 flask/fastapi/sanic/django/
tornado/starlette/uvicorn/hypercorn/quart，任一 import 即判定 WEB。

### 3. default_entry 排序键：GUI > WEB > CLI

`ProjectInfo.default_entry` 排序键从 `(0 if GUI else 1, name)` 改为
`(0 if GUI else 1 if WEB else 2, name)`，WEB 优先于 CLI。多入口混合项目
（如 CLI + WEB）未指定 `--entry` 时优先运行 WEB 入口。

### 4. web-static-dirs 与 data-dirs 同等保护

`copy_source` 的 `_build_ignore_fn` 合并 `data_dirs` + `web_static_dirs`
为一个保护目录集合，目录树内跳过 `_EXCLUDE_METADATA`（pyproject.toml/*.md
等），仅应用 `_EXCLUDE_ALWAYS`（构建产物/缓存/IDE）。`_strip_py_sources`
同理保护目录内 `.py` 不剥离。前端构建产物目录（如 `dist/`）需完整保留。

### 5. wrapper monkey-patch Flask.run / uvicorn.run

在 import 用户代码前 monkey-patch `flask.Flask.run` 与 `uvicorn.run`：
- Flask：`app.run()` 时挂载 `@app.route("/")` 返回 index.html、
  `@app.route("/<path:path>")` 返回静态文件
- FastAPI：`uvicorn.run(app, ...)` 时 `app.mount("/", StaticFiles(directory=..., html=True))`
- 两者都启动 `threading.Timer(delay=1.0)` 调 `webbrowser.open`，延迟 1 秒
  等服务器监听端口就绪

`web_static_dirs` 在打包时由 stages.py 解析为 dist 内绝对路径，wrapper 直接
使用，不依赖工作目录。非 WEB 类型或未配置 `web_static_dirs` 时此块无操作。

### 6. open_browser 默认启用 WEB 类型

`stages.py._build_one_loader` 中 `open_browser = ctx.opts.open_browser or
ep.app_type is AppType.WEB`：WEB 类型自动启用，CLI/配置可显式覆盖。
`cli.py` 合并策略 `ns.open_browser or base.open_browser`（CLI 或配置任一启用）。

### 7. 模板设计：入口仅定义 API 路由

`web-flask-vue` 与 `web-fastapi-react` 模板入口仅定义 `/api/hello` API 路由，
静态文件 serve 由 wrapper monkey-patch 注入。模板含 `dist/index.html`
（Vue 3 / React CDN），pyproject.toml 含 `[tool.fspack] web-static-dirs = ["dist"]`。
入口仍含 `app.run()`/`uvicorn.run()` 供非打包开发使用，打包时由 wrapper 覆盖。

## 代码实现情况

- `AppType.WEB` 枚举值 `"web"`，与 CLI/GUI 并列
- `_WEB_HINTS` frozenset 含 9 个框架/服务器导入名
- `infer_app_type` 三段式：先查 GUI hints → 再查 WEB hints → declared 回退
- `_parse_web_static_dirs` 复用 `_parse_string_list_cfg(reject_empty=True)`
- `EntryWrapper._WRAPPER_TEMPLATE` 新增 web 注入块（lines 167-261），条件
  `if _WEB_STATIC_DIRS and _OPEN_BROWSER` 控制是否注入
- `_build_one_loader` 传 `web_static_dirs=ctx.info.web_static_dirs` 与
  `open_browser=ctx.opts.open_browser or ep.app_type is AppType.WEB`
- `_compile_user_sources` 解析 `web_static_dirs` 为 dist/src 下绝对路径传给
  `_precompile_pyc` → `_strip_compiled_py` → `_strip_py_sources`
- `copy_source` 与 `_build_ignore_fn` 合并 data_dirs + web_static_dirs 保护
- `_build_command` 条件 `app_type in (AppType.GUI, AppType.WEB)` 加 `-mwindows`

## 整合优化情况

- `web_static_dirs` 与 `data_dirs` 在 `copy_source`/`_strip_py_sources` 中
  合并为单一保护目录集合，避免重复判断逻辑
- `_WEB_HINTS` 与 `_GUI_HINTS` 风格一致，`infer_app_type` 三段式清晰
- 模板源码复用现有 `_pyproject` 风格但独立定义（含 `[tool.fspack]` 配置）
- `open_browser` 复用 `BuildDefaults`/`BuildOptions`/`_BUILD_DEFAULT_KEYS`
  既有模式，与 `nuitka`/`pyc_strip` 等布尔开关一致

## 测试验证结果

- ruff check：All checks passed
- pyrefly check：0 errors
- pytest：2185 passed, 12 skipped, coverage 95.64%
- 新增 28 个测试覆盖：AppType.WEB 枚举/infer_app_type/web_static_dirs 解析/
  open_browser 配置合并/generate_wrapper_source 注入/_build_command -mwindows/
  copy_source 保护/_strip_py_sources 保护/模板注册/CLI --open-browser/runner 提示

## 遗留事项

无。所有需求清单项已完成，门禁全通过。

## 下一轮计划

iter-148 完成 req-51 全部需求。后续可考虑：
- 实际端到端打包验证（`fsp init -t web-flask-vue myapp && fsp b`）
- 前端构建工具集成（Vite/vue-cli）的 dist 目录自动检测
- 多静态目录支持（web_static_dirs 多目录场景的 Flask blueprint 挂载）
