# iter-130 错误恢复与自愈

## 需求清单

- [x] dist 半成品检测：构建开始前检测 dist 含残留产物但无 stamp 时 warning 提示 `fsp c`（req-49 阶段 4）
- [x] wheel 下载失败清理：pip download 失败时清理本次部分下载的 .whl，避免半成品污染缓存（req-49 阶段 4）
- [x] Nuitka .build 残留清理：compile_src 将 `_cleanup_build_dirs` 移入 finally，编译异常时也清理；补测试（req-49 阶段 4）
- [x] runtime 解压失败删损坏归档：extract_archive 解压失败时删除损坏归档，避免缓存反复命中损坏文件（req-49 阶段 4）

## 迭代目标

增强构建流程中网络/磁盘错误的恢复能力，覆盖四项错误恢复场景：
1. dist 半成品检测——上次构建中断/失败留下残留产物时，构建开始前 warning 提示用户清理
2. wheel 下载失败清理——pip download 部分下载后失败时，清理半成品 wheel 避免缓存污染
3. Nuitka .build 残留清理——编译异常时 finally 块确保 .build 目录被清理
4. runtime 解压失败删损坏归档——解压失败时删除损坏归档，下次构建重新下载

## 改动文件清单

### 源码
- `src/fspack/packaging/pipeline/__init__.py`：
  - 新增常量 `_PYC_STAMP = ".pyc_stamp"`、`_NUITKA_STAMP = ".nuitka_compile_stamp"`
  - 新增函数 `_warn_dist_incomplete(dist_dir)`：检测 dist 含产物但无 stamp 时 warning
  - `build` 函数在 `setup_log_file` 前调 `_warn_dist_incomplete(dist)`
- `src/fspack/packaging/wheels/downloader.py`：
  - 新增函数 `_cleanup_partial_wheels(cache_dir, before)`：删除本次新增的部分 wheel
  - `download_wheels` 用 try/except DependencyError 包裹 `_run_pip_download`，失败时调 `_cleanup_partial_wheels` 后 re-raise
- `src/fspack/packaging/wheels/__init__.py`：re-export `_cleanup_partial_wheels`
- `src/fspack/packaging/nuitka/compile.py`：
  - `compile_src` 将 `cls._cleanup_build_dirs(src_dir)` 从顺序执行移入外层 finally 块
  - 重构为嵌套 try/finally：内层清理 bootstrap_script，外层清理 .build 残留
- `src/fspack/packaging/runtime.py`：
  - 新增函数 `_safe_unlink_archive(archive_path, label)`：删除损坏归档，OSError 仅告警
  - `EmbedRuntime.extract_archive` 在 BadZipFile 时调 `_safe_unlink_archive` 删除损坏 zip
  - `StandaloneRuntime.extract_archive` 在 TarError/OSError 时调 `_safe_unlink_archive` 删除损坏 tarball
  - 修复 `RuntimeDownloader.ensure` 的 pyrefly 注释（`# type: ignore[arg-type]` → `# pyrefly: ignore[bad-argument-type]`，iter-129 遗留）

### 测试
- `tests/test_builder.py`：
  - 导入 `_warn_dist_incomplete`
  - 新增 6 个测试：no_dist/empty_dist/only_nsi 不告警，artifacts_no_stamp 告警，有 pyc/nuitka stamp 不告警
- `tests/test_wheels.py`：
  - 导入 `_cleanup_partial_wheels`
  - 新增 4 个测试：pip_error_cleans_partial_wheels（集成）、cleanup_preserves_existing、cleanup_no_partial、cleanup_unlink_oserror_warns
- `tests/test_nuitka.py`：
  - 新增 2 个测试：failure_cleans_build_dirs（单文件失败清理 .build）、compile_files_exception_cleans_build_dirs（异常时 finally 清理 .build）
- `tests/test_runtime.py`：
  - 更新 test_extract_embed_bad_zip：增加 `assert not bad.exists()` 验证损坏 zip 被删除
  - 更新 test_extract_standalone_bad_tar：增加 `assert not bad.exists()` 验证损坏 tarball 被删除
  - 新增 2 个测试：embed_bad_zip_unlink_failure_warns、standalone_bad_tar_unlink_failure_warns（删除失败仅告警）

## 关键决策与依据

### 1. dist 半成品检测的判定条件
检测条件：dist 已存在 AND 含构建产物（子目录或 .exe 文件，排除 `installer.nsi`） AND 无任何 stamp 文件（`.pyc_stamp` 和 `.nuitka_compile_stamp` 均不存在）。

- 排除 `installer.nsi`：`clean_dist` 保留此文件便于重新打包分发，单独存在不构成半成品
- 检测子目录和 .exe：runtime/、src/、*.exe 是构建阶段产物，存在即说明构建已开始
- stamp 存在时不告警：stamp 文件说明上次构建至少完成到编译阶段，stamp 缓存机制可正确处理重复构建

### 2. wheel 部分下载清理的 before 集合
`download_wheels` 在调 `_run_pip_download` 前记录 `before = {f.name for f in cache_dir.glob("*.whl")}`。失败时 `_cleanup_partial_wheels` 删除 `cache_dir` 中不在 `before` 集合中的 wheel。

- 保留下载前已存在的 wheel：其他项目可能共享缓存目录，不应误删
- 删除本次新增的 wheel：pip 可能下载部分 wheel 后失败，残留半成品会被 `--no-index` 离线解析错误命中

### 3. compile_src 的 finally 块结构
重构为嵌套 try/finally：
```python
try:
    try:
        compiled_files, failed = cls._compile_files(...)
    finally:
        shutil.rmtree(bootstrap_script.parent, ignore_errors=True)
    # 后处理（仅 _compile_files 正常返回时执行）
    stripped = cls._strip_compiled_sources(...)
finally:
    cls._cleanup_build_dirs(src_dir)  # 无论成功/失败/异常都清理
```

- 内层 finally 清理 bootstrap_script（与原逻辑一致）
- 外层 finally 清理 .build 残留（新增：确保异常时也清理）
- 后处理（_strip_compiled_sources）仅在 _compile_files 正常返回时执行，异常时跳过（compiled_files 未定义）

### 4. 损坏归档删除策略
`extract_archive` 解压失败时调 `_safe_unlink_archive` 删除损坏归档：
- `BadZipFile`/`TarError` 明确指示归档损坏，删除后下次构建重新下载
- `_safe_unlink_archive` 的 OSError 仅告警不抛：删除失败仍抛 EmbedError 让上层处理，避免删除失败掩盖解压失败
- 与 iter-128 `_safe_unlink`（hash 索引清理）策略一致：内容损坏删文件，I/O 错误不删

### 5. pyrefly 注释修复
`RuntimeDownloader.ensure` 的 `# type: ignore[arg-type]` 是 mypy 语法，pyrefly 不识别。改为 `# pyrefly: ignore[bad-argument-type]`。此错误是 iter-129 遗留（iter-129 doc 声称 0 errors 但实际 pyrefly 报 1 error），iter-130 顺手修复。

## 代码实现情况

### dist 半成品检测
```python
def _warn_dist_incomplete(dist_dir: Path) -> None:
    if not dist_dir.is_dir():
        return
    has_artifacts = any(
        p.name != _KEEP_NSI and (p.is_dir() or p.suffix == ".exe") for p in dist_dir.iterdir()
    )
    if not has_artifacts:
        return
    if (dist_dir / _PYC_STAMP).is_file() or (dist_dir / _NUITKA_STAMP).is_file():
        return
    _logger.warning(
        "dist 目录含上次构建的残留产物但缺少 stamp 文件: %s，"
        "可能为中断/失败的构建。建议执行 `fsp c` 清理后重新构建，避免残留文件干扰。",
        dist_dir,
    )
```

### wheel 部分下载清理
```python
try:
    result = _run_pip_download(...)
except DependencyError:
    _cleanup_partial_wheels(cache_dir, before)
    raise
```

### compile_src finally 块
```python
try:
    try:
        compiled_files, failed = cls._compile_files(...)
    finally:
        shutil.rmtree(bootstrap_script.parent, ignore_errors=True)
    # 后处理...
finally:
    cls._cleanup_build_dirs(src_dir)
```

### 损坏归档删除
```python
except zipfile.BadZipFile as e:
    _safe_unlink_archive(archive_path, "embed zip")
    raise EmbedError(f"embed zip 损坏: {archive_path}") from e
```

## 整合优化情况

- `_safe_unlink_archive` 与 iter-129 `_safe_unlink` 策略一致（OSError 仅告警），但参数不同（含 label 用于日志），独立函数避免混淆
- `_cleanup_partial_wheels` 的 before 集合复用 `download_wheels` 已有的 `before` 变量，无需额外扫描
- `compile_src` 的嵌套 try/finally 结构清晰：内层管 bootstrap_script 生命周期，外层管 .build 残留清理
- dist 半成品检测的 stamp 文件名常量 `_PYC_STAMP`/`_NUITKA_STAMP` 与 `pyc.py`/`compile.py` 中的字面量一致

## 测试验证结果

- ruff check：0 errors
- ruff format：全部通过
- pyrefly check：0 errors（修复 iter-129 遗留的 1 error）
- pytest 全套：1993 passed, 12 skipped（比 iter-129 多 14 个新测试）
- coverage：95.62%（>= 95% 门禁，比 iter-129 的 95.60% 提升 0.02%）
- 守护测试 7 个：全部通过

新增测试 14 个：
- `_warn_dist_incomplete`：6 个（no_dist/empty_dist/only_nsi/artifacts_no_stamp/pyc_stamp/nuitka_stamp）
- `_cleanup_partial_wheels`：4 个（pip_error_cleans/preserves_existing/no_partial/unlink_oserror_warns）
- `compile_src` .build 清理：2 个（failure_cleans/compile_files_exception_cleans）
- 损坏归档删除失败告警：2 个（embed_bad_zip/standalone_bad_tar unlink_failure_warns）
- 更新已有测试：2 个（embed_bad_zip/standalone_bad_tar 增加 `assert not bad.exists()`）

## 遗留事项

无。iter-130 四项任务全部完成。req-49 阶段 4（错误恢复与自愈）5 项中已完成 5/5（iter-130 四项 + iter-129 hash 索引回退）。

## 下一轮计划

iter-131 起进入 req-49 阶段 5（性能优化与收尾）。重点：
1. 分析构建流水线性能瓶颈（profile 数据驱动）
2. 优化热路径（import 延迟、subprocess 启动、文件 I/O）
3. 补充性能基准测试（pytest-benchmark 回归门禁）
4. 收尾：req-49 完成状态汇总、memory 更新
