# req-30 Nuitka 环境自动就绪与版本锁定

## 需求

- [x] 解决 embed python 不带 nuitka 模块导致 `--nuitka` 总是提示跳过的问题
- [x] 缓存 nuitka wheel 到本地并安装到 runtime 进行构建
- [x] 按 Python 版本锁定对应 nuitka 版本（差异化兼容矩阵）
- [x] Linux 缺 gcc 时 raise 错误（不静默跳过）
- [x] stamp 缓存：源码/版本未变时跳过整个 Nuitka 阶段

## 背景

iter-35 引入 `--nuitka` 模式但 embed python 默认无 pip 无 nuitka，`is_available()` 永远 False，导致功能形同虚设。需要实现完整的 Nuitka 环境就绪流程：自动下载并解压锁定版 nuitka wheel 到 runtime，加 C 编译器前置检查与 stamp 缓存。

## 设计要点

### Nuitka 版本按 Python 版本差异化锁定

```
NUITKA_VERSIONS = {
    "3.8": "2.5.1",  # 4.x 不再维护 EOL 3.8
    "3.9": "2.5.1",
    "3.10": "4.1.3",  # 当前最新稳定版
    "3.11": "4.1.3",
    "3.12": "4.1.3",
    "3.13": "4.1.3",
    "3.14": "4.1.3",
}
```

### 绕过 pip 直接解压 wheel

embed python 无 pip，但 nuitka 是纯 Python 包（`py3-none-any`），可直接解压 wheel 到 `runtime/Lib/site-packages`：

1. `download_wheels(("nuitka==<version>",), ...)` 复用现有 pip download 逻辑
2. `unpack_wheels(wheels, site_packages)` 复用现有解压逻辑
3. nuitka 依赖（`ordered-set`）自动解析

### C 编译器缺失直接 raise

- Windows 缺 mingw：raise `NuitkaError`
- Linux 缺 gcc：raise `NuitkaError`（用户确认需显式报错）

### stamp 缓存键

`dist/.nuitka_compile_stamp` = `{nuitka_version}|{py_version}|{src_fingerprint}`

三要素一致则跳过整个 Nuitka 阶段（含 ensure_env 与 compile_src）。

## 实现

详见 `iter-37-Nuitka环境自动就绪与版本锁定.md`。

- `config.py` 新增 `NUITKA_VERSIONS`/`DEFAULT_NUITKA_VERSION`/`nuitka_version_for()`
- `exceptions.py` 新增 `NuitkaError`
- `packaging/nuitka.py` 新增 `ensure_env()`/`compile_with_stamp()`/`_check_c_compiler()`/`_stamp_path()`/`_stamp_key()`
- `builder.py` Nuitka 阶段调用 `compile_with_stamp()` 替代直接 `compile_src()`
