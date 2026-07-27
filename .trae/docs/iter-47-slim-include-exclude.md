# iter-47 wheel 精简用户自定义 include/exclude 规则

## 需求清单

- [x] `[tool.fspack]` 新增 `slim-include`/`slim-exclude` 配置项
- [x] `slim_include` 强制保留被 spec 剥离的文件（覆盖 STRIP_EXTS/闭包外剥离）
- [x] `slim_exclude` 强制剥离被 spec 保留的文件（覆盖闭包内保留/shared 保留）
- [x] 优先级：`slim_include` > `slim_exclude` > spec 自动分类
- [x] 支持 glob 模式（`fnmatch`，`*` 匹配任意字符含 `/`）
- [x] 全套门禁通过

## 迭代目标

为 wheel 精简打包增加用户自定义 include/exclude 规则，实现更细粒度的打包控制。
用户可在 `pyproject.toml` 的 `[tool.fspack]` 段配置 glob 模式，强制保留或剥离
特定文件，覆盖 spec 自动分类的默认行为。主要用于：

- 强制保留被 spec 误剥离的文件（如 Qt6Charts.dll 被 AST 闭包排除，但用户知道需要）
- 强制剥离被 spec 保留的文件（如 opengl32sw.dll 被 Quick 闭包保留，但用户知道不需要）
- 精细控制特定目录的保留/剥离（如 `PySide6/translations/*` 全部剥离）

## 改动文件清单

- [src/fspack/config.py](../../src/fspack/config.py)
  - `ProjectInfo` 新增 `slim_include: tuple[str, ...]`/`slim_exclude: tuple[str, ...]` 字段
  - 新增 `_parse_slim_patterns` 解析函数（fnmatch glob 模式元组）
  - `parse_project` 解析 `[tool.fspack] slim-include`/`slim-exclude`
- [src/fspack/slim/base.py](../../src/fspack/slim/base.py)
  - 新增 `_match_any` 辅助函数（fnmatch.fnmatchcase 匹配）
  - `_slim_extract` 新增 `user_include`/`user_exclude` 参数，在 spec 分类前检查用户规则
  - `_unpack_one_wheel` 新增 `user_include`/`user_exclude` 参数并透传
  - `slim_unpack` 新增 `slim_include`/`slim_exclude` 关键字参数并透传
- [src/fspack/builder.py](../../src/fspack/builder.py)
  - `unpack_wheels` 新增 `slim_include`/`slim_exclude` 参数并透传给 `slim_unpack`
  - `_install_wheels` 调用 `unpack_wheels` 时传入 `ctx.info.slim_include`/`slim_exclude`
- [tests/test_slim.py](../../tests/test_slim.py)：新增 6 个测试
  - `test_user_include_force_keep_excluded_file`：include 覆盖 STRIP_EXTS
  - `test_user_exclude_force_strip_kept_file`：exclude 覆盖闭包内保留
  - `test_user_exclude_glob_pattern`：glob 模式匹配目录所有文件
  - `test_user_include_priority_over_exclude`：include 优先级高于 exclude
  - `test_user_rules_no_match_fallback_spec`：不匹配时走 spec 自动分类
  - `test_user_exclude_case_sensitive`：大小写敏感
- [tests/test_config.py](../../tests/test_config.py)：新增 4 个测试
  - `test_parse_project_with_slim_include_exclude`：正常解析
  - `test_parse_project_slim_include_not_list_raises`：非列表报错
  - `test_parse_project_slim_exclude_empty_element_raises`：空字符串元素报错
  - `test_parse_project_slim_include_exclude_defaults_empty`：默认空元组

## 关键决策与依据

### 1. 优先级：include > exclude > spec

`slim_include` 优先级高于 `slim_exclude`，依据：当用户同时配置了同一路径的 include
和 exclude 时，保留比剥离更安全（避免误剥离导致运行时缺失依赖）。这也是 fspacker
等工具的常见约定。

### 2. glob 模式用 fnmatch.fnmatchcase

- `fnmatch` 是标准库，无新依赖
- `*` 匹配任意字符含 `/`，支持 `PySide6/translations/*` 匹配子目录文件
- `fnmatchcase` 大小写敏感，与 wheel 内 POSIX 路径原样匹配（避免 Windows 大小写
  不敏感导致误匹配）

### 3. 用户规则在 spec 分类前检查

在 `_slim_extract` 循环中，每个条目先检查用户规则，再走 spec 自动分类。这样：
- 用户 include 匹配 → 直接保留（跳过 spec 分类）
- 用户 exclude 匹配 → 直接跳过（跳过 spec 分类）
- 都不匹配 → 走 spec 自动分类（向后兼容）

### 4. 参数过多用 noqa: PLR0913

`_slim_extract`/`_unpack_one_wheel`/`slim_unpack`/`unpack_wheels` 参数超过 5 个，
沿用项目已有先例（`_default_classify` 的 `# noqa: PLR0913`）。不引入 dataclass
封装，避免过度抽象（用户规则只有两个字段，且语义清晰）。

## 代码实现情况

### _slim_extract 用户规则集成

```python
def _slim_extract(zf, dest, top_pkg, keep_subs, user_include=frozenset(), user_exclude=frozenset()):
    spec = get_spec(normalize_name(top_pkg))
    for info in zf.infolist():
        # 用户规则优先级最高：include > exclude > spec 自动分类
        if user_include and _match_any(info.filename, user_include):
            zf.extract(info, dest)
            continue
        if user_exclude and _match_any(info.filename, user_exclude):
            continue
        category, sub = spec.classify_entry(info.filename, top_pkg, keep_subs)
        # ... spec 自动分类逻辑
```

### 配置格式

```toml
[tool.fspack]
slim-include = ["PySide6/Qt6Charts.dll"]
slim-exclude = ["PySide6/opengl32sw.dll", "PySide6/translations/*"]
```

## 测试验证结果

- ruff check：All checks passed
- ruff format：47 files already formatted
- pyrefly check：0 errors
- pytest（非 slow）：966 passed, 21 deselected
- 覆盖率：97.08%（slim/base.py 99%，slim/default.py 100%，slim/qt.py 100%）

## 使用示例

### 场景 1：强制保留被 AST 闭包排除的 Qt 模块

```toml
[tool.fspack]
slim-include = ["PySide6/Qt6Charts.dll", "PySide6/QtCharts.pyd"]
```

### 场景 2：强制剥离 opengl32sw.dll（Quick 闭包保留但用户不需要软件 OpenGL）

```toml
[tool.fspack]
slim-exclude = ["PySide6/opengl32sw.dll"]
```

### 场景 3：剥离整个 translations 目录

```toml
[tool.fspack]
slim-exclude = ["PySide6/translations/*"]
```

### 场景 4：组合使用

```toml
[tool.fspack]
slim-include = ["PySide6/Qt6Charts.dll"]
slim-exclude = ["PySide6/opengl32sw.dll", "PySide6/translations/*", "PySide6/include/*"]
```

## 遗留事项

无。

## 下一轮计划

无。本次修复闭环完成。
