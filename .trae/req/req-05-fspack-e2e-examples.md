# fspack P5 需求清单

用户已安装 mingw-w64 + wine，要求制定至少 5 个典型项目示例验证打包效果。覆盖无库、有库 CLI、有库 GUI、有库 pygame、有库 web 五类场景。

## P5 范围

[x] 1. helloworld 示例端到端（已有）：无库 CLI，wine 运行输出 "hello, world"。
[x] 2. clitool 示例：有库 CLI（requests），wine 运行打印 requests 版本号。
[x] 3. guicalc 示例：有库 GUI（PySide6），wine 下 offscreen 模式验证 import 与 QApplication 创建。
[x] 4. pygamedemo 示例：有库 pygame，wine 下 pygame.init() + 打印版本。
[x] 5. webapp 示例：有库 web（flask），wine 下用 test_client 验证路由响应。
[x] 6. 为每个示例写 slow 端到端测试（build + wine 运行 + 断言输出）。
[x] 7. 真实运行 `pytest -m slow` 验证全部示例打包与运行效果，记录结果。

## 不在 P5 范围

- Linux 平台端到端（python-build-standalone 真实下载，待 P5 之后单独验证）。
- NSIS 安装包端到端（待 makensis 安装）。

## 验收标准

- 5 个示例项目在 `tests/examples/` 下，各有 pyproject.toml + 入口 .py。
- `pytest -m slow` 全部通过（允许 GUI/pygame 在 wine 下因显示驱动缺失跳过，但需记录）。
- 每个示例验证：dist/<name>.exe 存在、runtime/python311.dll 存在、wheel 解包到 site-packages、wine 运行输出预期内容。

## 约束

- 示例代码简洁，聚焦验证打包链路而非业务逻辑。
- 有库项目 pyproject.toml 必须声明 dependencies（验证 AST 依赖分析 + wheel 下载）。
- GUI 项目 wine 运行需设 QT_QPA_PLATFORM=offscreen 避免显示依赖。
- pygame 需设 SDL 驱动为 dummy 避免音频/显示依赖。
