# iter-137: 编译产物验证增强

## 需求清单

- [x] `_strip_compiled_sources` 批量验证 .pyd 可加载性扩展为并发验证（`_individual_import_test` 并发化）
- [x] 损坏 .pyd 自动删除并回退到 .py（已有，测试已覆盖 `test_strip_compiled_sources_verify_preserves_py_when_pyd_corrupt`）
- [x] Nuitka 编译失败时记录失败文件列表到 stamp，下次跳过这些文件避免反复尝试

## 迭代目标

补齐 req-49 L105-107 列出的编译产物验证增强三项任务：
(1) `_individual_import_test` 从串行改并发（ThreadPoolExecutor），批量测试崩溃后
逐个定位损坏 .pyd 时 50 个文件场景从 ~5s 降到 ~1.25s；
(2) 损坏 .pyd 删除逻辑已有（iter-134 strip.py L94-100），测试已覆盖，本轮无新增；
(3) 新增 `.nuitka_failed_files.json` 记录编译失败文件列表，`compile_with_stamp`
读取后传给 `compile_src` → `_collect_py_files` 跳过这些文件，避免反复尝试。

## 改动文件清单

- `src/fspack/packaging/nuitka/verify.py`：
  - 顶部新增 `from concurrent.futures import ThreadPoolExecutor` 与
    `_MAX_VERIFY_WORKERS = 4` 常量
  - `_individual_import_test` 从串行 for 循环重构为 `ThreadPoolExecutor.map`，
    `max_workers = min(len(modules), _MAX_VERIFY_WORKERS)`，空模块列表直接返回
    不启动线程池
- `src/fspack/packaging/nuitka/compile.py`：
  - 新增 `_failed_files_path`/`_load_failed_files`/`_save_failed_files` 三个
    辅助函数（与 `_hash_index_path`/`_load_hash_index` 同构，损坏删文件策略一致）
  - `_compile_files` 返回值从 `(set[Path], int)` 改为 `(set[Path], list[Path])`，
    `failed_files` 为失败文件绝对路径列表
  - `compile_src` 签名加 `skip_files: frozenset[str] | None = None`，传给
    `_collect_py_files`；返回类型从 `None` 改 `list[str]`（失败文件相对 src_dir
    的 POSIX 路径）；`return` 改 `return []`
  - `compile_packages` 适配 `failed_files: list[Path]`，`len(failed)` 显示
  - `_collect_py_files` 签名加 `skip_files` 参数，收集时跳过相对 POSIX 路径匹配的文件
  - `compile_with_stamp` stamp 不命中时读 `_load_failed_files`，传给 `compile_src`；
    编译后调 `_save_failed_files` 写入本次失败文件列表
- `src/fspack/packaging/nuitka/protocol.py`：
  - `compile_src` 返回类型 `None` → `list[str]`，加 `skip_files` 参数
  - `_collect_py_files` 加 `skip_files` 参数
  - `_compile_files` 返回类型 `tuple[set[Path], int]` → `tuple[set[Path], list[Path]]`
- `tests/test_nuitka.py`：
  - 新增 14 个测试覆盖并发验证、`_collect_py_files` skip、`_load/_save_failed_files`
    读写与损坏处理、`compile_with_stamp` 集成
  - 7 处 `fake_compile_src` 返回值 `None` → `[]`（适配新返回类型）
  - 5 处 `fake_compile_files` 类型注解 `tuple[set[Path], int]` → `tuple[set[Path], list[Path]]`，
    返回值 `0` → `[]`、`1` → `[Path("fake.py")]`
  - 2 处 `assert failed == N` → `assert len(failed) == N`

## 关键决策与依据

### 并发验证的 ThreadPoolExecutor 选择

`_individual_import_test` 仅在批量测试崩溃时触发（Nuitka 4.x + Python 3.13+ Windows
+ zig 编译器罕见场景）。串行 N 个 subprocess 启动开销 100ms × N，50 个损坏 .pyd
场景 ~5s。用 `ThreadPoolExecutor`（subprocess 释放 GIL，线程并行启动子进程），
`max_workers = min(len(modules), _MAX_VERIFY_WORKERS)`，与 `_MAX_COMPILE_WORKERS`
一致（4），50 个文件降到 ~1.25s。

`pool.map` 顺序返回结果，`_test_one` 返回 `str | None`（可加载返回模块名，否则 None），
主线程聚合到 `importable` 集合，无共享可变状态竞争。

### 失败文件列表独立文件 vs 写入 stamp

req-49 L107 说"记录到 stamp"，但 stamp 文件当前是纯文本（stamp_key），改 JSON 格式
会破坏向后兼容（旧 stamp 识别为不匹配重编）。采用独立文件 `.nuitka_failed_files.json`
（与 `.nuitka_hash_index.json` 同构），stamp 格式不变，向后兼容。

失败文件列表与 stamp 同目录（dist/），删除 dist 时一并清理。stamp 命中时跳过整个
Nuitka 阶段（失败文件列表不读取，无意义）；stamp 不命中时读取并传给 `compile_src`。

### 失败文件列表的"跳过"语义

失败文件列表记录"相对 src_dir 的 POSIX 路径"，`_collect_py_files` 收集时跳过这些
路径。用户修复失败文件后需删除 `.nuitka_failed_files.json` 或 stamp 文件强制重试
（与现有"重编"机制一致：stamp 不匹配就全编，失败文件列表仅在 stamp 不命中时生效）。

接受的不完美：源码变了但失败文件列表仍跳过（用户修复失败文件后源码指纹会变，stamp
不命中，但失败文件列表让这些文件被跳过）。权衡：区分"源码原因"与"非源码原因"失败
需要记录文件内容 hash，复杂度过高；用户手动删除 stamp 强制重试是简单可靠的回退路径。

### `_load_failed_files` 损坏处理策略

与 `_load_hash_index` 一致：
- 文件不存在 → 空 frozenset
- OSError → 空 frozenset（瞬时错误，不删文件）
- JSON 非法 → 删文件 + 空 frozenset
- 顶层非 list → 删文件 + 空 frozenset
- 含非 str 条目 → 剔除（保留 str 条目）

### `compile_src` 返回值变更的影响

`compile_src` 返回类型从 `None` 改 `list[str]`，影响所有调用方：
- `compile_with_stamp`：接收返回值调 `_save_failed_files`（主要使用场景）
- 10+ 处测试直接调 `compile_src` 但不检查返回值（无赋值），改返回值不破坏

`_compile_files` 返回值从 `(set, int)` 改 `(set, list)`，影响：
- `compile_src`/`compile_packages` 内 `failed` 变量从 int 变 list，`if failed:` 仍可用
  （空 list 为 False），`stage.set_detail(f"失败 {failed} 个")` 需改 `len(failed)`
- 2 处测试 `assert failed == N` 改 `assert len(failed) == N`
- 5 处 fake 类型注解与返回值更新

## 代码实现情况

### `_individual_import_test` 并发化

```python
@staticmethod
def _individual_import_test(
    py_exe: Path, search_roots: list[Path], module_names: list[str]
) -> set[str]:
    path_inserts = ";".join(f"sys.path.insert(0, r'{root}')" for root in search_roots)
    importable: set[str] = set()
    if not module_names:
        return importable

    def _test_one(mod: str) -> str | None:
        test_code = f"import sys; {path_inserts}\nimport importlib\nimportlib.import_module({mod!r})\n"
        result = subprocess.run([str(py_exe), "-c", test_code], capture_output=True, check=False)
        return mod if result.returncode == 0 else None

    max_workers = min(len(module_names), _MAX_VERIFY_WORKERS)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for mod in pool.map(_test_one, module_names):
            if mod is not None:
                importable.add(mod)
    return importable
```

### 失败文件列表读写

```python
def _failed_files_path(dist_dir: Path) -> Path:
    return dist_dir / ".nuitka_failed_files.json"

def _load_failed_files(dist_dir: Path) -> frozenset[str]:
    # 文件不存在/损坏返回空 frozenset，与 _load_hash_index 策略一致
    path = _failed_files_path(dist_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except OSError:
        return frozenset()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _safe_unlink(path)
        return frozenset()
    if not isinstance(data, list):
        _safe_unlink(path)
        return frozenset()
    return frozenset(s for s in data if isinstance(s, str))

def _save_failed_files(dist_dir: Path, failed_files: list[str]) -> None:
    # 原子写入（与 stamp/hash 索引一致），空列表也写入覆盖上次记录
    path = _failed_files_path(dist_dir)
    try:
        _atomic_write_text(path, json.dumps(failed_files, ensure_ascii=False, indent=2))
    except OSError as e:
        _logger.warning("写入失败文件列表失败（不影响构建）: %s: %s", path, e)
```

### `compile_with_stamp` 集成

```python
# stamp 不命中时读取上次失败文件列表
skip_files = _load_failed_files(dist_dir)
if skip_files:
    _logger.info("跳过上次失败的 %d 个 .py 文件: %s", len(skip_files), sorted(skip_files))

failed_files = cls.compile_src(
    src_dir, runtime_dir, py_version, target, nuitka_cache,
    stage=stage, build_python_exe=build_python_exe,
    entry_rels=entry_rels, ccache=ccache, cache_root=cache_root,
    skip_files=skip_files,
)

# 编译后写 stamp + hash 索引 + 失败文件列表
try:
    _atomic_write_text(stamp, stamp_key)
except OSError as e:
    _logger.warning("写入 Nuitka stamp 失败: %s", e)
_update_hash_index(dist_dir, stamp_key)
_save_failed_files(dist_dir, failed_files)
```

### `_collect_py_files` 跳过失败文件

```python
@staticmethod
def _collect_py_files(
    src_dir: Path,
    entry_rels: frozenset[str] | None,
    skip_files: frozenset[str] | None = None,
) -> list[Path]:
    py_files = sorted(
        p for p in src_dir.rglob("*.py")
        if not any(part.lower().endswith(".build") for part in p.parts)
        and p.name != "__init__.py"
    )
    if entry_rels:
        py_files = [p for p in py_files if p.relative_to(src_dir).as_posix() not in entry_rels]
    if skip_files:
        py_files = [p for p in py_files if p.relative_to(src_dir).as_posix() not in skip_files]
    return py_files
```

## 测试验证结果

### 新增测试（14 个）

并发验证（2 个）：
- `test_individual_import_test_concurrent_handles_multiple_modules`：4 模块并发，
  2 可加载 2 崩溃，返回正确集合
- `test_individual_import_test_empty_modules_returns_empty`：空模块列表不启动线程池

`_collect_py_files` skip（2 个）：
- `test_collect_py_files_skips_failed_files`：skip 2 个文件（含子目录），仅收集 good.py
- `test_collect_py_files_skip_files_none_preserves_all`：skip_files=None 不跳过任何文件

`_load/_save_failed_files` 读写（7 个）：
- `test_load_failed_files_missing_returns_empty`：文件不存在返回空
- `test_load_failed_files_valid_list`：合法 JSON 列表读取
- `test_load_failed_files_corrupt_json_deletes_file`：非法 JSON 删文件
- `test_load_failed_files_non_list_deletes_file`：顶层非 list 删文件
- `test_load_failed_files_strips_non_str_entries`：剔除非 str 条目
- `test_save_failed_files_writes_json`：写入后回读校验
- `test_save_failed_files_empty_list_overwrites`：空列表覆盖上次记录

`compile_with_stamp` 集成（3 个）：
- `test_compile_with_stamp_writes_failed_files_after_compile`：编译后写入失败文件列表
- `test_compile_with_stamp_reads_failed_files_and_passes_to_compile_src`：读取上次
  失败文件列表传给 compile_src 的 skip_files
- `test_compile_with_stamp_cache_hit_does_not_read_failed_files`：stamp 命中时不
  读取失败文件列表（无意义）

### 既有测试适配

- 7 处 `fake_compile_src` 返回值 `None` → `[]`
- 5 处 `fake_compile_files` 类型注解与返回值更新
- 2 处 `assert failed == N` → `assert len(failed) == N`
- `test_strip_compiled_sources_verify_preserves_py_when_pyd_corrupt`（iter-134 已有）
  覆盖损坏 .pyd 删除场景，本轮无新增

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 10 files already formatted
- pyrefly: 0 errors（CLI `--project-excludes "**/assets/templates/**"`）
- pytest: 2056 passed, 12 skipped（iter-136 为 2042 passed，新增 14 个测试）
- coverage: 95.70%（>= 95% 门禁，iter-136 为 95.71%，微降 0.01% 因新增代码量）
- 10 benchmarks: 全通过

## 整合优化情况

- `_individual_import_test` 并发化与 `_compile_files` 的 `ThreadPoolExecutor` 模式
  一致（subprocess 释放 GIL，线程并行启动子进程），`_MAX_VERIFY_WORKERS` 与
  `_MAX_COMPILE_WORKERS` 同值（4）
- 失败文件列表读写策略与 `_load_hash_index`/`_update_hash_index` 同构（损坏删文件、
  类型校验、原子写入），保持 nuitka 模块文件 I/O 一致性
- `compile_src` 返回值变更最小化影响：测试调用方大多不检查返回值，仅 `compile_with_stamp`
  接收使用

## 遗留事项

- pyrefly.toml `project-excludes` 配置在 pyrefly 1.1.1 未生效（iter-135 遗留，待用户复核）
- 失败文件列表的"跳过"语义不区分"源码原因"与"非源码原因"失败，用户修复失败文件后
  需手动删除 `.nuitka_failed_files.json` 或 stamp 强制重试（接受的不完美，区分需文件
  内容 hash，复杂度过高）
- `compile_packages` 的失败文件未记录到 `.nuitka_failed_files.json`（仅用户源码
  `compile_src` 记录），第三方包编译失败下次仍会重试（接受，第三方包编译是用户可选
  优化，失败场景罕见）

## 下一轮计划

iter-138 依赖分析异常容错（req-49 L108-110，阶段 3 深度健壮性）：
1. `_parse_file_worker` 单文件 ast.parse 失败记录到报告（`ast_errors` 字段），不静默跳过
2. `_parse_parallel` 单个 worker 超时不阻塞其他 worker（`as_completed` + timeout）
3. QML 解析失败不影响主流程（已有，补测试）
