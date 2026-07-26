# iter-44 standalone python Win7 兼容性 DLL 注入

## 需求清单

- [x] 在 `_ensure_build_python` 返回 standalone python 路径前注入 Win7 兼容 DLL
- [x] 缓存命中与新建分支均注入
- [x] Linux 分支不触发注入
- [x] 测试覆盖三个场景
- [x] 全套门禁通过

## 迭代目标

修复 fspack 自身打包后在 Win7 上无法运行 nuitka 编译的问题。standalone python 启动需 `api-ms-win-core-path-l1-1-0.dll`，fspack 之前仅在 embed runtime 注入此 DLL，未在 standalone python 缓存目录注入。

## 改动文件清单

- [src/fspack/packaging/nuitka.py](../../src/fspack/packaging/nuitka.py)：`_ensure_build_python` 重构缓存命中分支为 if/else，函数末尾惰性导入并调用 `_inject_win7_compat_dll` 注入 DLL 到 `py_exe.parent`
- [tests/test_nuitka.py](../../tests/test_nuitka.py)：新增 3 个测试用例

## 关键决策与依据

### 1. 复用 builder._inject_win7_compat_dll 而非提取独立模块

**决策**：在 `nuitka.py` 函数体内惰性导入 `from fspack.builder import _inject_win7_compat_dll`。

**依据**：
- 函数本身已是通用工具（接受任意 `runtime_dir` 参数），docstring 描述的是典型用例
- assets 源 DLL 路径用 `Path(__file__).parent / "assets" / "runtime"`，依赖 builder.py 在 `src/fspack/` 层级；提取到 `packaging/win7.py` 需调整路径，改动面扩大
- 惰性导入避免顶层循环依赖（builder.py 仅在函数体内惰性导入 nuitka.py）
- 不破坏现有 test_builder.py 的 monkeypatch（`fspack.builder._WIN7_COMPAT_DLL_NAME`）

### 2. 重构缓存命中分支为 if/else 统一注入点

**决策**：将原 `if py_exe.is_file(): ... return py_exe`（早返回）改为 `if/else`，函数末尾统一调用注入。

**依据**：
- 缓存命中与新建分支均需注入（覆盖用户清理过 DLL 但保留 python.exe 的场景）
- `_inject_win7_compat_dll` 幂等：DLL 已存在则跳过，缺失则补充
- 统一注入点避免两处重复调用

### 3. KNOWN_STANDALONE_VERSIONS 最低 3.10，无需版本守卫

**决策**：不调用 `_needs_win7_compat_dll` 判断，直接注入。

**依据**：
- KNOWN_STANDALONE_VERSIONS 最低 3.10（`"3.10": "3.10.20"`）
- `_needs_win7_compat_dll` 对 3.9+ 返回 True，故所有 standalone python 版本都需要此 DLL
- 注入函数本身幂等且源 DLL 缺失时仅 warning 不报错，向后兼容

## 代码实现情况

### nuitka.py 修改

```python
if py_exe.is_file():
    _logger.info(...)
    stage.hit_cache()
    stage.set_detail(...)
else:
    archive_path = cls._download_standalone_python(...)
    cls._extract_standalone_python(...)
    if not py_exe.is_file():
        raise NuitkaError(...)
    stage.set_detail(...)

# Win7 兼容性：Python 3.9+ 官方不再支持 Win7，standalone python 启动需
# api-ms-win-core-path-l1-1-0.dll（与 embed runtime 同样需要）。复用 builder
# 的注入逻辑：惰性导入避免 nuitka → builder 顶层循环依赖（builder 函数体内
# 才惰性导入 nuitka）。注入幂等，缓存命中与新建均安全。
# KNOWN_STANDALONE_VERSIONS 最低 3.10，故 standalone python 始终需要此 DLL。
from fspack.builder import _inject_win7_compat_dll

_inject_win7_compat_dll(py_exe.parent)
return py_exe
```

### 测试新增

- `test_ensure_build_python_injects_win7_compat_dll_on_extract`：解压成功后 standalone python 目录有 DLL（断言文件存在且非空）
- `test_ensure_build_python_injects_win7_compat_dll_on_cache_hit`：缓存命中时补充注入 DLL（模拟用户清理过 DLL 但保留 python.exe）
- `test_ensure_build_python_skips_win7_compat_dll_for_linux`：Linux 分支早返回，不创建缓存目录也不注入

## 整合优化情况

- 复用现有 `_inject_win7_compat_dll`，无重复代码
- 不新增模块文件，避免 assets 路径调整
- 不破坏现有 test_builder.py 的 monkeypatch 测试

## 测试验证结果

```
uv run ruff check src tests          → All checks passed!
uv run ruff format --check src tests → 47 files already formatted
uv run pyrefly check                 → 0 errors (70 suppressed, 7 warnings not shown)
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95
  → 941 passed, 21 deselected in 6.43s
  → coverage: 97.03%
```

nuitka.py 模块覆盖率 96%（原 96%，未下降）。

## 遗留事项

无。

## 下一轮计划

无（修复完成，等待用户验证 Win7 实机运行）。
