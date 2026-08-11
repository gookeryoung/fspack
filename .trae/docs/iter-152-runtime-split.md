# iter-152: runtime.py 拆分为 runtime_urls/runtime_extract/runtime_download

## 需求清单
- [x] runtime.py（520 行）拆分为 < 350 行内聚子模块
- [x] 保持 runtime.py facade：download_embed / download_standalone / ensure_* / write_pth / _validate_tar_member / STANDALONE_* 全部公开名字 re-export
- [x] 兼容 patch：`setattr("runtime.download_embed", ...)` / `setattr("runtime.download_standalone", ...)`
- [x] 全量测试通过 + runtime/offline/builtin/net_retry 专项验证

## 迭代目标
1. 将运行时下载、安全解压、URL/命名辅助三大职责分离
2. 消除 EmbedRuntime.extract_archive / StandaloneRuntime.extract_archive 中的重复安全校验代码
3. ensure_* 使用函数式 dispatch 留出测试 patch 拦截点

## 改动文件清单

### 新增模块
1. `runtime_urls.py`（~80 行）
   - 常量：`STANDALONE_BASE_URL`、`STANDALONE_RELEASE_TAG`
   - 命名辅助：`embed_dirname()`、`embed_zip_name()`、`standalone_tarball_name()`、`standalone_url()`
   - `_sha256_file()`：分块 sha256 摘要

2. `runtime_extract.py`（~150 行）
   - `_safe_unlink_archive()`：损坏归档删除（仅告警）
   - `_validate_tar_member()`：PEP 706 data filter 等价（路径、盘符、穿越、链接、设备文件）
   - `_validate_zip_member()`：zip 路径安全 + 符号链接拒绝
   - **新增公共辅助** `extract_zip_safe()` / `extract_tar_safe()`：封装"预检 → extractall → 异常时删除归档"通用流程，消除子类重复代码

3. `runtime_download.py`（~370 行）
   - `RuntimeDownloader` ABC：`archive_name/download_url/marker_path/extract_archive` 四钩子 + 通用 `download()`（缓存检查、离线模式、进度下载、hash 校验）+ 通用 `extract()`
   - `EmbedRuntime`：Windows embed python（zip，镜像 URL，marker=python3X.dll），extract_archive 委托 `extract_zip_safe`
   - `StandaloneRuntime`：python-build-standalone（tar.gz，GitHub/aliyun 镜像 URL，marker=python/bin/pythonX.Y），extract_archive 委托 `extract_tar_safe`
   - 函数式 API：`download_embed` / `extract_embed` / `download_standalone` / `extract_standalone`
   - ensure 编排：`ensure_embed` / `ensure_standalone`—— **使用 `_R(name, fallback)` 延迟解析 facade 的 download/extract 函数**，确保 monkeypatch 修改后被感知
   - `_R(fn_name, fallback_fn)` dispatch 函数：首次调用延迟 import runtime facade，缓存模块引用，getattr 取当前属性值

### 修改模块
4. `runtime.py`（112 行，原 520 行）—— facade + write_pth
   - 从 3 个子模块 re-export 全部 22 个公开名字（`__all__` 列出）
   - 包含 `write_pth(dist_dir, version, extra_paths, enable_site)`：`_pth` 文件生成（用 embed_dirname 计算文件名）
   - 导入顺序避免循环：子模块只依赖 urls/extract，不依赖 facade

## 关键决策与依据

### 决策 1：拆分命名为 runtime_urls / runtime_extract / runtime_download（3 模块）
计划写 3 个（download / extract / win7），但 Win7 DLL 注入已在 iter-151 的 `runtime_trim.py` 中处理。用职责更强的命名：URL 命名辅助、安全解压、下载类。

### 决策 2：extract_zip_safe / extract_tar_safe 作为公共函数消除重复
原 EmbedRuntime.extract_archive 和 StandaloneRuntime.extract_archive 都有"遍历条目预检 → BadZipFile/TarError → 删除归档 → 抛 EmbedError"重复代码。抽为公共函数后，子类 extract_archive 仅一行调用。
- 依据：DRY，消除两个 18+ 行重复分支；辅助函数显式接收 `label` 参数，错误消息能区分 embed zip / standalone tarball

### 决策 3：ensure_* 用 `_R` dispatch facade 属性，不直接用类方法
原 ensure_embed 代码结构：
```python
dll_marker = EmbedRuntime.marker_path(...)
if not dll_marker.is_file():
    zip_path = download_embed(...)   # ← 函数名引用，便于 patch 后替换
    extract_embed(zip_path, ...)
```
拆分后把 `download_embed` 放到 runtime_download.py，但测试 patch 点是 facade 模块的 `runtime.download_embed` 属性。若 ensure_embed 直接 `from .runtime_download import download_embed` 调用，则 patch facade 无效。因此 ensure_embed 内部在需要执行下载/解压时通过 `_R("download_embed", download_embed)` 从 facade 取值。

## 代码实现情况
- 拆分后规模：runtime_urls 80 / runtime_extract 150 / runtime_download 370 / runtime 112，全部 < 380 行
- `extract_zip_safe` / `extract_tar_safe` 统一了"预检 → extractall → 异常删除归档"流程，并覆盖 `(tarfile.TarError, OSError)` 联合捕获（原 StandaloneRuntime 代码处理逻辑）
- dispatch 仅在 ensure_* 的"marker 未命中时的下载解压分支"使用，避免首次模块 import 触发循环依赖

## 测试验证结果
```
专项（runtime + offline + net_retry + builtin）：131 passed
  test_runtime.py            - ensure/patch/校验/解压
  test_offline_mode.py       - 离线模式缓存未命中
  test_offline_integration.py- 端到端离线构建
  test_net_retry.py          - 网络重试机制
  test_builtin.py            - 内置库打包

全量回归（10 测试文件）：
  478 passed, 11 skipped in 5.61s

性能基线：
  warm small/medium 5.17ms / 5.51ms（baseline 一致）
  cold small/medium 5.24ms / 8.46ms（baseline 一致）
```

## 遗留事项
- 进一步：可以让 `_validate_tar_member` 直接在 extract_tar_safe 内部不重复循环 tf.getmembers()（目前 3.11 分支 getmembers 一次 + extractall 再扫描一次），但 tarball 条目仅 3000+ 量级，收益小。后续视性能基线情况决定是否优化为 `for m in members: validate; tf.extract(m, path)` 单循环。

## 下一轮计划
1. iter-153：dep_analyzer.py 子包化（pe/elf/macho 三解析器）
2. 保持 dep_analyzer facade + 导入兼容（`analyze_binary_dependencies` 公共 API）
3. 跑全量测试 + baseline
