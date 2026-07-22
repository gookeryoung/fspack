# 迭代 26：打包过程模块抽象为 packaging 包

## 迭代目标

将分散的打包过程模块（embed/standalone/loader/installer/linux_installer）
抽象为统一的 `fspack.packaging` 包，提取共性基类，集中管理打包流程。

用户确认范围：全部打包模块；兼容策略：彻底迁移，更新所有引用，删除旧模块。

## 需求清单

- [x] 设计 packaging 包抽象层次（3 个基类 + 6 个子类）
- [x] 创建 packaging/runtime.py（RuntimeDownloader + EmbedRuntime + StandaloneRuntime）
- [x] 创建 packaging/loader.py（LoaderCompiler + WindowsLoader + LinuxLoader）
- [x] 创建 packaging/installer.py（Installer + NsisInstaller + LinuxInstaller）
- [x] 创建 packaging/__init__.py 导出 runtime + loader API
- [x] 迁移 builder.py / commands/package.py 引用
- [x] 迁移测试文件 import 与 monkeypatch 路径（6 个测试文件 + e2e_slow）
- [x] 删除旧模块 embed/standalone/loader/installer/linux_installer
- [x] 修复 pyrefly @override 缺失（27 处）与签名不匹配
- [x] 修复 ruff RUF100（@override 使 ARG 检查跳过，noqa 失效）
- [x] 门禁通过（538 passed，覆盖率 97.92%）

## 改动文件清单

### 新增

- `src/fspack/packaging/__init__.py`：包入口，仅导出 runtime + loader API
  （installer 依赖 builder，避免循环导入不在此导出）
- `src/fspack/packaging/runtime.py`：RuntimeDownloader 基类 + Embed/Standalone
  子类 + 函数式 API（download_embed/extract_embed/ensure_embed/download_standalone/
  extract_standalone/ensure_standalone/write_pth 等）
- `src/fspack/packaging/loader.py`：LoaderCompiler 基类 + Windows/Linux 子类 +
  函数式 API（compile_loader/generate_loader_source/loader_cache_dir/
  mingw_available/gcc_available 等）
- `src/fspack/packaging/installer.py`：Installer 基类 + Nsis/Linux 子类 +
  函数式 API（build_installer/build_linux_installer/generate_nsis_script/
  compile_installer/build_tarball/build_deb）

### 修改

- `src/fspack/builder.py`：导入从 fspack.embed/standalone/loader 改为
  fspack.packaging.runtime/loader
- `src/fspack/commands/package.py`：导入从 fspack.installer/linux_installer
  改为 fspack.packaging.installer
- `tests/test_embed.py`：import 与 monkeypatch 路径改为
  fspack.packaging.runtime
- `tests/test_standalone.py`：同上
- `tests/test_loader.py`：import 与 monkeypatch 路径改为
  fspack.packaging.loader（subprocess.run/shutil.which/shutil.copy2）
- `tests/test_installer.py`：import 与 monkeypatch 路径改为
  fspack.packaging.installer
- `tests/test_linux_installer.py`：同上
- `tests/test_e2e_slow.py`：loader/installer/linux_installer 导入路径迁移

### 删除

- `src/fspack/embed.py`
- `src/fspack/standalone.py`
- `src/fspack/loader.py`
- `src/fspack/installer.py`
- `src/fspack/linux_installer.py`

## 关键决策与依据

### 1. 基类划分：3 个基类对应 3 类打包流程

- `RuntimeDownloader`：download → extract → ensure 三步流程
  （embed python + python-build-standalone）
- `LoaderCompiler`：generate → compile → cache 流程
  （Windows mingw + Linux gcc）
- `Installer`：build → 校验 → build_package 编排流程
  （NSIS + tar.gz/.deb）

依据：三类流程内部共性显著（缓存检查、进度下载、归档解压、marker 检查等），
跨平台差异通过钩子方法下沉到子类。

### 2. 循环导入规避：__init__.py 仅导出 runtime + loader

`installer.py` 依赖 `fspack.builder`，若 `__init__.py` 导出 installer 会形成
`builder → packaging.__init__ → installer → builder` 循环。解决方案：
`__init__.py` 只导出 runtime + loader，installer API 直接
`from fspack.packaging.installer import ...`。

### 3. 函数式 API 保留：测试 monkeypatch 兼容

保留 `download_embed`/`ensure_embed`/`compile_loader`/`build_installer` 等
模块级函数，委托给类方法。关键细节：`ensure_*` 函数内部调用 `download_*`
函数（而非类方法），便于测试 monkeypatch 拦截 `download_*` 路径。

### 4. runtime_label 类属性：错误消息兼容

基类用 `f"下载 {cls.runtime_label} 失败"` 构造错误消息，子类通过
`runtime_label` 类属性（"embed python" / "python-build-standalone"）定制，
保持与旧模块错误消息一致（含空格），测试 `match=` 不需改动。

### 5. @override 装饰器：pyrefly strict 模式要求

pyrefly strict 模式要求所有覆盖基类的方法加 `@override`。共补 27 处。
导入模式复用 slim/base.py 的版本守卫：

```python
if sys.version_info >= (3, 12):  # pragma: no cover
    from typing import override
else:
    from typing_extensions import override
```

### 6. 子类签名统一为 **kwargs: object

基类钩子方法用 `**kwargs: object` 传递差异化参数（mirror/release_tag），
子类签名也统一为 `**kwargs: object`，内部用 `assert isinstance()` 类型收窄。
避免 pyrefly bad-override（子类命名参数与基类 **kwargs 不兼容）。

### 7. RUF100 修复：@override 使 ARG 检查被跳过

`LinuxLoader._build_command` 的 `app_type`/`icon_obj` 参数未使用，
原本加 `# noqa: ARG003`。但 `@override` 装饰器使 ruff 跳过 ARG 检查，
noqa 变为未使用，触发 RUF100。解决：删除 noqa 注释。

## 代码实现情况

### runtime.py 设计

```python
class RuntimeDownloader(abc.ABC):
    download_timeout: int = 180
    runtime_label: str = "运行时"

    # 钩子方法（子类实现）
    @classmethod
    @abc.abstractmethod
    def archive_name(cls, version, **kwargs) -> str: ...
    def download_url(cls, version, **kwargs) -> str: ...
    def marker_path(cls, runtime_dir, version) -> Path: ...
    def extract_archive(cls, archive_path, runtime_dir) -> None: ...

    # 可覆盖钩子
    def download_label(cls, version) -> str: ...
    def post_extract(cls, runtime_dir, version) -> None: ...

    # 通用流程
    def download(cls, version, cache_dir, *, stage, **kwargs) -> Path: ...
    def extract(cls, archive_path, runtime_dir) -> None: ...
    def ensure(cls, version, cache_dir, runtime_dir, *, stage, **kwargs) -> Path: ...
```

### loader.py 设计

```python
class LoaderCompiler(abc.ABC):
    platform: Platform
    exe_suffix: str
    compiler_name: str
    install_hint: str

    @classmethod
    @abc.abstractmethod
    def generate_source(cls, py_xy) -> str: ...
    def _build_command(cls, c_file, out_exe, app_type, icon_obj) -> list[str]: ...

    # 通用流程（含缓存）
    def compile(cls, source, out_exe, app_type, work_dir, platform, *, cache_dir, stage): ...
    def available(cls) -> bool: ...
```

### installer.py 设计

```python
class Installer(abc.ABC):
    @classmethod
    @abc.abstractmethod
    def target_platform(cls) -> Platform: ...
    def exe_filename(cls, info) -> str: ...
    def build_package(cls, dist_dir, info, release_dir) -> Path: ...

    # 通用编排
    def build_installer(cls, project_dir, mirror, py_version, *, no_build, dist_dir) -> Path: ...
```

## 整合优化情况

- 函数式 API 委托类方法，消除旧模块重复逻辑
- 错误消息通过 `runtime_label` 类属性统一构造
- `@override` 装饰器全部补齐，pyrefly strict 0 错误
- 旧模块删除，避免代码漂移

## 测试验证结果

- ruff check src tests：通过
- ruff format --check src tests：50 文件已格式化
- pyrefly check：0 错误（3 suppressed, 5 warnings not shown）
- pytest -m "not slow" --cov=fspack --cov-fail-under=95：
  - 538 passed, 20 deselected
  - 覆盖率 97.92%（>95%）
  - packaging/runtime.py 90%（未覆盖 RuntimeDownloader.ensure 基类方法，
    因函数式 ensure_* 重新实现逻辑以支持 monkeypatch；可接受）
  - packaging/loader.py 100%
  - packaging/installer.py 100%

## 遗留事项

- `RuntimeDownloader.ensure` 基类方法未被直接调用（函数式 `ensure_embed`/
  `ensure_standalone` 为 monkeypatch 兼容重新实现逻辑）。覆盖率 90% 可接受。
  未来若测试改为 monkeypatch 类方法路径，可消除该重复。
- `_parse_pip_download_wheels` 目录扫描回退可能返回跨版本 wheel（iter-25 遗留，
  与本次抽象无关）

## 下一轮计划

无。本次抽象任务已闭环。
