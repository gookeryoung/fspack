# 源码复制隐藏目录排除与强制包含

## 需求来源

- 用户需求：打包时自动跳过 `.pnpm-store`/`.cnb`/`.codeup`/`.claude` 等 `.` 开头的配置文件夹内容，除非强制包含。
- 附带需求：分析 `fsp b` 后 dist 内多余文件并处理（发行包不应携带构建中间标记与旧版残留）。

## 接口定义

- `fspack.packaging.sync._exclude_hidden_dirs(directory, names) -> set[str]`：
  `shutil.ignore_patterns` 回调签名；排除 `.` 开头且 `isdir` 的条目（dotfile 不排除）。
- `copy_source(..., include_dirs: tuple[str, ...] = ())`：新增参数，
  相对项目目录的 POSIX 路径，命中的隐藏目录（含祖先链）从排除集中恢复。
- `_build_ignore_fn(..., include_dirs=())`：`_EXCLUDE` 组合加入 `_exclude_hidden_dirs`；
  protected 分支同样应用；`_force_keep_hidden` 返回应恢复的条目集合（调用方做差集）。
- `_is_path_or_ancestor(child, target)`：child 是 target 自身或祖先时 True。
- `_resolve_dir(directory)`：resolve 失败回退原始路径。

## 配置链路

- `[tool.fspack] include-dirs` → `parsing._parse_include_dirs`（`_parse_string_list_cfg`，空元素报错）
  → `ProjectInfo.include_dirs` → `executor` 透传 `copy_source(include_dirs=...)`。
- 语义边界：`include-dirs` 仅对隐藏目录自动排除生效，对 `node_modules`/`.env`/
  元数据等具名排除规则无强制效果；`exclude` 显式排除始终优先。

## 发行包排除（dist_prep）

- `_DIST_INTERMEDIATE_EXCLUDES` 增加 `.build_ok`/`.build_failed`（自动流入 NSIS `/x` 列表）。
- `_DIST_IGNORE` 改为 `_merge_ignore_fns(ignore_patterns(...), _exclude_hidden_dirs)`：
  旧版构建残留在 dist 内的隐藏目录（如 `dist/src/.pnpm-store`）不进 tar.gz/zip/7z/deb/pkg/dmg。
- dist 多余文件分析结论（对应 cndb 构建日志）：dist 根的 `.dep_cache.json`/
  `.pyc_stamp`/`.nuitka_compile_stamp`/`build/`/`release/` 原已排除；
  `.build_ok`/`.build_failed` 原先漏排除（本次修复）；构建失败残留 runtime 的
  场景由既有 `--auto-clean`/`fsp c` 机制处理，不再重复建设。

## 测试

- `test_sync.py`：隐藏目录跳过（根/嵌套/data-dirs 内）、include-dirs 强制包含
  （直接命中/祖先链/不救回具名排除）、增量同步删除旧残留。
- `test_config.py`：include-dirs 解析（正常/非列表/空元素/缺省）。
- `test_installer.py`：zip 发行包排除中间标记与隐藏目录残留。

## 状态

- [x] sync.py 隐藏目录排除 + include_dirs
- [x] 配置链路（parsing/models/config __init__/executor）
- [x] dist_prep 中间标记与 _DIST_IGNORE
- [x] 单元测试与文档（configuration.md/CHANGELOG.md）
