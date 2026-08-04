# iter-128 缓存健壮性

## 需求清单

- [x] `_load_deps_cache` 损坏时删除缓存文件（req-49 阶段 2）
- [x] `_precompile_pyc` 编译失败 `returncode != 0` 时不写 stamp
- [x] Nuitka stamp 写入用 `tempfile + rename` 原子化
- [x] `fsp doctor` 增加 `--check-cache` 子命令

## 迭代目标

提升缓存层健壮性，避免损坏的缓存文件与半写入的 stamp 文件导致构建跳过必要步骤：
1. 依赖解析缓存文件损坏（JSON 非法/结构错误/编码错误）时自动删除，避免下次构建重复触发损坏告警
2. compileall 失败（`returncode != 0`）时不写 pyc stamp，让下次构建重试（与 iter-127 超时分支一致的"失败不缓存"策略）
3. Nuitka stamp 写入原子化（tempfile + os.replace），避免 Ctrl+C 中断后 stamp 半写入被下次构建误读为有效缓存跳过编译
4. `fsp doctor --check-cache` 主动扫描缓存目录，发现并删除损坏文件

## 改动文件清单

### 源码
- `src/fspack/packaging/wheels/cache.py`：`_load_deps_cache` 区分内容损坏（删文件）与 OSError（保留），增加 dict/list 类型校验
- `src/fspack/packaging/pyc.py`：`_precompile_pyc` 在 `returncode != 0` 时 `return` 不写 stamp，更新 iter-127 注释
- `src/fspack/packaging/nuitka/compile.py`：新增 `_atomic_write_text` 模块级函数（tempfile.mkstemp + Path.replace + contextlib.suppress 清理），`compile_with_stamp` 末尾改用它
- `src/fspack/doctor_envs.py`：新增 `_check_cache_integrity(cache_dir)` 扫描 `.deps-*.json` 并删除损坏文件，返回 `CheckResult`
- `src/fspack/cli_doctor.py`：导入并导出 `_check_cache_integrity`，新增 `run_doctor_cache_check()` 渲染表格并返回 `CheckResult`
- `src/fspack/cli_parser.py`：`_add_doctor_subparser` 增加 `--check-cache` 参数
- `src/fspack/cli.py`：`_run_doctor` 在 `--check-cache` 时调 `run_doctor_cache_check()`，可与 `--test`/`--bench` 组合

### 测试
- `tests/test_wheels.py`：更新 `test_load_deps_cache_handles_corrupt_json` 断言文件被删；新增 5 个测试（非 dict JSON、wheels 类型错误、非法 UTF-8、OSError 保留文件）
- `tests/test_builder.py`：`test_precompile_pyc_compileall_failure_warns_not_raises` 增加断言 stamp 未写入
- `tests/test_nuitka.py`：更新 `test_compile_with_stamp_write_oserror_warns` 改用 patch `_atomic_write_text`；新增 5 个 `_atomic_write_text` 直测（创建/覆盖/无残留/replace 失败清理/创建父目录）+ 1 个 `returncode != 0` 不写 stamp 测试
- `tests/test_cli_doctor.py`：新增 8 个 `_check_cache_integrity` 测试（目录不存在/无文件/全有效/JSON 损坏删除/非 dict/类型错误/多文件预览/OSError 跳过）+ 1 个 `run_doctor_cache_check` 渲染测试 + 1 个 CLI `--check-cache` 分发测试

## 关键决策与依据

### 1. 区分内容损坏与 OSError
`_load_deps_cache` 的 except 分支拆分：
- `(json.JSONDecodeError, ValueError)` → 删除文件（内容明确损坏，下次构建重新生成）
- `OSError` → 不删除（可能是瞬时权限/磁盘 I/O 问题，删除反而误伤可恢复的缓存）

`UnicodeDecodeError` 是 `ValueError` 子类，被内容损坏分支捕获并删除文件——这是正确行为，因为编码错误的缓存无法恢复。

### 2. dict/list 类型校验
原代码 `data.get("wheels", [])` 在 `data` 非 dict 时抛 `AttributeError` 未被捕获。新增 `isinstance(data, dict)` 与 `isinstance(names, list)` 校验，类型不符时抛 `ValueError` 进入删除分支。

### 3. "失败不缓存"策略统一
iter-127 引入 compileall 超时不写 stamp，iter-128 扩展到 `returncode != 0`。两者一致：编译失败时让下次构建重试，避免失败的编译被 stamp 跳过导致用户长期运行未编译的 .py。

### 4. 原子写入用 Path.replace 而非 os.replace
`tempfile.mkstemp` 返回 str 路径，转为 `Path` 后用 `Path.replace(target)`（等价 `os.replace`，POSIX rename(2) 与 Windows ReplaceFile 均原子）。失败时用 `contextlib.suppress(OSError)` 清理临时文件。

### 5. `--check-cache` 与 `--test`/`--bench` 正交
`--check-cache` 作为独立步骤在 `--test`/`--bench` 之前执行，可组合使用（如 `doctor --check-cache --test`）。这与 iter-128 的缓存健壮性主题一致：先修复缓存再跑模板测试。

## 代码实现情况

### `_load_deps_cache` 损坏删除
```python
except (json.JSONDecodeError, ValueError) as e:
    _logger.warning("依赖解析缓存损坏，删除并重新解析: %s: %s", cache_file, e)
    try:
        cache_file.unlink()
    except OSError as unlink_err:
        _logger.warning("删除损坏缓存文件失败: %s: %s", cache_file, unlink_err)
except OSError as e:
    _logger.warning("读取依赖解析缓存失败，将重新解析: %s: %s", cache_file, e)
```

### `_atomic_write_text` 原子写入
```python
def _atomic_write_text(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=target.parent, prefix=".tmp_", suffix=target.suffix)
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(content)
        tmp_path.replace(target)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
```

### `_check_cache_integrity` 扫描诊断
遍历 `.deps-*.json`，对每个文件做与 `_load_deps_cache` 相同的 JSON 结构校验，损坏文件删除并计入 `corrupt_names`。OSError 不计为损坏（与 `_load_deps_cache` 一致）。详情列前 3 个损坏文件名 + 总数。

## 整合优化情况

- `_check_cache_integrity` 的 JSON 校验逻辑与 `_load_deps_cache` 保持一致，确保诊断与运行时行为统一
- `_atomic_write_text` 是模块级函数，未来 pyc stamp 或其他 stamp 写入也可复用（本次仅 Nuitka stamp 用）
- `run_doctor_cache_check` 复用 `_build_table` 渲染，与 `run_doctor` 报告风格一致

## 测试验证结果

- ruff check：0 errors
- ruff format：全部通过
- pyrefly check：0 errors
- pytest 全套：1964 passed, 12 skipped
- coverage：95.56%（>= 95% 门禁）
- 守护测试 7 个：全部通过

## 遗留事项

无。iter-128 四项任务全部完成。

## 下一轮计划

iter-129 内容 hash 回退（req-49 阶段 3）：当 stamp 未命中或损坏时，用内容 hash 作为回退缓存键，避免完全重新编译。重点：
1. 分析现有 stamp 命中失败后的回退路径
2. 设计内容 hash 缓存层（src 指纹已存在，可复用）
3. 实现 hash 回退逻辑
4. 测试覆盖 hash 命中/未命中/损坏场景
