# 模板与测试示例工程的冗余盘点与整合方案

- 日期：2026-11-13
- 范围：`src/fspack/templates/`、`src/fspack/assets/init_templates/`、`src/fspack/assets/templates/`、`tests/`
- 性质：只读分析。本文不做任何代码或既有文档结构调整，仅给出事实、对比与迁移路径。

## 0. 结论摘要

**推荐「方案 1 + 方案 3」的混合形态：**

1. `init` 模板保持唯一事实源，测试侧不再手抄模板内容，改为「模板 → 生成 → 断言」；
2. 新增一层**声明式场景清单 + fixture 工厂**（`tests/_projects.py` 或 `tests/scenarios.toml`），统一生成测试所需的最小/中等工程，替代 29 个测试文件里的 233 处内联 `pyproject.toml`；
3. `doctor` 富示例模板**不并入** init 模板：它是产品功能（`fsp doctor` 的样例）+ 真实工程集成 fixture，保留独立目录，只收敛加载接口（退役 `ProjectTemplate` 兼容 shim）。

**为什么不是「测试作为唯一源」**：模板是用户可见契约（文件名、描述、分类、向导顺序、生成内容），测试不承载这些 UX 语义；反向抽取会把测试的实现细节固化成产品行为，且 8 个 doctor 模板共 4 465 行，迁移成本最高、第三方兼容性最差。

**工作量（按文件与断言规模，不按时间估算）**：

| 阶段 | 改动面 | 规模 |
|---|---|---|
| Phase 0 防漂移护栏 | 新增 1 个测试文件 | ~150 行断言，0 业务改动 |
| Phase 1 夹具工厂 | 新增 1 个 helper，迁移 5 个测试文件 | ~120 行 helper + ~50 处调用点 |
| Phase 2 版本口径归一 | 18 个 init 模板 + 2 个常量 + 4 处测试断言 | 24 个文件，纯文本 |
| Phase 3 退役 shim | 删除 `project_template.py`（95 行），改 4 个测试文件 + 1 个 doctor 模块 | ~110 行 |

**风险：中低。** 最大风险不是迁移本身，而是「测试从模板派生后失去独立 oracle」——如果断言也由模板生成，模板写错时测试会跟着错。缓解方式：参数化列表从注册表派生（防漏测），但关键契约（依赖、app_type、requires-python）保留一份人工评审的显式期望表。

## 1. 盘点口径

全部结论来自只读检索与对比，未运行会写盘的命令：

- 模板清单：递归读取 `assets/{init_templates,templates}/**/pyproject.toml` 与 `template.toml`，解析 `requires-python`/`dependencies`/`scripts`/`[tool.fspack]`；
- 测试清单：`tests/**/pyproject.toml` 静态文件数为 0；以 `pyproject.toml` 写入点统计程序化构造规模；
- 引用关系：`ProjectTemplate.root_dir()`、`shutil.copytree`、`list_templates`、`init_project` 的调用点；
- 行号均对应当前工作区 main（提交 `02c7fde`）。

## 2. 模板侧盘点

### 2.1 总量

| 角色 | 目录 | 模板数 | 文件数 | 总行数 | 元数据来源 |
|---|---|---:|---:|---:|---|
| `init`（`fsp init`） | `assets/init_templates/<category>/<name>/` | 18 | 60（含 18 个 `template.toml`） | 736 | `template.toml` + `pyproject.toml` |
| `doctor`（`fsp doctor` 样例） | `assets/templates/<category>/<name>/` | 8 | 76 | 4 465 | 仅 `pyproject.toml` |
| 合计 | — | **26** | **136** | **5 201** | — |

分类固定为 `cli/gui/game/sci/web/config`（[registry.py:36](../../src/fspack/templates/registry.py#L36)）；`init` 默认角色为 `{"init"}`（[registry.py:40](../../src/fspack/templates/registry.py#L40)）。

### 2.2 模板字段

`template.toml`（仅 init）字段：`id`、`name`、`description`、`category`、`app_type`、`dependencies`，可选 `py_version`、`roles`、`extra_config`。

统一后的 `Template` dataclass 另有：`files`（init 为占位符源文件，doctor 为空元组）、`dir`、`version`、`requires_python`（默认 `">=3.13"`，[registry.py:87-102](../../src/fspack/templates/registry.py#L87-L102)）、`roles`。

生成文件命名约定：init 模板统一包含 `template.toml` + `pyproject.toml` + `$entry_module.py`，渲染时 `$project_name`/`$description`/`$entry_module` 由 [engine.py:32-60](../../src/fspack/templates/engine.py#L32-L60) 注入；doctor 模板无占位符，直接 `copytree`。

### 2.3 init 模板（18 个）

| 分类/ID | 文件（除公共三件套外的差异） | requires-python | dependencies | scripts | 性质 |
|---|---|---|---|---|---|
| cli/helloworld | — | `>=3.8,<3.12` | — | — | 纯脚手架 |
| cli/requests | — | `>=3.8,<3.12` | requests | — | 纯脚手架 |
| cli/rich | — | `>=3.8,<3.12` | rich | — | 纯脚手架 |
| cli/typer | — | `>=3.8,<3.12` | typer | — | 纯脚手架 |
| config/full-config | `README.md`、`.gitignore`、`tests/` | `>=3.8,<3.12` | — | — | 承载默认配置（`[tool.fspack]` 4 项） |
| config/multi-entry | `.python-version` + `cli.py`/`gui.py` | `>=3.8,<3.12` | — | `cli`、`gui` | 入口点约定（多入口） |
| config/pyinstaller | — | `>=3.8,<3.12` | — | — | 承载默认配置（`[tool.fspack]` 8 项） |
| game/snake | — | `>=3.8,<3.12` | pygame | — | 纯脚手架 |
| gui/pyside2 | — | `>=3.8,<3.10` | PySide2 | — | 纯脚手架 |
| gui/pyside6 | — | `>=3.8,<3.12` | PySide6 | — | 纯脚手架 |
| gui/pyside6_qml | `main.qml` | `>=3.8,<3.12` | PySide6 | — | 资源型脚手架 |
| gui/tkinter | — | `>=3.8,<3.12` | — | — | 纯脚手架 |
| sci/matplotlib | — | `>=3.8,<3.12` | matplotlib | — | 纯脚手架 |
| sci/numpy | — | `>=3.8,<3.12` | numpy | — | 纯脚手架 |
| sci/scipy | — | `>=3.8,<3.12` | numpy、scipy | — | 纯脚手架 |
| web/fastapi | — | `>=3.8,<3.12` | fastapi、uvicorn | — | 纯脚手架 |
| web/flask | — | `>=3.8,<3.12` | flask | — | 纯脚手架 |
| web/web-fastapi-react | `frontend/` | `>=3.8,<3.12` | fastapi、uvicorn | — | 承载默认配置（`web-static-dirs`）+ 前端资源 |

统计：**13 个纯脚手架**（入口 + pyproject + 元数据），5 个承载额外约定（配置 3、多入口 1、资源/前端 2，其中 `web-fastapi-react` 同时属于两类）。

入口约定现状：**17/18 个 init 模板未声明 `[project.scripts]`**，依赖 `$entry_module.py` 文件名兜底（`detect_entry`）；只有 `config/multi-entry` 演示 `[project.scripts]`。这意味着模板层对「包级入口、多入口、GUI/WEB 混合」的覆盖只集中在 1 个模板上。

### 2.4 doctor 模板（8 个）

| 分类/ID | 文件数 | requires-python | dependencies | scripts | 用途 |
|---|---:|---|---|---|---|
| cli/cli_complex | 14 | `==3.14.*` | lxml、ordered-set | — | 多文件 CLI、复杂解析 |
| config/multi_entry | 5 | `>=3.8,<3.11` | PySide2>=5.15.2、flask | `cli`、`gui`、`web` | 三入口（含 web） |
| game/pygame_snake | 3 | `>=3.13` | pygame | — | 最小 pygame 样例 |
| game/pygame_tetris | 10 | `>=3.8,<3.13` | pygame>=2.5.0 | — | 带 `[tool.fspack]`（`no_site`/`pyc_strip`） |
| gui/pyside2_qml_dashboard | 10 | `>=3.8,<3.11` | pyside2>=5.15.2.1 | — | QML dashboard |
| gui/tk_app | 2 | 未声明 | — | — | 最小 tkinter（测试主力 fixture） |
| sci/sci_stack | 3 | `>=3.8` | matplotlib、numexpr、numpy、scipy | — | 科学计算综合 |
| web/webview_app | 29 | `>=3.10,<3.14` | cffi、cryptography、pythonnet、pywebview[ssl] | `webview_app` | 真实 webview 工程 |

doctor 模板的定位是「可复制运行的富示例」，不是最小脚手架：单模板 2–29 个文件，行数占模板总量的 86%（4 465 / 5 201）。

## 3. 测试侧盘点

### 3.1 数量与形态

| 形态 | 数量 | 证据 |
|---|---:|---|
| `tests/` 下静态示例工程目录（含 `pyproject.toml`） | **0** | 递归查找 `tests/**/pyproject.toml` 为 0 |
| 程序化内联 `pyproject.toml` 写入点 | **233** 处 / 29 个文件 | `pyproject.toml").write_text` / `pyproject.write_text` 计数 |
| 含 `[tool.fspack]` 的测试构造 | 169 处 / 14 个文件 | `tool.fspack` 计数 |
| 复用 doctor 模板作为 fixture | **4 个模板** / 3 个测试文件 | `ProjectTemplate.root_dir()` + `copytree` |
| 由 init 模板生成后断言 | **18 个**（smoke）+ **11 个**（e2e 解析，去重后） | `test_init_smoke.py`、`test_init_e2e.py` |
| 手写完整多文件工程构造器 | **1 个**（`_write_project`，4 处使用） | [test_e2e_slow.py:97](../../tests/test_e2e_slow.py#L97) |

结论：测试侧**没有一份独立的静态示例工程**，全部是「程序化最小 pyproject」或「复用模板」。所谓「测试样例」并不是一个可维护的事实源，而是散落在 29 个文件里的构造逻辑。

### 3.2 主要测试文件与覆盖场景

| 文件 | 场景 | 与模板的关系 |
|---|---|---|
| `test_init_smoke.py` | 18 个 init 模板逐个 `init_project` → `build(dry_run=True)` | 模板派生（参数化来自 `list_templates`） |
| `test_init_e2e.py` | 11 个模板 init 后解析 `ProjectInfo`/`build_defaults` | 模板派生，但模板 id 手写 |
| `test_init_templates.py` | 渲染内容断言（依赖、app_type、入口内容） | 模板内容 + 手写期望表 |
| `test_init_list.py` | 模板列表、分类数量、向导顺序 | 手写期望 |
| `test_template_loader.py` | 扫描/加载/异常容错 | 真实 doctor 模板 + `tmp_path` |
| `test_doctor_templates.py` | doctor 模板元数据完整性 | 真实 doctor 模板 |
| `test_config.py` | `ProjectInfo`/`parse_project`/`resolve_py_version` | 直接读 4 个 doctor 模板目录 |
| `test_build_dry_run.py` | dry-run 计划输出 | 复制 `gui/tk_app` 3 次 |
| `test_executor.py` | Windows/Linux 完整编排 | 复制 `gui/tk_app` 2 次 |
| `test_e2e_slow.py` | 真实构建 + 运行产物 | 1 个手写构造器（4 个项目） |
| `test_offline_integration.py` | 离线 build 入口 | 内联 flask 工程 |
| 其余 ~18 个文件 | 单点功能（installer/runtime/runner/sync/profile…） | 1–3 行最小 pyproject |

### 3.3 复用 doctor 模板作为 fixture 的位置

| doctor 模板 | 使用点 | 断言内容 |
|---|---|---|
| `gui/tk_app` | [test_config.py:84](../../tests/test_config.py#L84)、[test_config.py:398](../../tests/test_config.py#L398)、[test_build_dry_run.py:117](../../tests/test_build_dry_run.py#L117) 等 3 处、[test_executor.py:166](../../tests/test_executor.py#L166)、[test_executor.py:458](../../tests/test_executor.py#L458) | 名称/入口/`app_type=GUI`/`python311`；构建产物 `.entry`、wrapper、exe |
| `gui/pyside2_qml_dashboard` | [test_config.py:107-108](../../tests/test_config.py#L107-L108)、[test_config.py:414](../../tests/test_config.py#L414) | `requires_python == ">=3.8,<3.11"`、GUI 类型 |
| `config/multi_entry` | [test_config.py:355](../../tests/test_config.py#L355)、[test_config.py:764](../../tests/test_config.py#L764) | 多入口解析、`.python-version` + `<3.11` 解析到 3.10.11 |
| `game/pygame_snake` | `test_config.py`（pygame 依赖分类） | 依赖分类 |

这 4 个模板同时是 `fsp doctor` 的用户样例，**测试与产品共用同一份文件**：模板内容即为测试输入，任何模板编辑都会直接改变测试断言的前提。

## 4. 冗余映射与已发生的不一致

| # | 同一事实 | 位置 A | 位置 B | 现状 / 不一致 | 证据 |
|---|---|---|---|---|---|
| R1 | 模板依赖 | 各 `template.toml` 的 `dependencies` | 各 `pyproject.toml` 的 `[project].dependencies` | 同一份依赖写两遍；`template.toml` 是注册表读取源，`pyproject.toml` 是生成产物内容 | `config/multi-entry/template.toml` vs 同目录 `pyproject.toml` |
| R2 | 依赖与 app_type 期望 | `template.toml` | `test_init_templates.py` 的 `GUI_TEMPLATE_DEPENDENCIES`、`ITER84_DEPENDENCIES`、`ITER84_APP_TYPES` | 测试手抄第 2/3 份，模板改依赖不会自动同步 | [test_init_templates.py:167-194](../../tests/test_init_templates.py#L167-L194) |
| R3 | Python 版本约束 | 18 个 init 模板均 `>=3.8,<3.12`（pyside2 `<3.10`） | 根 `pyproject.toml` 为 `>=3.8`（CI 覆盖 3.8–3.14） | 生成项目在 3.12+ 上按元数据不可安装，与产品支持矩阵不一致 | 模板 `requires-python`；根 `pyproject.toml` |
| R4 | 默认约束常量 | `cli_init._DEFAULT_TEMPLATE_REQUIRES = ">=3.8,<3.12"` | 各模板实际值 | 「全部模板默认值」硬编码；任一模板调整后详情区显示逻辑静默失效 | [cli_init.py:57](../../src/fspack/cli_init.py#L57) |
| R5 | 注册表默认约束 | `Template.requires_python = ">=3.13"` | 模板实际 `>=3.8,<3.12` | 未声明 `requires-python` 的模板（如 `tk_app`）会拿到 3.13，与 init 模板口径相反 | [registry.py:102](../../src/fspack/templates/registry.py#L102) |
| R6 | tkinter 示例 | `init_templates/gui/tkinter`（`<3.12`，3 文件） | `templates/gui/tk_app`（未声明，2 文件） | 同一场景两份维护；测试把 `tk_app` 的「未声明」写死为断言（`parse_project` 用默认版本） | [test_config.py:396-398](../../tests/test_config.py#L396-L398) |
| R7 | snake 示例 | `init_templates/game/snake`（`<3.12`） | `templates/game/pygame_snake`（`>=3.13`） | **同一玩法、约束互相矛盾**（一个不允许 3.12+，一个只允许 3.13+） | 两份 `pyproject.toml` |
| R8 | 多入口示例 | `init_templates/config/multi-entry`（2 脚本、无依赖） | `templates/config/multi_entry`（3 脚本、PySide2 + flask、`<3.11`） | 同一特性两套结构；web 入口只在 doctor 版本出现 | 两份 `pyproject.toml` |
| R9 | PySide2 示例 | `init_templates/gui/pyside2`（`<3.10`） | `templates/gui/pyside2_qml_dashboard`（`<3.11`） | 两个不同上界；测试断言的是 doctor 版本 | [test_config.py:108](../../tests/test_config.py#L108) |
| R10 | 科学计算示例 | `sci/numpy`+`sci/scipy`+`sci/matplotlib` 三个模板 | `templates/sci/sci_stack`（四依赖合集） | 依赖集合重复维护；无一致性校验 | 模板清单 |
| R11 | CLI 示例 | `cli/helloworld`（2 文件） | `cli/cli_complex`（14 文件） | 场景相近、复杂度不同；doctor 版本承担解析压测 | 模板清单 |
| R12 | flask 工程 | 原 `assets/templates/web/web_app`（已删除） | `test_offline_integration.py` 内联复刻 | 模板删除后测试把结构复制进代码，成为唯一维护点 | [test_offline_integration.py:1-12](../../tests/test_offline_integration.py#L1-L12) |
| R13 | doctor 加载逻辑 | `registry._load_doctor_template` / `list_templates` | `ProjectTemplate.list_all/from_id/from_dir` 重新实现扫描 | 兼容 shim 与注册表双路径；shim 明确标注「新代码不要用」 | [project_template.py:1-11](../../src/fspack/templates/project_template.py#L1-L11)、[:43-95](../../src/fspack/templates/project_template.py#L43-L95) |
| R14 | 模板数量 | 注释「当前 24 个」 | 实际 18 个；断言只要求 `>= 18` | 注释过期；断言无法发现模板被误删 | [test_init_smoke.py:25](../../tests/test_init_smoke.py#L25)、[:48](../../tests/test_init_smoke.py#L48) |
| R15 | 测试夹具构造 | 233 处内联 pyproject | 无共享工厂 | 同一最小工程在多个文件重复；`name/version/entry` 约定靠复制 | 统计见 §3.1 |

**已出现的漂移（不是假设）**：

- R3/R7：模板 `game/snake` 声明 `<3.12`，doctor 的 `pygame_snake` 声明 `>=3.13`；同一产品同时存在两套互斥口径，且根包实际支持 3.8–3.14。
- R14：注释仍写 24 个模板，实际已精简为 18（`test_init_templates.py:388-406` 记录了 24→18 的整合历史）。
- R12：模板删除后，测试内联复制了 flask 工程，证明「模板与测试共用结构」在删除时只做了一半迁移。
- R2：依赖期望表是手抄的，模板新增依赖不会触发测试失败；反之模板依赖写错也可能被同样写错的期望表掩盖。

## 5. 三种整合方案

### 方案 1：模板作为唯一事实源，测试「模板 → 生成 → 断言」

**做法**：所有 init 模板由 `list_templates(role="init")` 参数化生成；测试只断言生成结果的结构不变量（入口存在、可解析、`app_type`、依赖可安装），删除手写依赖表；doctor 模板不再作为通用 fixture，只保留目录扫描测试。

- 改动面：测试侧为主；`test_init_templates.py` 的 3 张手写表删除；`test_config/test_build_dry_run/test_executor` 改为先生成模板再断言。
- 迁移成本：中。断言从「内容级」降为「契约级」，需要重写 ~30 条断言。
- e2e/快照影响：`test_init_smoke` 天然兼容；`test_executor` 的 Windows/Linux 编排用例改跑 `gui/tkinter` 生成物即可（tkinter 无第三方依赖）；e2e slow 不受影响。
- 防漂移能力：**强**（结构层面），但**弱**（内容层面）——模板写错时测试跟着错。
- 第三方兼容性：最好。用户拿到的就是被测试的同一份产物。
- 不该合并的边界：doctor 的复杂工程（webview_app、cli_complex、pygame_tetris）不能作为 init 唯一源，否则会把 4 465 行示例塞进 init 矩阵。

### 方案 2：测试作为唯一事实源，模板由测试样例抽取

**做法**：以 `tests/` 中的工程为规范，反向生成/校验 `template.toml` 与模板文件。

- 改动面：迁移 29 个测试文件的 233 处构造，抽取公共样例；模板目录改由工具生成。
- 迁移成本：**最高**。需要为 18 个模板定义「测试样例 → 模板文件」的投影规则，还要保留描述/分类/向导顺序等非结构信息（测试不表达这些）。
- e2e/快照影响：e2e slow 的 `_write_project` 会成为事实源之一，网络依赖场景被固化。
- 防漂移能力：强（回归稳定），但只防「测试覆盖到的部分」；未覆盖模板（如 typer/rich）会失去事实源。
- 第三方兼容性：差。生成项目面向用户，测试夹具面向最小复现，两者目标冲突。
- 适用边界：只适合「以回归稳定性优先、模板仅内部使用」的场景，不适用于对外脚手架。

### 方案 3：引入中间层——声明式工程描述 / fixture 工厂

**做法**：新增 `tests/_projects.py`（或 `tests/scenarios.toml`），用声明式场景描述（`id`、`base_template`、`overrides`、`files`、`build_options`）同时驱动：

- 测试夹具：`scenario.materialize(tmp_path)`；
- 模板校验：断言 `base_template` 渲染结果与场景期望一致；
- 可选：`fsp init --example <scenario>` 复用同一描述（未来能力）。

- 改动面：新增 1 个 helper；迁移 5 个高频测试文件的内联构造；模板本身不改（Phase 0/1）。
- 迁移成本：中低。233 处中大多数是 1–3 行最小 pyproject，按「最小 CLI / 多入口 / GUI / 带构建配置」4 类归并即可覆盖 80%。
- e2e/快照影响：e2e slow 的 `_write_project` 迁入工厂，行为不变；快照（help 输出）不受影响。
- 防漂移能力：**强**，且保留独立 oracle——场景描述是人工评审的第二份声明，不直接复制模板内容。
- 第三方兼容性：好。init 模板仍是用户产物；中间层只服务测试。
- 不该合并的边界：真实历史项目（webview_app 等）保持独立目录，场景描述只引用、不内联。

### 5.4 对比矩阵

| 维度 | 方案 1（模板为源） | 方案 2（测试为源） | 方案 3（中间层） | 推荐混合（1+3） |
|---|---|---|---|---|
| 改动面 | 测试 ~5 文件 | 测试 29 文件 + 模板生成器 | 新增 helper + 5 文件 | helper + 5 文件 + 少量模板常量 |
| 迁移成本 | 中 | 最高 | 中低 | 中低 |
| e2e/快照影响 | 低 | 高（固化网络场景） | 低 | 低 |
| 防漂移能力 | 结构强/内容弱 | 覆盖内强 | 强（独立 oracle） | 强 |
| 第三方兼容性 | 最好 | 差 | 好 | 好 |
| 模板 UX 保全 | 是 | 否（测试语义反向污染） | 是 | 是 |
| 可否分阶段回滚 | 可以 | 困难 | 可以 | 可以 |

## 6. 推荐：模板为源 + 场景中间层，doctor 模板独立

### 6.1 目标形态

```
assets/init_templates/           # 唯一生成事实源（用户可见）
        │  render_template
        ▼
tests/_projects.py               # 场景清单 + fixture 工厂（测试事实源）
        │  materialize(tmp_path)
        ▼
tests/*                          # 只断言契约，不手抄模板内容

assets/templates/                # doctor 产品样例 + 真实工程 fixture（独立）
        │  list_templates(role="doctor")
        ▼
test_template_loader / doctor 测试 / 少量集成测试
```

三条规则：

1. **参数化从注册表派生，契约期望人工维护**：模板 id 列表用 `list_templates` 生成（防漏测），依赖/app_type/requires-python 的关键期望放在场景清单里（防「模板错、测试跟着错」）。
2. **测试只引用 doctor 模板，不复制其内容**：需要最小工程时用场景工厂；需要真实工程时用 `templates/<category>/<id>` 原目录。
3. **版本口径单一**：`requires-python` 只在模板 `pyproject.toml` 出现一次；`cli_init` 的默认值从注册表派生，不再硬编码。

### 6.2 分阶段迁移路径（每阶段可独立验收、可回滚）

#### Phase 0：防漂移护栏（无行为变更）

- 新增 `tests/test_template_consistency.py`：
  - 遍历 `list_templates(role="init")`：断言 `template.toml.dependencies` 与 `pyproject.toml.dependencies` 一致；
  - 断言所有 init 模板的 `requires-python` 一致（白名单允许 pyside2 例外）；
  - 断言模板总数与分类数量（把 `>=18` 改为精确值，模板增删必须显式改测试）；
  - 断言 `template.toml.app_type` 与 `test_init_templates` 的期望表一致。
- 验收：新测试通过；不改任何模板与业务代码。
- 回滚：删除该测试文件即可。
- 预期立刻暴露：R3/R4/R5（一致性断言会失败），需要同步修正常量或模板——若希望 Phase 0 纯护栏，可先把 R3/R5 加入 `xfail` 并注明 Phase 2 处理。

#### Phase 1：夹具工厂，收敛 233 处内联构造

- 新增 `tests/_projects.py`：`Scenario` dataclass + `DEFAULT_SCENARIOS`（最小 CLI、多入口、GUI、带 `[tool.fspack]`、带包级入口五类）+ `materialize()`。
- 迁移 `test_config.py`、`test_build_dry_run.py`、`test_executor.py`、`test_installer.py`、`test_runner.py` 的内联构造；doctor 模板引用改为 `scenario(template_id="tk_app")` 的显式引用（仍读原目录）。
- 验收：用例数与覆盖率不变（纯重构）；e2e slow 不联网部分通过。
- 回滚：helper 与调用点同一提交，回滚该提交。
- 风险控制：不要为一次性 stub 强行抽象——只归并出现 ≥2 次的 4 类结构。

#### Phase 2：版本口径归一

- 在 `default_variables` 增加 `requires_python` 变量（默认 `>=3.8`），18 个 init 模板的 `pyproject.toml` 改用 `$requires_python`；pyside2 通过 template.toml 覆盖；
- `cli_init._DEFAULT_TEMPLATE_REQUIRES` 改为从注册表计算（或删除该常量，详情区直接读模板值）；
- `Template.requires_python` 默认值改 `">=3.8"`；
- doctor 模板的约束按真实支持矩阵修正（如 `pygame_snake` 改 `>=3.8`，`tk_app` 补 `>=3.8`），并同步更新 `test_config.py:108` 与 `:398` 的断言。
- 验收：Phase 0 护栏全绿；`test_init_smoke` 18 个模板 dry-run 通过；e2e 慢测通过。
- 回滚：按「模板批量替换」「常量/代码」「测试断言」拆 3 个提交，可分别回滚。
- 注意：这会改变用户生成项目的元数据，需要 release note；属于唯一需要产品确认的阶段。

#### Phase 3：退役 `ProjectTemplate` shim

- 删除 `src/fspack/templates/project_template.py`（95 行）；
- `src/fspack/doctor/templates.py` 改用 `list_templates(role="doctor")` / `get_template(..., role="doctor")`；
- 4 个测试文件的 `ProjectTemplate.root_dir()` 改为 `fspack.templates` 的 doctor 根目录访问器（若需要 monkeypatch，保留一个私有 `_doctor_templates_root` 即可）。
- 验收：全量测试通过；`fspack doctor` 模板列表输出不变。
- 回滚：单提交回滚，恢复 shim 文件。

### 6.3 不该合并的边界

1. **doctor 的复杂真实工程**（`webview_app` 29 文件、`cli_complex` 14 文件、`pygame_tetris` 10 文件、`pyside2_qml_dashboard` 10 文件）：保留独立目录，它们是「真实依赖、真实入口、真实资源」的集成样例，合并进 init 会让脚手架臃肿并拖慢 CI。
2. **e2e 慢测的真实构建工程**：`_write_project` 的多入口/多文件形态可以迁入场景工厂，但**不要**改成只跑 `helloworld`——它覆盖的是 loader 编译、入口包装、产物运行的真实链路。
3. **离线错误路径的内联 flask 工程**：它锁定的是「模板删除后仍需覆盖的用户形态」，迁入场景工厂可以，但不要为了「去重」删掉该场景。
4. **平台/版本特例**：pyside2 的 `<3.10`、fastapi 的 Win7 不支持、webview_app 的 `>=3.10,<3.14` 都是有依据的例外，不应被「统一为 >=3.8」抹平。

### 6.4 如果不做会怎样

- **漂移继续累积**：目前已有 5 处版本口径（根 pyproject、18 个 init 模板、registry 默认、cli_init 常量、8 个 doctor 模板），且已出现 snake 的互斥约束。
- **模板错误的漏检**：依赖期望表是手抄的，模板改错时测试可能一起「正确地错」。
- **测试维护成本线性上升**：每加一个模板要同时改注册表、`test_init_smoke` 自动覆盖、`test_init_templates` 手写期望、可能的 doctor fixture；每加一个测试场景要复制一份 pyproject。
- **删除模板时的半迁移**：`web_app` 已提供过一次案例——模板删除、测试内联，留下无法追溯来源的结构副本。
- **API/文档口径继续分叉**：`ProjectTemplate` 明确标注「新代码不要用」，但 doctor 与测试仍在使用，后续重构会在两套加载器之间反复。

## 7. 附录：证据与复现命令

```powershell
# 模板清单与字段
Get-ChildItem src/fspack/assets/init_templates -Recurse -Filter pyproject.toml
Get-ChildItem src/fspack/assets/templates -Recurse -Filter pyproject.toml

# 模板文件/行数
$i = Get-ChildItem src/fspack/assets/init_templates -Recurse -File
$d = Get-ChildItem src/fspack/assets/templates -Recurse -File
($i | Get-Content | Measure-Object -Line).Lines   # 736
($d | Get-Content | Measure-Object -Line).Lines   # 4465

# 测试侧内联构造规模
Select-String -Path tests/*.py -Pattern 'pyproject\.toml"\)\.write_text|pyproject\.write_text\('
Select-String -Path tests/*.py -Pattern 'tool\.fspack'

# 模板复用点
Select-String -Path tests/*.py -Pattern 'ProjectTemplate\.root_dir|shutil\.copytree'
Select-String -Path tests/*.py -Pattern 'init_project|list_templates'
```

局限说明：本文统计的是**静态代码中的构造点**，不等于运行时用例数（参数化会放大）；行号基于当前 main，迁移后需按实际文件重新定位。
