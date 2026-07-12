# iter-01 CLI 与 C Loader 垂直切片

## 迭代目标

交付 P1：从 pyproject.toml 解析到 `fsp b` 产出可被 `fsp r` 运行的 hello world .exe，全程在 Linux 用 mingw 交叉编译 + wine 运行验证。

## 关键决策与依据

### 模块结构
```
src/fspack/
  __init__.py          # 版本号
  cli.py               # fsp b/c/r 子命令分发（argparse）
  commands/
    __init__.py
    build.py           # fsp b → builder.build()
    clean.py           # fsp c → 清理 dist/
    run.py             # fsp r → wine 运行 dist .exe
  project.py           # pyproject.toml → ProjectInfo
  analyzer.py          # AST import 扫描 → DependencyReport
  mirror.py            # MirrorConfig 国内镜像
  embed.py             # embed python 下载/解压/_pth
  loader.py            # C loader 源码生成 + mingw 交叉编译
  builder.py           # 流水线编排
  config.py            # dataclass（ProjectInfo/AppType/MirrorConfig 等）
  exceptions.py        # FspackError 层级
```

### 镜像选型
- 华为云（已验证 HTTP 200）：`https://mirrors.huaweicloud.com/python/{ver}/python-{ver}-embed-amd64.zip`，pip 索引 `https://mirrors.huaweicloud.com/pypi/simple/`。
- 备选阿里云、清华；默认华为云，`--mirror` 可切换。

### embed python _pth
```
python3X.zip
.
Lib
Lib\site-packages
import site
```

### C loader 设计
- 不依赖 Python.h（embed 包不含开发头文件），改用动态加载 `python3X.dll`，解析 `Py_Main(argc, argv)` 符号调用。
- 配置在生成时以 `#define` 烧入：`ENTRY_FILE`、`PYTHON_HOME`（相对 exe 的 runtime 子目录）、`PYTHON_DLL`（如 `python311.dll`）。
- 设置环境变量 PYTHONHOME/PYTHONPATH 后 `Py_Main(["loader.exe", exe_dir\ENTRY_FILE, ...用户参数])`。
- GUI 子系统：mingw `-mwindows`；CLI：默认 console。
- 版本无关：优先加载 `python3.dll`（稳定 ABI），回退 `python3X.dll`。

### 依赖下载策略（Linux dev 拉 Windows wheel）
- embed python.exe 是 Windows 程序，Linux dev 无法直接跑它的 pip。
- 方案：dev python 执行 `pip download -d wheelhouse --platform win_amd64 --python-version <ver> --only-binary=:all: -i <镜像> <deps>`，再用 `zipfile` 把每个 .whl 解包到 `dist/Lib/site-packages/`。
- 无依赖项目（hello world）跳过此步。

### Python 3.8 兼容
- `.python-version=3.8`，uv 已装 3.8.20。
- toml：`sys.version_info>=(3,11)` 用 `tomllib`，否则 `tomli`（条件依赖 `tomli; python_version<'3.11'`）。
- 标准库模块名：`sys.stdlib_module_names`（3.10+）不可用，3.8/3.9 用 curated frozenset 回退。
- 注解用 `from __future__ import annotations`，可用 `list[str]` 等。

### 测试策略
- 公共 API 单测：解析、AST、镜像 URL、_pth 内容、loader C 源码字符串、wheel 解包。mock 网络（urllib）与子进程（pip/mingw）。
- `@pytest.mark.slow`：真实 embed 下载、mingw 编译、wine 运行、端到端 hello world。
- 门禁：`ruff check`/`ruff format --check`/`pyrefly check`/`pytest -m 'not slow' --cov≥95%`。

## 改动文件清单

新增：
- src/fspack/{config,exceptions,project,analyzer,mirror,embed,loader,builder,cli}.py
- src/fspack/commands/{__init__,build,clean,run}.py
- tests/test_{config,project,analyzer,mirror,embed,loader,builder,cli}.py
- tests/examples/helloworld/ 示例项目
- .trae/req/req-01-fspack-p1.md、.trae/docs/iter-01-cli-loader.md

修改：
- src/fspack/__init__.py（保持版本）
- src/fspack/cli.py（重写为子命令分发）
- pyproject.toml（加 tomli 条件依赖）
- README.md（更新快速上手）

## 验证结果

非 slow 门禁全过（2026-07-12）：

- `uv run ruff check src tests`：All checks passed!
- `uv run ruff format --check src tests`：27 files already formatted
- `uv run pyrefly check`：0 errors（1 suppressed）
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：83 passed, 1 deselected, 覆盖率 99.45%

修复要点：
- `DependencyReport.missing` 用 `re.split(r"[<>=!~;\[]", d, maxsplit=1)` 剥离版本号，修正 "numpy>=1.0" 误判 missing。
- 移除 `test_analyze_dependencies_classification` 中错误的 `assert 'main' in r.ast_local`（项目名未被 import 不在 ast_local）。
- 移除 `test_write_pth_content` 中 site-packages 目录断言（该目录由 `ensure_embed` 创建，`write_pth` 只写文件）。
- 移除 `_infer_app_type` 死代码 try/except（`_has_entry` 已验证解析成功）。
- 新增 `tests/test_commands.py` 直测 build/clean/run 子命令与 `_build_cmd` Linux(wine 有/无)/非 Linux 分支。
- 补 project.py 分支测试：[project] 非 dict、_has_entry SyntaxError 跳过、多依赖非 GUI 推断 CLI。

唯一未覆盖行：`project.py:83`（`detect_entry` 中 `mod in seen` 防御性 continue，正常流程无法触发，保留作防御）。

## 遗留事项

- mingw-w64 + wine 需用户 sudo 安装；安装前 slow 测试（真实 embed 下载 + mingw 编译 + wine 运行 + hello world 端到端）无法运行。
- 需求 #10 端到端验证待 mingw+wine 就绪后跑 `pytest -m slow`。
- P2：NSIS 安装包；P3：Linux 支持。
