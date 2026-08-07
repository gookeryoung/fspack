# iter-147: 修复 python-build-standalone tarball 相对路径链接被误拒

## 需求清单

- [x] 修复 `--target linux` 构建时 python-build-standalone tarball 含合法相对路径
      符号链接（如 `python/bin/2to3` → `python3.11`）被 `_validate_tar_member`
      误判为恶意条目导致 `EmbedError` 的问题
- [x] `_validate_tar_member` 对符号链接/硬链接的检查与 PEP 706 `data` filter
      语义对齐：仅拒绝绝对路径/路径穿越/Windows 盘符的 linkname，允许相对路径
      安全链接
- [x] 全套门禁通过（ruff/format/pyrefly/pytest/coverage ≥ 95%）

## 迭代目标

修复 CI Linux 环境下 `uv run fspack b . --target linux` 失败：

```
File ".../src/fspack/packaging/runtime.py", line 161, in _validate_tar_member
    raise EmbedError(f"python-build-standalone tarball 含链接条目: {member.name}")
fspack.exceptions.EmbedError: python-build-standalone tarball 含链接条目: python/bin/2to3
```

iter-136 引入的 `_validate_tar_member`（PEP 706 `data` filter 等价手动实现，用于
Python 3.11 及以下）完全禁止任何符号链接/硬链接，但 PEP 706 `data` filter 实际
仅拒绝绝对路径或指向目标目录之外的链接，允许相对路径的安全链接。python-build-
standalone 官方 tarball 含 `python/bin/2to3`、`python/bin/python3` 等指向
`python3.11` 的合法相对符号链接，在 Python 3.11/Linux CI 上被错误拒绝。

## 改动文件清单

- `src/fspack/packaging/runtime.py`（修改）：
  - `_validate_tar_member`：对 `issym()`/`islnk()` 不再直接拒绝，改为校验
    `member.linkname`：绝对路径（`/` 开头）、Windows 盘符（`X:`）、路径穿越
    （含 `..` 段）才拒绝，相对路径安全链接放行。docstring 同步说明与 PEP 706
    `data` filter 行为一致。
- `tests/test_runtime.py`（修改）：
  - `_make_malicious_tar`：`members` 元组扩展为 `(name, type[, linkname])`，
    支持自定义 linkname 以测试相对/绝对/穿越路径；默认 symlink linkname 仍为
    `/etc/passwd`，hardlink 默认改为 `python/bin/python3.11`（相对路径，安全）。
  - `test_extract_standalone_rejects_symlink`：更新为验证绝对路径符号链接被拒
    （match="绝对路径链接"），docstring 说明 PEP 706 data filter 语义。
  - 新增 `test_extract_standalone_rejects_traversal_symlink`：穿越路径符号链接
    被拒（match="路径穿越链接"）。
  - 新增 `test_extract_standalone_rejects_windows_drive_symlink`：盘符符号链接
    被拒（match="盘符链接"）。
  - `test_extract_standalone_rejects_hardlink`：linkname 改为 `../../etc/passwd`
    （穿越路径），match 改为"路径穿越链接"。
  - 新增 `test_extract_standalone_allows_relative_symlink`：直接调用
    `_validate_tar_member` 验证 `python/bin/2to3` → `python3.11` 相对符号链接
    不抛异常。
  - 新增 `test_extract_standalone_allows_relative_hardlink`：直接调用
    `_validate_tar_member` 验证 `python/bin/python3` → `python3.11` 相对硬链接
    不抛异常。

## 关键决策与依据

### 决策 1：手动预检与 PEP 706 data filter 语义对齐

PEP 706 `data` filter（CPython 3.12+ 内置，3.14 起默认）对链接的处理：
- `AbsoluteLinkError`：拒绝绝对路径符号链接
- `LinkOutsideDestinationError`：拒绝指向目标目录之外的链接
- **允许**相对路径且不穿越的安全符号链接/硬链接

iter-136 的 `_validate_tar_member` 在 3.11- 上完全禁止所有链接，与 3.12+
`data` filter 行为不一致。修复方案：对 `linkname` 做与 `name` 相同的三项检查
（绝对路径/盘符/穿越），与 CPython `data_filter` 源码逻辑等价。

依据：
- [PEP 706](https://peps.python.org/pep-0706/) `data` filter 规范
- [Python tarfile 文档](https://docs.python.org/3/library/tarfile.html#tarfile-extraction-filter)
  `AbsoluteLinkError`/`LinkOutsideDestinationError` 描述
- python-build-standalone 官方 tarball 含 `python/bin/2to3` 等相对符号链接

### 决策 2：allows 测试直接调用 `_validate_tar_member` 而非 `extract_standalone`

`extract_standalone` 实际解压符号链接/硬链接在 Windows 上可能因权限失败（需
开发者模式），会导致 `allows` 测试在 Windows 本地开发环境失败。改为直接调用
`_validate_tar_member` 验证纯逻辑不抛异常，集成行为已由 `rejects` 测试覆盖
（验证抛 EmbedError + tarball 被删除）。

### 决策 3：zip 符号链接检查保持严格不变

`_validate_zip_member` 仍完全拒绝符号链接。embed zip 是 Windows Python
embeddable distribution，不含符号链接（Windows 不常用符号链接），保持严格
合理。仅 tar 的链接检查与 PEP 706 对齐。

## 代码实现情况

### `_validate_tar_member` 修改

```python
if member.issym() or member.islnk():
    linkname = member.linkname.replace("\\", "/")
    if linkname.startswith("/"):
        raise EmbedError(
            f"python-build-standalone tarball 含绝对路径链接: {member.name} -> {member.linkname}"
        )
    if len(linkname) >= 2 and linkname[1] == ":":
        raise EmbedError(
            f"python-build-standalone tarball 含盘符链接: {member.name} -> {member.linkname}"
        )
    if ".." in linkname.split("/"):
        raise EmbedError(
            f"python-build-standalone tarball 含路径穿越链接: {member.name} -> {member.linkname}"
        )
```

错误信息包含 `member.name` 与 `member.linkname`，便于排查恶意条目。

## 整合优化情况

无重复代码：`linkname` 的三项检查与 `name` 的三项检查逻辑相同但作用于不同字段，
未提取辅助函数（避免过度抽象，字段语义不同）。

## 测试验证结果

- test_runtime.py 48 个测试全部通过（原 43 个 + 新增 5 个：traversal_symlink、
  windows_drive_symlink、allows_relative_symlink、allows_relative_hardlink，
  symlink/hardlink rejects 为改名重写）
- 全套门禁：2138 passed、12 skipped、26 deselected、coverage 95.69%、
  ruff format/check 0 errors、pyrefly 0 errors
- `runtime.py` 覆盖率 99%（仅第 291 行未覆盖，与本次修改无关）

## 遗留事项

无。

## 下一轮计划

无（独立 bug 修复）。等待用户下一步指示。
