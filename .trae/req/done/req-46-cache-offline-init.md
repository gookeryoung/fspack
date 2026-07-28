# 需求：缓存环境变量配置 + 离线环境支持 + init 新建项目命令

## 背景

fspack 打包时缓存路径分散硬编码在多个文件中，缺乏环境变量配置；离线环境
（内网 CI、离线打包机）下因无法联网导致打包失败，错误信息不清晰。同时缺少
新建项目脚手架命令，新用户上手成本高，需手动创建 pyproject.toml、入口脚本、
目录结构等。

## 需求

### 一、缓存环境变量配置（已完成，iter-76）

- [x] 1. 统一缓存根目录管理：`FSPACK_CACHE_DIR` 环境变量覆盖默认 `~/.fspack/cache`
- [x] 2. 离线模式开关：`FSPACK_OFFLINE` 环境变量（`1`/`true`/`yes`/`on` 启用）
- [x] 3. 各子模块缓存目录派生：embed/standalone/wheels/nuitka/loaders/ccache/tkinter
- [x] 4. 替换 pipeline.py/loader_compile.py/nuitka_env.py 中硬编码路径为统一函数
- [x] 5. 单元测试覆盖：环境变量覆盖、子目录派生、离线模式解析

### 二、离线环境支持（iter-77 ~ iter-79）

- [x] 6. 下载层离线模式：runtime/wheel/ccache/tkinter 下载缓存未命中时报清晰错误
      不卡死、不尝试网络请求
- [x] 7. 离线 wheel 本地搜索增强：`--find-links` 优先 + `pip --no-index` 离线参数
      确保离线模式下从本地 wheel 目录解析依赖
- [x] 8. 离线打包集成测试：模拟离线环境（mock `is_offline()` 返回 True）验证
      缓存命中正常、缓存未命中报清晰错误
- [x] 9. 文档完善：README 新增"离线打包"章节，docs/ 新增离线模式说明

### 三、init 新建项目命令（iter-80 ~ iter-85）

- [x] 10. `fsp init` 命令骨架：交互式询问项目名/类型/模板，生成项目结构
- [x] 11. 模板引擎：基于 `string.Template` 渲染，支持变量替换与文件树生成
- [x] 12. 模板清单与选择界面：`fsp init --list` 列出所有模板，`--template` 指定
- [x] 13. 不少于 20 项典型项目模板，覆盖 CLI/GUI/游戏/科学/Web 等场景

## 验收标准

### 离线环境支持

- 缓存命中时离线模式正常构建，无网络请求
- 缓存未命中时抛出明确异常（包含 "离线模式" + 缓存路径 + 缺失文件名）
- 离线模式下不卡死、不重试网络、不超时等待
- 现有测试全部通过，覆盖率不低于 95%

### init 命令

- `fsp init` 交互式询问项目名与模板，生成可构建的项目骨架
- `fsp init --list` 列出所有模板（不少于 20 项）
- `fsp init --template helloworld myapp` 非交互式生成指定模板项目
- 生成的项目可被 `fsp build` 成功构建
- 模板覆盖：CLI（helloworld/args/rich/requests/click/typer）、
  GUI（pyside2/pyside6/pyside2-qml/pyside6-qml/pyqt5/tkinter）、
  游戏/科学/Web（pygame/snake/matplotlib/numpy/scipy/flask/fastapi/pyinstaller）、
  多入口/完整配置（multi-entry/full-config）

## 关键决策

- **环境变量优先级**：`FSPACK_CACHE_DIR` > 默认 `~/.fspack/cache`，
  `FSPACK_OFFLINE` 不区分大小写接受 `1`/`true`/`yes`/`on`
- **离线模式 fail-fast**：检测到 `is_offline()` 为 True 且缓存未命中时立即抛出
  包含 "离线模式" 关键字的异常，避免尝试网络请求导致超时卡死
- **wheel 离线解析**：`_run_pip_download` 已有 `--no-index` 优先策略，离线模式下
  当 `--no-index` 失败时直接报错而非回退到在线下载
- **模板引擎选型**：`string.Template` 而非 Jinja2，避免引入新依赖；模板文件以
  `.tpl` 后缀存放于 `src/fspack/templates/<name>/`，渲染时剥离 `.tpl` 后缀
- **模板交互**：rich 库实现交互式选择（已有依赖），不引入新依赖
