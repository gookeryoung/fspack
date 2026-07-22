# iter-28 console/entry/net 类化重构

## 需求清单

- [x] 1. 重构 net.py，完善项目（重建 packaging/net.py，整合 SSL + HTTP 下载为 Downloader 类）
- [x] 2. console.py 类化为 ConsoleUI，调用点全改
- [x] 3. entry_wrapper 整合到 packaging/entry.py，类化为 EntryWrapper

## 迭代目标

将散落的模块级函数按职责类化并整合进 `packaging` 包：

1. **net.py 重建**：iter-27 拆分 builder 时 net.py 被误删，`create_ssl_context` 被并入 runtime.py。本轮新建 `packaging/net.py`，将 `create_ssl_context`（来自 runtime.py）与 `download_with_progress`（来自 progress.py）整合为 `Downloader` 类。
2. **console.py 类化**：模块级函数 `step/success/warn/error/setup_logging` 全部封装为 `ConsoleUI` 类方法，模块级 `console = ConsoleUI()` 单例。调用点全部改为 `console.xxx()`，无向后兼容。暴露 `rich` 属性供 Progress/Status/capture 使用。
3. **entry_wrapper 整合**：删除 `entry_wrapper.py`，迁移到 `packaging/entry.py`，类化为 `EntryWrapper`（@staticmethod 集合）。

## 改动文件清单

### 新增

- `src/fspack/packaging/net.py` —— `Downloader` 类（`create_ssl_context` @staticmethod + `download` 实例方法）
- `src/fspack/packaging/entry.py` —— `EntryWrapper` 类（`dotted_module_name`/`generate_wrapper_source` @staticmethod）

### 删除

- `src/fspack/entry_wrapper.py` —— 迁移至 packaging/entry.py

### 修改

- `src/fspack/console.py` —— 移除模块级函数，新增 `ConsoleUI` 类与 `console` 单例
- `src/fspack/progress.py` —— 移除 `download_with_progress`，`console=console` 改为 `console=console.rich`，`console.status` 改为 `console.rich.status`
- `src/fspack/packaging/runtime.py` —— 移除 `create_ssl_context`，改用 `Downloader` 实例下载
- `src/fspack/packaging/__init__.py` —— 导出 `Downloader`、`EntryWrapper`
- `src/fspack/builder.py` —— 调用点改为 `console.success()`/`console.rich.print()`/`EntryWrapper.xxx()`
- `src/fspack/cli.py` —— `setup_logging()` 改为 `console.setup_logging()`
- `src/fspack/packaging/installer.py` —— `step()`/`success()` 改为 `console.step()`/`console.success()`

### 测试修改

- `tests/test_console.py` —— 改用 `console.step()`/`console.rich.capture()`/`console.setup_logging()`
- `tests/test_progress.py` —— 移除 `download_with_progress` 测试与 `_FakeResp`，`console.capture/print` 改为 `console.rich.*`
- `tests/test_net.py` —— 重写为 `TestCreateSslContext`（3 用例）+ `TestDownloaderDownload`（4 用例）
- `tests/test_entry_wrapper.py` —— 改用 `EntryWrapper.dotted_module_name()`/`EntryWrapper.generate_wrapper_source()`
- `tests/test_builder.py` —— `console.capture()` 改为 `console.rich.capture()`
- `tests/test_embed.py` —— monkeypatch 路径 `fspack.progress.urllib.request.urlopen` 改为 `fspack.packaging.net.urllib.request.urlopen`
- `tests/test_standalone.py` —— 同上

## 关键决策与依据

1. **net.py 重建为 Downloader 类**：用户明确要求 "packaging/net.py + Downloader 类"。整合 SSL 上下文创建与 HTTP 下载两个紧密相关的职责，实例化时构造 SSL 上下文，下载方法直接复用。
2. **console.py 独立 + 类化**：用户明确要求 "独立 + 类化，调用点全改"。保留 console.py 作为独立模块（不并入 packaging，因为它是横切关注点），但内部类化为 ConsoleUI。`rich` 属性暴露底层 rich.Console，供 Progress/Status/capture 等需要原始 Console 的场景使用。
3. **entry_wrapper 整合到 packaging/entry.py**：用户明确要求 "整合到 packaging/entry.py"。EntryWrapper 与打包流程强相关（生成入口包装器源码），归入 packaging 包合理。类化为 @staticmethod 集合，因为无实例状态。
4. **不导出 installer**：installer 依赖 builder，导出会导致循环导入（builder → packaging → installer → builder）。保持现状，直接从 `fspack.packaging.installer` 导入。
5. **monkeypatch 路径迁移**：`download_with_progress` 从 progress.py 移到 packaging/net.py 后，测试中 `fspack.progress.urllib.request.urlopen` 必须改为 `fspack.packaging.net.urllib.request.urlopen`。

## 代码实现情况

### ConsoleUI 类

```python
class ConsoleUI:
    def __init__(self) -> None:
        self._console: Final = Console(theme=_theme)

    @property
    def rich(self) -> Console:
        return self._console

    def setup_logging(self, verbose: bool = False) -> None: ...
    def step(self, title: str) -> None: ...
    def success(self, msg: str) -> None: ...
    def warn(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...

console: Final[ConsoleUI] = ConsoleUI()
```

### Downloader 类

```python
class Downloader:
    def __init__(self, *, timeout: int = 180, ssl_ctx: ssl.SSLContext | None = None) -> None:
        self._timeout = timeout
        self._ssl_ctx = ssl_ctx or self.create_ssl_context()

    @staticmethod
    def create_ssl_context() -> ssl.SSLContext: ...

    def download(self, url: str, dest: Path, *, stage: StageRecorder | None = None, label: str = "") -> int: ...
```

### EntryWrapper 类

```python
class EntryWrapper:
    _TEMPLATE = _WRAPPER_TEMPLATE

    @staticmethod
    def dotted_module_name(src_dir: Path, entry_file: Path) -> tuple[str, str] | None: ...

    @staticmethod
    def generate_wrapper_source(entry_name: str, module_dotted: str | None, entry_rel: str, pkg_root_rel: str = ".") -> str: ...
```

## 整合优化情况

- progress.py 移除 download_with_progress 后，模块职责更纯粹：仅保留 BuildTracker/StageRecorder 数据类与 spinner/iter_with_progress 两个渲染辅助。
- runtime.py 移除 create_ssl_context 后，不再承担 SSL 配置职责，专注运行时下载/解压/ensure 流程。
- packaging 包现共 6 个子模块：runtime、loader、installer、wheels、net、entry，职责清晰。

## 测试验证结果

- ruff check：通过
- ruff format --check：52 文件已格式化
- pyrefly check：0 errors（3 suppressed, 5 warnings）
- pytest：538 passed, 20 deselected
- 覆盖率：97.95%（≥95% 门禁）

关键模块覆盖率：

- console.py: 100%
- packaging/net.py: 100%
- packaging/entry.py: 100%
- progress.py: 100%
- packaging/runtime.py: 90%（未覆盖行多为网络/解压实际调用分支）

## 遗留事项

无。

## 下一轮计划

无。本轮完成用户全部两项需求，进入收尾。
