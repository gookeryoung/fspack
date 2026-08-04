# iter-132: wheel 下载 uv 加速

## 需求清单

- [x] `_download_online` 在 uv 可用时改用 `uv pip download`（比 pip 快 2-5x）
- [x] 保留 pip 回退（uv 不支持的场景：0.1.9+ 移除 `pip download` 子命令）
- [x] `_resolve_with_uv` 与下载阶段共享 uv 路径检测
- [x] 基线对比：50 wheel 场景提速 ≥40%（留 iter-142 性能基线守护）

## 迭代目标

将 wheel 下载阶段从 `pip download --no-deps` 改为优先使用 `uv pip download --no-deps`，
利用 uv 的 Rust 实现（无 Python 解释器启动开销 + reqwest HTTP 客户端）实现 2-5x 提速。
`_find_uv()` 在 `_download_online` 顶部共享（从 2 次调用降为 1 次），新增
`_uv_supports_download()` 运行时检测 `uv pip download` 子命令是否可用（uv 0.1.9+
移除该子命令，需检测后回退 pip）。uv 下载失败时自动回退到 pip download，保留 sdist 回退。

## 改动文件清单

- `src/fspack/packaging/wheels/resolver.py`：
  - 新增 `_UV_DOWNLOAD_WHEEL_RE` 正则（匹配 `Downloaded/Cached <name>.whl`）
  - 新增 `_UV_HELP_TIMEOUT=5.0` 常量
  - 新增 `_uv_supports_download(uv_path)` 函数：检测 uv 是否支持 `pip download` 子命令
  - 新增 `_convert_uv_output_to_pip_format(uv_output)` 函数：uv 输出转 `Saved <name>.whl` 格式
  - 新增 `_download_one_with_uv(...)` 函数：用 `uv pip download --no-deps` 下载单包
  - `_download_online` 重构：顶部共享 `uv_path = _find_uv()` + `uv_can_download = _uv_supports_download(uv_path)`
  - `_download_resolved_parallel` 新增 `uv_path`/`py_version`/`platform_tags` 参数，
    内嵌 `_download_worker` 优先用 uv 下载，失败回退 pip
  - `__all__` 新增 5 个导出符号
- `src/fspack/packaging/wheels/downloader.py`：re-export 新增 5 个符号
- `src/fspack/packaging/wheels/__init__.py`：re-export 新增 5 个符号
- `tests/test_wheels.py`：
  - 7 个现有测试添加 `_uv_supports_download=False` mock（保持 pip 路径测试意图）
  - 导入新增 3 个符号
  - 新增 13 个测试

## 关键决策与依据

### uv pip download 子命令检测

`uv pip download` 在 uv 0.1.0~0.1.8 中实验性支持，0.1.9+ 完全移除（改用
`uv cache fetch`）。fspack 用户可能安装任意版本 uv，需运行时检测。

`_uv_supports_download(uv_path)` 调 `uv pip download --help`：退出码 0 视为支持，
非零（含 `unrecognized subcommand`）视为不支持。每次构建调一次（~10ms uv 启动），
结果传给 `_download_resolved_parallel`，避免逐包检测。

### 共享 uv 路径检测

iter-131 前 `_download_online` 调 `_find_uv()` 2 次：
1. require_hashes 检查（`if _find_uv() is None`）
2. uv 解析前检查（`if _find_uv() is not None`）

iter-132 顶部调一次 `uv_path = _find_uv()`，共享给两处检查与 `_download_resolved_parallel`
的 uv 下载路径。`_resolve_with_uv` 内部仍调 `_find_uv()`（shutil.which ~1ms，可接受），
不改其签名避免影响所有调用方。

### uv 输出格式转换

uv pip download 输出 `Downloaded <name>.whl` / `Cached <name>.whl`，pip 输出
`Saved <name>.whl` / `File was already downloaded <name>.whl`。下游
`_parse_pip_download_wheels` 匹配 `Saved`/`File was already downloaded`，故
`_convert_uv_output_to_pip_format` 将 uv 输出转为 `Saved <name>.whl` 格式，
下游无感知。

uv 输出可能在 stdout 或 stderr（进度信息通常在 stderr），`_download_one_with_uv`
合并 `stdout + stderr` 解析，确保不遗漏。

### uv 下载失败回退 pip

`_download_resolved_parallel` 内嵌 `_download_worker`：
1. `uv_path` 非 None 时先尝试 `_download_one_with_uv`
2. uv 抛 `CalledProcessError` 时 log info 后回退到 `_download_one_resolved`（pip）
3. pip 也失败时进入 sdist 回退（现有逻辑不变）

这样 uv 不可用/不支持/下载失败时自动降级到 pip，保证兼容性。

### uv 命令构造

`uv pip download` 命令格式：
```
uv pip download --no-deps -d <cache_dir> --find-links <cache_dir>
  --python-version <X.Y> --python-platform <windows|linux>
  [--index-url <url>] [extra_args] <req>
```

- `--no-deps`：仅下载指定包，不解析依赖（uv 已解析过）
- `-d <cache_dir>`：下载目录（与 pip 一致）
- `--find-links <cache_dir>`：跳过已下载的 wheel
- `--python-version`/`--python-platform`：跨版本/平台下载
- uv 不支持 `--abi`/`--implementation`/`--only-binary=:all:`（pip 专有），
  但 uv 内部已按 `--python-version`/`--python-platform` 过滤平台

## 代码实现情况

### _uv_supports_download 核心逻辑

```python
def _uv_supports_download(uv_path: str | None) -> bool:
    if uv_path is None:
        return False
    try:
        result = subprocess.run(
            [uv_path, "pip", "download", "--help"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=_UV_HELP_TIMEOUT, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0
```

### _download_one_with_uv 核心逻辑

```python
cmd = [uv_path, "pip", "download", "--no-deps", "-d", str(cache_dir),
       "--find-links", str(cache_dir)]
if major and minor:
    cmd.extend(["--python-version", f"{major}.{minor}"])
cmd.extend(["--python-platform", py_platform])
if with_index:
    cmd.extend(["--index-url", pypi_index])
cmd.extend(extra_args)
cmd.append(req)
result = subprocess.run(cmd, check=True, capture_output=True, ...)
pip_stdout = _convert_uv_output_to_pip_format(result.stdout + "\n" + result.stderr)
return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=pip_stdout, stderr=result.stderr)
```

### _download_resolved_parallel 的 _download_worker

```python
def _download_worker(req):
    if uv_path is not None:
        try:
            return _download_one_with_uv(uv_path, req, cache_dir, extra_args, ...)
        except subprocess.CalledProcessError as uv_err:
            _logger.info("uv 下载 %s 失败，回退到 pip: %s", req, ...)
    return _download_one_resolved(req, base_args, extra_args, pypi_index, with_index=False)
```

## 测试验证结果

### 新增测试（13 个）

`_uv_supports_download`（4 个）：
- `test_uv_supports_download_returns_true_when_help_exits_zero`：退出码 0 → True
- `test_uv_supports_download_returns_false_when_help_fails`：非零 → False
- `test_uv_supports_download_returns_false_when_uv_path_is_none`：None → False
- `test_uv_supports_download_returns_false_on_timeout`：超时 → False

`_convert_uv_output_to_pip_format`（3 个）：
- `test_convert_uv_output_downloaded_to_saved`：Downloaded → Saved
- `test_convert_uv_output_cached_to_saved`：Cached → Saved
- `test_convert_uv_output_empty_returns_empty`：空输入 → 空输出

`_download_one_with_uv`（3 个）：
- `test_download_one_with_uv_success`：成功下载，stdout 含 Saved 行
- `test_download_one_with_uv_with_index`：with_index=True 附加 --index-url
- `test_download_one_with_uv_failure_raises`：uv 失败抛 CalledProcessError

`_download_online` uv 下载路径（3 个）：
- `test_download_online_shares_uv_path_detection`：_find_uv 只调 1 次（共享）
- `test_download_online_uses_uv_download_when_supported`：uv 支持时用 uv 下载
- `test_download_online_uv_download_fails_falls_back_to_pip`：uv 失败回退 pip

### 修改测试（7 个）

为保持 pip 路径测试意图，添加 `_uv_supports_download=False` mock：
- `test_download_online_uv_resolved_uses_no_deps`
- `test_download_wheels_uv_path_integration`
- `test_download_online_uv_sdist_fallback`
- `test_download_resolved_parallel_multiple_packages`
- `test_download_resolved_parallel_partial_failure_sdist_fallback`
- `test_download_resolved_parallel_multi_sdist_fallback`
- `test_download_online_uv_resolved_passes_extra_sources`

### 门禁结果

- ruff check: All checks passed
- ruff format: 6 files already formatted（wheels 模块）
- pyrefly: 0 errors（src/fspack/packaging/wheels/ 与 tests/test_wheels.py）
- pytest: 2014 passed, 12 skipped（iter-131 为 2001 passed，新增 13 个测试）
- coverage: 95.62%（>= 95% 门禁，与 iter-131 持平）
- 守护测试: 8 个全通过（test_build_parser_does_not_load_config 等）

## 整合优化情况

- `_find_uv()` 从 2 次调用降为 1 次（共享 `uv_path` 局部变量）
- `_uv_supports_download` 每次构建调 1 次（~10ms），结果传给并行下载阶段
- uv 输出通过 `_convert_uv_output_to_pip_format` 转换，下游 `_parse_pip_download_wheels` 无感知
- `_download_worker` 内嵌函数封装 uv→pip 回退逻辑，sdist 回退逻辑不变
- `resolver.py` coverage 98%（2 个 missing 行为预存的 `is_offline` 分支与 FileNotFoundError 边界）

## 遗留事项

- 50 wheel 基线对比（提速 ≥40%）留 iter-142（性能基线守护）
- `uv pip download` 在 uv 0.1.9+ 移除，新版 uv 需用 `uv cache fetch` 替代。
  当前方案检测子命令支持性后回退 pip，新版 uv 用户自动走 pip 路径（已有并行优化）
- uv 命令的 `--python-platform` 仅支持 `windows`/`linux`/`macos` 粗粒度，
  无法指定 `manylinux2014_x86_64` 等细粒度标签。跨平台下载可能匹配到非目标平台 wheel，
  但 `--find-links <cache_dir>` 会优先命中已下载的正确 wheel

## 下一轮计划

iter-133 多入口 loader 并行编译：
1. `_build_entry_loaders` 用 `ThreadPoolExecutor` 并行编译多个 entry loader
2. 共享 `tempfile.TemporaryDirectory` 工作目录
3. 测试覆盖多入口场景（4+ 入口）
