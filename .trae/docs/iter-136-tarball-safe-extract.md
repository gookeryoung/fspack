# iter-136: tarball 安全 extract 完整化

## 需求清单

- [x] `extract_standalone` 3.11 及以下用 `tarfile.open` + 手动 `data` filter（参考 PEP 706 backport）
- [x] `extract_embed` 校验 zip 条目路径无 `..` 与绝对路径
- [x] 测试覆盖恶意 tarball（路径穿越、符号链接攻击、硬链接、设备文件）与恶意 zip（路径穿越、绝对路径、Windows 盘符、符号链接）

## 迭代目标

补齐 req-49 L101-103 列出的归档解压安全加固：embed zip 与 standalone tarball
均从镜像站网络下载，存在镜像被篡改注入恶意条目的风险。Python 3.12+ 已通过
``tarfile.data_filter``（PEP 706）内置防护，但 3.11 及以下无 filter 参数；
zipfile 全版本均无内置安全过滤。本轮手动实现等价检查覆盖低版本 tar 与全版本 zip。

## 改动文件清单

- `src/fspack/packaging/runtime.py`：
  - 顶部新增 `import stat`（zip 符号链接模式位检测用）
  - 新增 `_validate_tar_member`：PEP 706 `data` filter 等价检查（绝对路径/盘符/
    路径穿越/符号链接/硬链接/设备文件）
  - 新增 `_validate_zip_member`：zip 条目路径安全检查（绝对路径/盘符/路径穿越/
    符号链接，通过 `external_attr >> 16` 取 Unix st_mode）
  - `EmbedRuntime.extract_archive`：解压前 `for info in zf.infolist()` 预检，
    恶意条目抛 EmbedError 并删除归档（新增 `except EmbedError` 分支）
  - `StandaloneRuntime.extract_archive`：3.11- 分支 `for member in tf.getmembers()`
    预检，恶意条目抛 EmbedError 并删除归档（新增 `except EmbedError` 分支）
- `tests/test_runtime.py`：
  - 新增 `_make_malicious_tar` 辅助函数（构造含 symlink/hardlink/char 设备文件
    条目的 tar.gz）
  - 新增 4 个 zip 恶意条目测试：路径穿越、绝对路径、Windows 盘符、符号链接
  - 新增 6 个 tar 恶意条目测试：路径穿越、绝对路径、Windows 盘符、符号链接、
    硬链接、字符设备文件

## 关键决策与依据

### 预检 vs extractall(filter="data") 的分工

- **3.12+**：信任 CPython 内置 `tarfile.data_filter`，直接 `extractall(filter="data")`
- **3.11-**：手动遍历 `tf.getmembers()` 预检，通过后 `extractall()` 无 filter

预检逻辑独立测试（测试环境为 3.11），3.12+ 的 `filter="data"` 分支标注
`pragma: no cover`（与 iter-130 既有约定一致）。这样测试环境能完整覆盖预检逻辑，
3.12+ 上信任 CPython 内置实现，避免重复造轮子。

### 恶意条目归档删除策略

恶意条目检测后归档一并删除（与"损坏归档"行为一致），理由：

1. 归档从镜像网络下载，镜像被篡改是临时情况，删除后下次重新下载可能恢复
2. 保留恶意归档会让下次构建再次检测到恶意条目，反复失败
3. 用户看到"含恶意条目"错误信息会知道是安全问题，与"损坏"语义可区分

实现上新增 `except EmbedError` 分支单独处理（预检抛的 EmbedError 不是
TarError/OSError，不会被原 `except (TarError, OSError)` 捕获），删除归档后
re-raise 保留原始错误信息。

### zip 符号链接检测

zip 格式通过 `external_attr` 高 16 位存储 Unix st_mode（MS-DOS 时代兼容设计）。
`stat.S_ISLNK(mode)` 检测 S_IFLNK（0o120000）。`mode == 0` 表示未设置 Unix
模式位（Windows 创建的 zip 常见），跳过检测避免误报。

### Windows 盘符检查

`len(name) >= 2 and name[1] == ":"` 拦截 `C:foo` 形式。zip 标准不允许冒号在
文件名中（Windows 上冒号是非法字符），合法 zip 不应有盘符路径，此检查安全。

反斜杠归一化（`name.replace("\\", "/")`）处理 Windows 创建的 tar/zip 可能用
反斜杠分隔路径的情况，统一用 `/` 分隔后检查。

## 代码实现情况

### _validate_tar_member

```python
def _validate_tar_member(member: tarfile.TarInfo) -> None:
    name = member.name.replace("\\", "/")
    if name.startswith("/"):
        raise EmbedError(f"python-build-standalone tarball 含绝对路径条目: {member.name}")
    if len(name) >= 2 and name[1] == ":":
        raise EmbedError(f"python-build-standalone tarball 含盘符条目: {member.name}")
    if ".." in name.split("/"):
        raise EmbedError(f"python-build-standalone tarball 含路径穿越条目: {member.name}")
    if member.issym() or member.islnk():
        raise EmbedError(f"python-build-standalone tarball 含链接条目: {member.name}")
    if member.isdev():
        raise EmbedError(f"python-build-standalone tarball 含设备文件条目: {member.name}")
```

### _validate_zip_member

```python
def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    name = info.filename.replace("\\", "/")
    if name.startswith("/"):
        raise EmbedError(f"embed zip 含绝对路径条目: {info.filename}")
    if len(name) >= 2 and name[1] == ":":
        raise EmbedError(f"embed zip 含盘符条目: {info.filename}")
    if ".." in name.split("/"):
        raise EmbedError(f"embed zip 含路径穿越条目: {info.filename}")
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        raise EmbedError(f"embed zip 含符号链接条目: {info.filename}")
```

### extract_archive 加固模式

```python
try:
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            _validate_zip_member(info)
        zf.extractall(runtime_dir)
except zipfile.BadZipFile as e:
    _safe_unlink_archive(archive_path, "embed zip")
    raise EmbedError(f"embed zip 损坏: {archive_path}") from e
except EmbedError:
    # 预检发现的恶意条目：归档可能被篡改，删除避免下次构建再次使用
    _safe_unlink_archive(archive_path, "embed zip")
    raise
```

tar 分支结构相同，3.11- 用 `for member in tf.getmembers(): _validate_tar_member(member)`
预检，3.12+ 用 `tf.extractall(runtime_dir, filter="data")`。

### _make_malicious_tar 测试辅助

```python
def _make_malicious_tar(path: Path, members: list[tuple[str, str]]) -> None:
    """members 为 (name, type) 元组，type ∈ {'file', 'symlink', 'hardlink', 'char'}."""
    with tarfile.open(path, "w:gz") as tf:
        for name, mtype in members:
            info = tarfile.TarInfo(name=name)
            if mtype == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tf.addfile(info, None)
            elif mtype == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = "python/bin/python3.11"
                tf.addfile(info, None)
            elif mtype == "char":
                info.type = tarfile.CHRTYPE
                info.devmajor = 1
                info.devminor = 3
                tf.addfile(info, None)
            else:  # file
                info.type = tarfile.REGTYPE
                data = b"x"
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
```

## 测试验证结果

### 新增测试（10 个）

zip 恶意条目（4 个）：
- `test_extract_embed_rejects_path_traversal`：`../../etc/passwd` 路径穿越
- `test_extract_embed_rejects_absolute_path`：`/etc/passwd` Unix 绝对路径
- `test_extract_embed_rejects_windows_drive`：`C:evil.txt` Windows 盘符
- `test_extract_embed_rejects_symlink`：`external_attr = 0o120777 << 16` 符号链接

tar 恶意条目（6 个）：
- `test_extract_standalone_rejects_path_traversal`：`../../etc/passwd` 路径穿越
- `test_extract_standalone_rejects_absolute_path`：`/etc/passwd` Unix 绝对路径
- `test_extract_standalone_rejects_windows_drive`：`C:evil.txt` Windows 盘符
- `test_extract_standalone_rejects_symlink`：SYMTYPE 符号链接（linkname=/etc/passwd）
- `test_extract_standalone_rejects_hardlink`：LNKTYPE 硬链接
- `test_extract_standalone_rejects_device_file`：CHRTYPE 字符设备文件

每个测试断言两点：
1. 抛 `EmbedError` 且 match 对应关键词（路径穿越/绝对路径/盘符/链接条目/设备文件）
2. 归档被删除（`assert not tar.exists()`），避免下次构建复用恶意归档

### 既有测试不回归

- `test_extract_embed`/`test_extract_standalone`：合法归档解压正常
- `test_extract_embed_bad_zip`/`test_extract_standalone_bad_tar`：损坏归档仍走
  "损坏"流程（不误报为恶意条目）
- `test_extract_*_bad_*_unlink_failure_warns`：删除失败告警逻辑不回归

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 2 files already formatted
- pyrefly: 0 errors（CLI `--project-excludes "**/assets/templates/**"`，
  toml 配置漂移待用户复核）
- pytest: 2042 passed, 12 skipped（iter-135 为 2032 passed，新增 10 个测试）
- coverage: 95.71%（>= 95% 门禁，iter-135 为 95.68%，提升 0.03%）
- 10 benchmarks: 全通过

## 整合优化情况

- tar 与 zip 预检逻辑共用相同的检查模式（绝对路径/盘符/路径穿越），仅 tar
  额外检查链接与设备文件、zip 额外检查符号链接模式位，符合两种格式的差异
- 恶意条目归档删除复用既有 `_safe_unlink_archive` 辅助函数，与"损坏归档"
  删除逻辑统一
- 错误信息明确区分"含恶意条目"（安全风险）与"损坏"（数据错误），便于用户
  排查

## 遗留事项

- pyrefly.toml `project-excludes` 配置在 pyrefly 1.1.1 未生效（iter-135 遗留，
  待用户复核，工具链配置变更需暂停）
- 3.12+ 的 `tf.extractall(filter="data")` 分支无法在 3.11 测试环境覆盖
  （`pragma: no cover` 标注，信任 CPython 内置实现）

## 下一轮计划

iter-137 编译产物验证增强（req-49 L105-107，阶段 3 深度健壮性）：
1. `_strip_compiled_sources` 批量验证 .pyd 可加载性扩展为并发验证
2. 损坏 .pyd 自动删除并回退到 .py 补测试覆盖损坏场景
3. Nuitka 编译失败时记录失败文件列表到 stamp，下次跳过这些文件避免反复尝试
