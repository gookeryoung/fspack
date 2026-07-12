# 迭代 05：端到端示例验证

## 迭代目标

制定 5 类典型项目示例（无库 CLI、有库 CLI、有库 GUI、有库 pygame、有库 web），在 mingw + wine 环境下真实构建并运行，验证 fspack 打包链路端到端可用。

## 改动文件清单

### 源码修复

- `src/fspack/mirror.py`：`DEFAULT_MIRROR` 从 `"huawei"` 改为 `"aliyun"`（华为云 PyPI 索引改版为 HTML 门户，不再提供 PEP 503 simple 索引）。
- `src/fspack/embed.py`：`write_pth` 写入位置从 `dist/python311._pth` 改到 `dist/runtime/python311._pth`（与 python311.dll 同目录），路径改为相对 runtime（`python311.zip`/`.`/`Lib\site-packages`/`..\src`/`import site`）。
- `src/fspack/loader.py`：更新模块 docstring 中 _pth 位置描述。

### 示例项目

- `tests/examples/helloworld/`：无库 CLI（已有）。
- `tests/examples/clitool/`：有库 CLI（requests），pyproject 声明 `dependencies = ["requests"]`。
- `tests/examples/guicalc/`：有库 GUI（PySide6），入口内 `os.add_dll_directory` 注册 PySide6 DLL 目录（embed python 下 Windows 不搜索 .pyd 所在目录的依赖 DLL）。
- `tests/examples/pygamedemo/`：有库 pygame，`pygame.init()` + 打印版本。
- `tests/examples/webapp/`：有库 web（flask），`test_client` 验证路由响应。

### 测试

- `tests/test_e2e_slow.py`：5 个 `@pytest.mark.slow` 端到端测试（build + wine 运行 + 断言输出）。guicalc 测试在 wine 缺 icuuc.dll 时跳过运行断言但保留构建验证。
- `tests/test_mirror.py`：默认镜像断言从 huawei 改为 aliyun。
- `tests/test_commands.py`：默认镜像断言从 huawei 改为 aliyun（build_run_default + package_run_default）。
- `tests/test_embed.py`：`test_write_pth_content` 断言新路径与位置。
- `tests/test_builder.py`：_pth 路径断言改为 `dist/runtime/python311._pth`，内容断言改为 `python311.zip` + `..\src`。

### 配置

- `pyproject.toml`：pyrefly `project-excludes` 加 `tests/examples/**`。

## 关键决策与依据

### 1. 默认镜像切换：华为云 → 阿里云

华为云 PyPI 镜像（`https://mirrors.huaweicloud.com/pypi/simple/`）改版为 HTML 门户页面，不再返回 PEP 503 simple 索引格式。pip download 报 `Could not find a version that satisfies the requirement flask (from versions: none)`。阿里云镜像（`https://mirrors.aliyun.com/pypi/simple/`）正常工作。华为云 embed python 下载（`https://mirrors.huaweicloud.com/python/`）仍可用，但 PyPI 索引失效导致有依赖项目无法打包，故切换默认。

### 2. _pth 文件位置：dist/ → runtime/

CPython embed 模式要求 `python3X._pth` 与 `python3X.dll` 同目录。原设计将 _pth 放在 `dist/`（与 loader.exe 同目录），但 python311.dll 在 `dist/runtime/`，Python 找不到 _pth，导致 sys.path 不含 site-packages，有依赖项目运行时报 `ModuleNotFoundError`。

embed zip 自带 `runtime/python311._pth`（仅 `python311.zip` + `.`，`import site` 注释掉），Python 实际读的是这个文件而非我们写的。修复：`write_pth` 改写到 `dist/runtime/`，覆盖原始文件，路径改为相对 runtime（`..\src` 引用 dist 下的 src）。

helloworld 之前能通过是因为无第三方依赖，loader 直接传入口脚本全路径运行，不依赖 site-packages。

### 3. PySide6 DLL 搜索路径

PySide6 的 Qt DLL（Qt6Core.dll、Qt6Widgets.dll 等）在 `site-packages/PySide6/` 子目录。Windows 加载 .pyd 时不搜索 .pyd 所在目录的依赖 DLL（仅搜索 exe 目录、系统目录、PATH）。示例入口需 `os.add_dll_directory(PySide6 目录)` 注册。

### 4. wine 缺 icuuc.dll

PySide6 6.11.1 的 Qt6Core.dll 依赖 `icuuc.dll`（Windows 10+ 系统 DLL，ICU Unicode 库）。wine 9.0 不提供此 DLL。在真实 Windows 上可正常运行；wine 下构建验证通过（PySide6 下载/解包/_pth/exe 全就绪），运行断言跳过。

## 验证结果

### 门禁（非 slow）

```
ruff check: All checks passed
ruff format --check: 38 files already formatted
pyrefly check: 0 errors
pytest -m "not slow": 122 passed, 5 deselected, coverage 99.87%
```

### 端到端（slow）

```
pytest -m slow: 4 passed, 1 skipped in 17.44s

test_build_and_run_helloworld    PASSED  (输出 "hello, world")
test_build_and_run_clitool       PASSED  (输出 "requests 2.34.2")
test_build_and_run_guicalc       SKIPPED (wine 缺 icuuc.dll，构建验证通过)
test_build_and_run_pygamedemo    PASSED  (输出 "pygame 2.6...")
test_build_and_run_webapp        PASSED  (输出 "hello from flask")
```

## 遗留事项

- guicalc 在 wine 下因缺 icuuc.dll 跳过运行，真实 Windows 可运行。用户可 `winetricks icuuc` 或安装 ICU 后重跑。
- 华为云 PyPI 索引失效，保留 huawei 配置（embed python 下载仍可用）但默认切到 aliyun。
- pip 需在 dev venv 中可用（`uv pip install pip`），fspack 依赖 `python -m pip download` 拉 wheel。
