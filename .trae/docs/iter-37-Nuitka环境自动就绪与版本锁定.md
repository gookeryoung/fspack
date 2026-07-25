# iter-37: Nuitka 环境自动就绪与版本锁定

## 需求清单

- [x] 解决 embed python 不带 nuitka 模块导致 `--nuitka` 总是提示跳过的问题
- [x] 缓存 nuitka wheel 到本地并安装到 runtime 进行构建
- [x] 按 Python 版本锁定对应 nuitka 版本（差异化兼容矩阵）
- [x] Linux 缺 gcc 时 raise 错误（不静默跳过）
- [x] stamp 缓存：源码/版本未变时跳过整个 Nuitka 阶段
- [x] 修复 BuildOptions.pyc_optimize 默认值与 cli.py 不一致（iter-36 遗留）

## 迭代目标

iter-35 引入 `--nuitka` 模式但 embed python 默认无 pip 无 nuitka，`is_available()` 永远 False，导致功能形同虚设。本迭代实现完整的 Nuitka 环境就绪流程：自动下载并解压锁定版 nuitka wheel 到 runtime，加 C 编译器前置检查与 stamp 缓存，让 `--nuitka` 真正可用。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| src/fspack/config.py | 新增 `NUITKA_VERSIONS` dict、`DEFAULT_NUITKA_VERSION`、`nuitka_version_for()` 函数；`BuildOptions.pyc_optimize` 默认值 `0 → 2`（与 cli argparse default 一致，修复 iter-36 遗留不一致） |
| src/fspack/exceptions.py | 新增 `NuitkaError(FspackError)` 异常 |
| src/fspack/packaging/nuitka.py | 新增 `_runtime_site_packages()`/`_check_c_compiler()`/`ensure_env()`/`compile_with_stamp()`/`_stamp_path()`/`_stamp_key()`；重写模块 docstring 说明环境就绪流程 |
| src/fspack/builder.py | Nuitka 阶段调用 `compile_with_stamp()` 替代直接 `compile_src()`，移除 spinner 包装（stamp 命中时不应显示 spinner） |
| tests/test_nuitka.py | 新增 18 个测试：`nuitka_version_for` 字典查询（4）、`_check_c_compiler` C 编译器检查（4）、`ensure_env` 环境就绪（5）、`compile_with_stamp` stamp 缓存（5） |
| tests/test_builder.py | `test_build_with_nuitka_invokes_compiler`/`test_build_nuitka_skipped_on_cross_compile` 改为 mock `compile_with_stamp`（含 `cls` 参数适配 classmethod） |
| tests/test_cli.py | `test_build_pyc_options_default_false` 断言 `pyc_optimize == 2`（与 cli default 一致） |

## 关键决策与依据

### Nuitka 版本按 Python 版本差异化锁定

`NUITKA_VERSIONS` dict 按 `major.minor` 映射 nuitka 版本：

- Python 3.8/3.9 → nuitka 2.5.1（4.x 已不再维护 EOL 的 3.8）
- Python 3.10+ → nuitka 4.1.3（当前最新稳定版）

键用 `major.minor` 与 `KNOWN_EMBED_VERSIONS`/`KNOWN_STANDALONE_VERSIONS` 风格一致，避免每个补丁版本重复（3.11.9 与 3.11.15 共用 4.1.3）。未知 Python 版本（如未来 3.15）回退 `DEFAULT_NUITKA_VERSION = "4.1.3"`。

### 用构建机 pip install --target 从 sdist 构建 nuitka

实际运行发现 nuitka 4.x 在 PyPI 只发布 sdist（无预构建 wheel），fspack 的 `download_wheels` 用 `--only-binary=:all:` 无法处理。改用构建机 `pip install --target <runtime_site_packages>` 让 pip 自动完成 sdist 下载、构建、解压：

```bash
build_python -m pip install --target runtime/Lib/site-packages --no-compile --no-cache-dir -i <mirror> nuitka==4.1.3
```

关键发现：

- nuitka sdist 构建出的 wheel 标签与构建机 python ABI 绑定（如 `cp313-cp313-win_amd64`），但实际内容是纯 Python（无 `.pyd`），跨 Python 版本可 `import`
- `--no-compile` 避免 .pyc 版本不匹配（构建机 python 版本可能与 runtime 不同）
- `--no-cache-dir` 避免 pip 缓存污染
- 复用 fspack 镜像源（`-i mirror.pypi_index`）

构建机 python 缺 pip 时 raise `NuitkaError`（uv venv 默认无 pip，CI 已配置 `uv pip install pip`）。

### C 编译器缺失直接 raise（用户确认）

- Windows 缺 mingw：raise `NuitkaError("Nuitka 编译需要 mingw-w64 交叉编译器..."）`
- Linux 缺 gcc：raise `NuitkaError("Nuitka 编译需要 gcc...")`

用户确认 Linux 缺 gcc 需显式报错而非静默跳过，避免用户误以为 Nuitka 已生效但实际未编译。

### stamp 缓存键设计

`dist/.nuitka_compile_stamp` 内容 = `f"{nuitka_version}|{py_version}|{src_fingerprint}"`

三要素：

- `nuitka_version`：切换 Nuitka 版本时强制重编（如 3.10 从 4.1.3 升级到 4.2）
- `py_version`：切换 Python 版本时强制重编（.pyd ABI 绑定）
- `src_fingerprint`：用户源码变化时强制重编（按 rule-01 闭环要求）

`pyc_optimize` 不纳入：Nuitka 编译不受 .pyc 优化级别影响，site-packages 的 .pyc 由 `_precompile_pyc` 单独缓存。

### pyc_optimize 默认值修复（iter-36 遗留）

iter-36 引入 `BuildOptions` 时 `pyc_optimize: int = 0`，但 iter-35 已将 cli.py `--pyc-optimize` argparse default 设为 2。两者不一致导致 `test_build_pyc_options_default_false` 失败。本迭代统一为 `pyc_optimize: int = 2`，与 cli default 一致。

## 代码实现情况

### config.py NUITKA_VERSIONS 字典

```python
NUITKA_VERSIONS: dict[str, str] = {
    "3.8": "2.5.1",
    "3.9": "2.5.1",
    "3.10": "4.1.3",
    "3.11": "4.1.3",
    "3.12": "4.1.3",
    "3.13": "4.1.3",
    "3.14": "4.1.3",
}
DEFAULT_NUITKA_VERSION = "4.1.3"

def nuitka_version_for(py_version: str) -> str:
    """按目标 Python 版本返回锁定的 Nuitka 版本."""
    major, minor = py_version.split(".")[:2]
    return NUITKA_VERSIONS.get(f"{major}.{minor}", DEFAULT_NUITKA_VERSION)
```

### nuitka.py ensure_env 流程

```python
@classmethod
def ensure_env(cls, runtime_dir, py_version, target, mirror, *, stage) -> str:
    cls._check_c_compiler(target)  # 缺 C 编译器 raise NuitkaError
    nuitka_ver = nuitka_version_for(py_version)
    py_exe = cls._runtime_python(runtime_dir, py_version, target)
    if cls.is_available(py_exe):  # 已装则跳过
        stage.hit_cache()
        return nuitka_ver
    # nuitka 4.x 只发布 sdist，用构建机 pip install --target 从 sdist 构建并解压
    build_python = sys.executable
    cls._ensure_pip_available(build_python)  # 缺 pip raise NuitkaError
    cmd = [build_python, "-m", "pip", "install", "--target", str(site_packages),
           "--no-compile", "--no-cache-dir", "-i", mirror.pypi_index,
           f"nuitka=={nuitka_ver}"]
    subprocess.run(cmd, check=True)
    if not cls.is_available(py_exe):  # 验证安装
        raise NuitkaError("nuitka 安装后 import nuitka 仍失败")
    return nuitka_ver
```

### nuitka.py compile_with_stamp 入口

```python
@classmethod
def compile_with_stamp(cls, src_dir, dist_dir, runtime_dir, py_version, target, mirror, *, stage):
    nuitka_ver = nuitka_version_for(py_version)
    stamp = cls._stamp_path(dist_dir)
    stamp_key = cls._stamp_key(src_dir, nuitka_ver, py_version)
    # stamp 命中：跳过整个阶段
    if stamp.is_file() and stamp.read_text() == stamp_key:
        stage.hit_cache()
        return
    # 未命中：ensure_env + compile_src + 写 stamp
    cls.ensure_env(runtime_dir, py_version, target, mirror, stage=stage)
    cls.compile_src(src_dir, runtime_dir, py_version, target, stage=stage)
    stamp.write_text(stamp_key)
```

### builder.py Nuitka 阶段集成

```python
if opts.nuitka and target is detect_platform():
    with tracker.stage("Nuitka 编译") as st:
        from fspack.packaging.nuitka import NuitkaCompiler
        NuitkaCompiler.compile_with_stamp(
            src_dst, cfg.dist_dir, runtime_dir, info.py_version, target, cfg.mirror, stage=st,
        )
```

## 整合优化情况

- `NuitkaCompiler` 公共 API 收编为四个方法：`is_available`/`ensure_env`/`compile_src`/`compile_with_stamp`，职责清晰
- `ensure_env` 用 `pip install --target` 复用 pip 的 sdist 构建能力，绕过 `download_wheels` 的 `--only-binary=:all:` 限制
- stamp 缓存跨阶段统一风格：与 `_pyc_stamp_path`/`_pyc_stamp_key` 设计一致
- `nuitka_version_for()` 封装字典查询，nuitka.py 与测试均通过此函数访问，避免直接读 dict

## 测试验证结果

- ruff check: All checks passed
- ruff format --check: 50 files already formatted
- pyrefly check: 0 errors (59 suppressed, 7 warnings not shown)
- pytest -m "not slow": 762 passed, 21 deselected, 覆盖率 97.24%
- Nuitka 模块覆盖率 96%（133 stmts, 6 miss, 40 branch, 1 BrPart）
- 新增测试覆盖：
  - `nuitka_version_for` 字典查询（4 个）：3.11→4.1.3、3.8→2.5.1、未知→default、major.minor 匹配
  - `_check_c_compiler` C 编译器检查（4 个）：Windows 缺 mingw raise、Windows 有 mingw pass、Linux 缺 gcc raise、Linux 有 gcc pass
  - `ensure_env` 环境就绪（7 个）：runtime py 缺失 raise、已装跳过 pip install、pip install --target 命令验证、缺 pip raise、pip install 失败 raise、安装后 import 失败 raise、Linux 路径
  - `compile_with_stamp` stamp 缓存（5 个）：命中跳过、未命中写 stamp、源码变化失效、stamp 键三段式、stamp 路径

## 遗留事项

- 端到端慢测试（`tests/test_e2e_slow.py`）未新增 `--nuitka` 真实编译场景，因 nuitka wheel 下载与编译耗时较长（每个 .py 3-10s），不适合 CI 默认执行。后续可在 `--nuitka` 稳定后新增 `@pytest.mark.slow` 标记的端到端测试
- Nuitka 编译失败时仅告警不中断，已成功编译的 .pyd 仍可用。若用户希望严格模式（任一文件失败即中断），可后续加 `--nuitka-strict` 选项

## 下一轮计划

无明确下一轮计划，等待用户反馈或新需求。
