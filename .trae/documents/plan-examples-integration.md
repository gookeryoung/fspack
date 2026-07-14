# examples 目录整合计划

## 目标

将 `tests/examples/`（8 个测试夹具示例）整合到 `examples/`（4 个展示示例），统一目录命名规则为 `<类型>_<名称>` 下划线风格，消除两个目录的用途重叠与命名不一致。

## 现状分析

### `examples/`（展示 + workspace member）

| 目录 | pyproject name | 类型 | 依赖 | 特殊文件 | 处置 |
|------|---------------|------|------|---------|------|
| cli_complex | cli-complex | CLI 多模块 | 无 | modules/ | 保留不改 |
| cli_office | cli-office | CLI | pypdf | workspace member | 保留不改 |
| pygame_snake | pygame-snake | pygame 展示 | pygame | assets/ + README + __init__.py | **删除**（与 snake 重叠，保留更简单的 snake） |
| tk_basic | tk-basic | GUI Tkinter | 无 | — | 保留不改 |

顶层有空的 `__init__.py`（保留不动）。`pygame_snake/dist/` 有残留构建产物（随删除一并清除）。

### `tests/examples/`（测试夹具，全部移到 examples/ 并重命名）

| 旧目录 | 旧 name | 新目录 | 新 name | 依赖 | 特殊文件 | dist 残留 |
|--------|---------|--------|---------|------|---------|----------|
| helloworld | helloworld | cli_helloworld | cli_helloworld | 无 | — | 无 |
| clitool | clitool | cli_tool | cli_tool | requests | — | 有，需清理 |
| guicalc | guicalc | gui_calc | gui_calc | PySide6 | — | 有，需清理 |
| pygamedemo | pygamedemo | pygame_demo | pygame_demo | pygame | — | 无 |
| pyqt5app | pyqt5app | pyqt5_app | pyqt5_app | PyQt5 | — | 无 |
| pyside2app | pyside2app | pyside2_app | pyside2_app | PySide2 | .python-version(3.9) + README.md | 无 |
| snake | snake | pygame_snake | pygame_snake | pygame | — | 有，需清理 |
| webapp | webapp | web_app | web_app | flask | — | 无 |

### 命名规则说明

- **目录名**：`<类型>_<名称>`，下划线分隔，参考现有 `examples/` 的 `cli_complex`/`tk_basic`。
- **name 字段**：`= 目录名`（下划线）。理由：`exe_name = f"{name}.exe"`，测试用 `f"{proj_name}.exe"`（proj_name=目录名）定位 exe，两者必须一致才能最小化测试改动。现有展示示例（cli-complex 等）用连字符是历史遗留，不在本次范围内统一。
- **snake → pygame_snake**：删除展示版 pygame_snake 后该名称释放，测试版 snake 复用之。结果：`pygame_demo`（init 打印版本）+ `pygame_snake`（完整贪吃蛇游戏）共存，命名一致。先前讨论曾考虑 `pygame_snake_test`，但 `_test` 后缀破坏命名一致性，故改用 `pygame_snake`。
- **入口 .py 文件名**：保留不变（如 helloworld.py 不重命名）。`detect_entry` 兜底扫描 `*.py` 找到含 `main()` 的文件即可，无需 name=文件名。这样最小化改动，`.entry` 内容与 `entry_file.name` 断言均不变。

## 关键技术约束（来自探索）

1. `exe_name = f"{self.name}.exe"`（config.py L56-58），name 来自 pyproject.toml。
2. `test_e2e_slow.py` 用 `f"{proj_name}.exe"` 定位 exe，proj_name 即目录名 → name 字段必须 = 目录名。
3. `detect_entry`（project.py L158-185）：先匹配 `<name>.py`，再 `<name>/__main__.py`，兜底扫描顶层 `*.py`。name≠文件名时走兜底，单文件示例无影响。
4. `_EXAMPLES = Path(__file__).parent / "examples"` 出现在 3 个测试文件，指向 `tests/examples/`，需改为项目根 `examples/`（`Path(__file__).parent.parent / "examples"`）。
5. `pyproject.toml` L128 pyrefly 排除 `tests/examples/**`，整合后该路径不存在，需删除该条（`examples/**` 已在排除列表）。
6. installer 测试用 `<name>-setup.exe`（NSIS）/ `<name>_<ver>_amd64.deb`（dpkg）/ `<name>-<ver>-linux.tar.gz`，均来自 name 字段，需同步更新。
7. `uv.lock` L236 `source = { virtual = "examples/cli_office" }` 引用路径不变（cli_office 不动）。

## 实施步骤

### 步骤 1：清理残留构建产物

删除以下 dist/ 目录（gitignored，本地残留）：
- `tests/examples/clitool/dist/`
- `tests/examples/guicalc/dist/`
- `tests/examples/snake/dist/`
- `examples/pygame_snake/dist/`（随步骤 2 整体删除）

### 步骤 2：删除展示版 pygame_snake

删除整个 `examples/pygame_snake/` 目录（含 snake.py、assets/、README.md、__init__.py、dist/）。

### 步骤 3：移动 + 重命名 8 个测试夹具示例

对 `tests/examples/` 下每个示例，移动到 `examples/<新目录名>/`，并更新其 `pyproject.toml` 的 name 字段为新目录名：

| 操作 | 旧路径 | 新路径 | name 字段 |
|------|--------|--------|----------|
| 移动+改名 | tests/examples/helloworld/ | examples/cli_helloworld/ | cli_helloworld |
| 移动+改名 | tests/examples/clitool/ | examples/cli_tool/ | cli_tool |
| 移动+改名 | tests/examples/guicalc/ | examples/gui_calc/ | gui_calc |
| 移动+改名 | tests/examples/pygamedemo/ | examples/pygame_demo/ | pygame_demo |
| 移动+改名 | tests/examples/pyqt5app/ | examples/pyqt5_app/ | pyqt5_app |
| 移动+改名 | tests/examples/pyside2app/ | examples/pyside2_app/ | pyside2_app |
| 移动+改名 | tests/examples/snake/ | examples/pygame_snake/ | pygame_snake |
| 移动+改名 | tests/examples/webapp/ | examples/web_app/ | web_app |

每个示例仅移动 `pyproject.toml` + 入口 `.py` + 特殊文件（pyside2_app 的 .python-version/README.md），**不移动 dist/**。入口 `.py` 文件名保持不变。

### 步骤 4：删除 tests/examples/ 空目录

移动完成后删除 `tests/examples/` 整个目录。

### 步骤 5：更新测试文件

#### `tests/test_project.py`
- L13：`_EXAMPLES = Path(__file__).parent / "examples"` → `Path(__file__).parent.parent / "examples"`
- L17：`_EXAMPLES / "helloworld"` → `_EXAMPLES / "cli_helloworld"`
- L18：`info.name == "helloworld"` → `"cli_helloworld"`
- L22：`info.exe_name == "helloworld.exe"` → `"cli_helloworld.exe"`
- L19-20：entry_module/entry_file 断言不变（helloworld.py 未改名）
- L30/L37/L38：`_EXAMPLES / "pyside2app"` → `_EXAMPLES / "pyside2_app"`

#### `tests/test_builder.py`
- L33：`_EXAMPLES = Path(__file__).parent / "examples"` → `Path(__file__).parent.parent / "examples"`
- L798/L799：`"helloworld"` → `"cli_helloworld"`（proj 目录名 + copytree 源）
- L829：`info.name == "helloworld"` → `"cli_helloworld"`
- L830：`"helloworld.exe"` → `"cli_helloworld.exe"`
- L832/L835：`src/helloworld.py` 断言不变（入口文件未改名）
- L931/L932：同 L798/L799
- L959：`info.name == "helloworld"` → `"cli_helloworld"`
- L960：`"helloworld"`（Linux exe）→ `"cli_helloworld"`
- L961：`"helloworld.exe"` → `"cli_helloworld.exe"`
- L963：`src/helloworld.py` 不变

#### `tests/test_e2e_slow.py`
- L17：`_EXAMPLES = Path(__file__).parent / "examples"` → `Path(__file__).parent.parent / "examples"`
- L29：注释 `tests/examples 下的示例目录名` → `examples/ 下的示例目录名`
- 所有 `_build_and_run("<旧名>", ...)` 调用的第一个参数改为新目录名：
  - L63 helloworld→cli_helloworld
  - L69 clitool→cli_tool
  - L116 pygamedemo→pygame_demo
  - L128 webapp→web_app
  - L205 snake→pygame_snake
- 内联测试（guicalc/pyside2app/pyqt5app）中 `proj = tmp_path / "<旧名>"` + `copytree(_EXAMPLES / "<旧名>", ...)` + exe 名同步更新：
  - L92/L93/L98：guicalc→gui_calc
  - L150/L151/L154：pyside2app→pyside2_app
  - L184/L185/L188：pyqt5app→pyqt5_app
- 各测试中 `proj = tmp_path / "<旧名>"` 的硬编码（L71/L121/L129/L211/L230/L254/L283/L319 等）同步改为新名
- Linux 测试 exe 名（无扩展名）：L234/L258 helloworld→cli_helloworld / clitool→cli_tool
- installer 测试产出文件名（来自 name 字段）：
  - L287：`helloworld-setup.exe`→`cli_helloworld-setup.exe`
  - L298：`Name "helloworld 0.1.0"`→`Name "cli_helloworld 0.1.0"`
  - L299：`OutFile "release\\helloworld-setup.exe"`→`cli_helloworld-setup.exe`
  - L323：`helloworld_0.1.0_amd64.deb`→`cli_helloworld_0.1.0_amd64.deb`
  - L328：`helloworld-0.1.0-linux.tar.gz`→`cli_helloworld-0.1.0-linux.tar.gz`
- 运行输出断言字符串不变（如 "hello from PySide2"、"requests "、"snake ready"）

### 步骤 6：更新配置文件

#### `pyproject.toml`
- L128：`project-excludes = [".venv/**", "template/**", "examples/**", "tests/examples/**"]` → 删除 `"tests/examples/**"`（该路径已不存在，`examples/**` 已覆盖）
- L57 workspace member `examples/cli_office` 不变
- L92 ruff `extend-exclude = ["examples", "template"]` 不变
- L136 bumpversion `exclude = ["template/*", "examples/*"]` 不变

### 步骤 7：更新 README.md

- L138：`tests/examples/ 下提供 5 类典型项目` → `examples/ 下提供多类典型项目`
- L142-146：示例表格更新为新名 + 补全缺失示例（pyqt5_app/pyside2_app/pygame_snake 当前未列）：

| 示例 | 类型 | 说明 |
|------|------|------|
| cli_helloworld | 无库 CLI | 最小示例，验证基础流水线 |
| cli_tool | 有库 CLI | requests 依赖，验证 wheel 下载与解包 |
| cli_complex | 无库 CLI 多模块 | 多模块项目结构 |
| cli_office | 有库 CLI | pypdf 依赖，workspace member |
| gui_calc | 有库 GUI | PySide6 依赖，验证 GUI 快捷方式与 DLL 搜索 |
| pyside2_app | 有库 GUI | PySide2 + 版本自动解析（3.9.13） |
| pyqt5_app | 有库 GUI | PyQt5 + Python 3.12 |
| tk_basic | 无库 GUI | Tkinter 标准库 GUI |
| pygame_demo | 有库 pygame | pygame init 验证 |
| pygame_snake | 有库 pygame | 完整贪吃蛇游戏，dummy 驱动测试 |
| web_app | 有库 web | flask 依赖，验证 web 框架打包 |

## 假设与决策

1. **name 字段 = 目录名（下划线）**：保证 `exe_name = 目录名.exe`，测试 `f"{proj_name}.exe"` 逻辑不变，仅字符串替换。现有展示示例（cli-complex 连字符）不在本次统一范围。
2. **入口 .py 文件不重命名**：保留 helloworld.py 等原名，`detect_entry` 兜底扫描处理。最小化改动，`.entry` 内容与 entry_file 断言不变。
3. **snake → pygame_snake**（非 pygame_snake_test）：复用删除后释放的名称，与 pygame_demo 命名一致，无 `_test` 后缀。
4. **不修改展示示例**（cli_complex/cli_office/tk_basic）：其 name 字段、目录名、文件均不变，避免影响 workspace member 与 uv.lock。
5. **历史文档不更新**：`.trae/docs/`、`.trae/documents/`、`.trae/skills/`、`.trae/req/` 中对旧示例名的引用为历史记录，不修改（rule-01 每 5 次迭代归档清理）。

## 验证

1. `uv run ruff check src tests` 通过
2. `uv run ruff format --check src tests` 通过
3. `uv run pyrefly check` 通过（确认 tests/examples/** 排除项删除后无新错误）
4. `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95` 通过（覆盖率不降）
5. 手动构建一个示例确认流水线正常：`F:\Dev\fspack\.venv\Scripts\fspack.exe b examples/cli_helloworld`
6. 确认 `examples/` 下无 `dist/` 残留、`tests/examples/` 目录已删除

## 不在范围内

- 统一展示示例 name 字段为下划线（cli-complex→cli_complex 等）
- 重命名入口 .py 文件
- 更新历史文档（.trae/docs/ 等）中的旧示例名引用
- slow 端到端测试的实际运行（需 mingw/wine/gcc，本地环境未必具备，仅保证代码正确）
