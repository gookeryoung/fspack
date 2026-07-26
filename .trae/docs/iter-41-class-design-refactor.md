# 迭代 41 - 类设计完善与函数拆分

## 需求清单

- [x] 当前项目的类设计过于臃肿，尤其是函数过大，结合 `.trae/skills/python-class-design` 进行完善
- [x] 阶段函数参数列表过长，用 dataclass 封装共享上下文

## 迭代目标

1. 按 `python-class-design` SKILL 的「函数 ≤ 5 参数 / 单一职责」原则，拆分项目中行数过大的函数
2. 拆分目标：单函数行数 ≤ 100 行（原最大 260 行），每个拆分出的子函数单一职责、有中文 docstring
3. 引入 `BuildContext` frozen dataclass 聚合阶段函数共享上下文，参数列表从 6-8 个降到 1-3 个
4. 不改变外部行为：所有公共 API（`build`/`download_wheels`/`compile_src`/`build_parser`/`main`）签名与语义保持不变
5. 全套门禁通过，覆盖率不低于上一轮（97.44%）

## 改动文件清单

### 主代码

- `src/fspack/builder.py`：
  - 新增 `BuildContext` frozen dataclass（5 字段：tracker/info/cfg/opts/runtime_dir）
  - `build()`（260 行）拆分为 `_prepare_runtime`/`_analyze_dependencies`/`_download_dependencies`/`_compile_user_sources`/`_build_entry_loaders` 五个阶段函数；阶段函数全部接收 `ctx` 替代散列参数
  - `_prepare_linux_runtime`/`_prepare_windows_runtime` 也改为接收 `ctx`
- `src/fspack/cli.py`：`build_parser()`（99 行）拆分为 `_add_build_subparser`/`_add_run_subparser`/`_add_clean_subparser`/`_add_package_subparser`
- `src/fspack/packaging/nuitka.py`：
  - `compile_src()`（184 行）拆分为 `_resolve_compile_python`/`_collect_py_files`/`_create_bootstrap_script`/`_compile_files`/`_strip_compiled_sources`
  - `_ensure_build_python()`（119 行）拆分为 `_download_standalone_python`/`_extract_standalone_python`
- `src/fspack/packaging/wheels.py`：`download_wheels()`（116 行）拆分为 `_prefilter_by_python_version`/`_build_pip_download_args`/`_run_pip_download`/`_parse_wheel_names`/`_record_wheel_stage`

### 文档

- `.trae/docs/iter-41-class-design-refactor.md`：新增本迭代记录
- `.trae/docs/iter-35-slim-volume-and-speed.md`：删除（保留最新 5 条迭代记录）

### 配置

- `pyproject.toml`：`[tool.fspack] exclude` 增加 `templates` 目录（自身打包排除模板文件）

## 关键决策与依据

### 1. 拆分粒度：阶段函数 + 模块级私有函数

- 阶段函数（`_prepare_runtime` 等）封装 `build()` 流水线中可独立测量的阶段，每个阶段对应一个 `tracker.stage()` 上下文
- 模块级私有函数（`_prefilter_by_python_version` 等）封装可复用的纯计算/IO 逻辑，遵循 `python-class-design` SKILL「模块级函数优于 Mixin」「纯函数直接放模块级」原则
- 拆分后函数全部 ≤ 91 行，平均 30-50 行，单职责清晰

### 2. `BuildContext` frozen dataclass 聚合阶段函数上下文

- 5 字段：`tracker`/`info`/`cfg`/`opts`/`runtime_dir`，目标平台通过 `cfg.target` 访问
- `frozen=True` 不可变、可哈希、线程安全，符合 SKILL「配置/描述类用 frozen dataclass」原则
- `site_packages` 是 `_prepare_runtime` 的返回值，作为下一阶段 `_download_dependencies` 的入参单独传递，不进 ctx（避免两阶段构造）
- 阶段函数参数从 6-8 个降到 1-3 个，远低于 rule-11 ≤ 5 阈值，可移除大部分 `# noqa: PLR0913`

### 3. 公共 API 签名保持不变

- `build()`/`download_wheels()`/`compile_src()`/`build_parser()`/`main()` 等公共函数签名零变更
- 拆分出的子函数全部以 `_` 前缀标记私有，不进 `__all__`
- 测试零修改即通过，证明行为保持一致

### 4. `build()` 保留 `# noqa: PLR0913`

- `build()` 是公共入口，7 个参数（project_dir/mirror/py_version/dist_dir/embed_cache/target/options）全部为必需配置
- 参数已按职责聚合（`BuildOptions` 封装 8 个构建开关、`MirrorConfig` 封装镜像源），进一步封装会割裂配置语义
- 按 SKILL「参数 ≤ 5，超出用 dataclass 封装」原则已合理，保留 `# noqa: PLR0913` 标注

### 5. `_run_pip_download` / `_download_online` 保留多参数

- 这两个函数封装 pip 调用细节，参数（`filtered`/`base_args`/`py`/`py_version`/`platform_tags`/`pypi_index`/`cache_dir`）全部为 pip 命令构造必需项
- 强行封装为 dataclass 会割裂参数间的隐式依赖（如 `py_version` 同时影响 `base_args` 和 `platform_tags`），得不偿失

### 6. `pyproject.toml` 排除 `templates` 目录

- `templates/release-pack.yml` 与 `templates/pack-check.yml` 是参考脚本，不参与 fspack 自身打包产物
- 与 `examples` 同属可选资源，纳入 `exclude` 避免复制到 `dist/src/`

## 代码实现情况

### 拆分前后函数行数对比

| 函数 | 拆分前 | 拆分后 |
|------|--------|--------|
| `builder.py:build` | 260 行 | 77 行 |
| `nuitka.py:compile_src` | 184 行 | 81 行 |
| `nuitka.py:_ensure_build_python` | 119 行 | 69 行 |
| `wheels.py:download_wheels` | 116 行 | 67 行 |
| `cli.py:build_parser` | 99 行 | 15 行 |

### BuildContext 重构前后阶段函数参数对比

| 阶段函数 | 重构前参数数 | 重构后参数数 |
|---------|------------|------------|
| `_prepare_runtime` | 6 | 1 |
| `_prepare_linux_runtime` | 3 | 1 |
| `_prepare_windows_runtime` | 4 | 1 |
| `_analyze_dependencies` | 4 | 1 |
| `_download_dependencies` | 8 | 3 |
| `_compile_user_sources` | 7 | 2 |
| `_build_entry_loaders` | 6 | 3 |

### 拆分后 builder.py 最大 10 个函数

```
81行  4参  builder.py:_precompile_pyc
77行  7参  builder.py:build
50行  2参  builder.py:_compile_user_sources
46行  3参  builder.py:_download_dependencies
40行  5参  builder.py:_resolve_project_icon
35行  3参  builder.py:_sync_tree
34行  3参  builder.py:_build_entry_loaders
30行  1参  builder.py:_prepare_runtime
26行  2参  builder.py:_site_packages_has_deps
24行  1参  builder.py:_prepare_linux_runtime
```

最大函数从 260 行降到 81 行；阶段函数参数从 6-8 个降到 1-3 个。

## 整合优化情况

- 拆分出的子函数命名一致（`_verb_object` 风格），与既有 `_find_pip_python`/`_find_uv` 等私有函数风格统一
- `_record_wheel_stage` 抽取了原 `download_wheels` 末尾 12 行统计回写逻辑，未来其他下载场景可复用
- `_parse_wheel_names` 将 stdout 解析与目录扫描回退逻辑封装为单一函数，返回 `(names, used_fallback)` 元组明确表达「回退扫描不可缓存」语义
- 拆分后 `compile_src` 主体仅剩编排（解析 python → 检查缓存 → 收集文件 → 创建脚本 → 编译 → 剥离），逻辑层次清晰
- `BuildContext` 是 frozen dataclass，符合 SKILL「不可变、可哈希、线程安全」配置类首选原则

## 测试验证结果

```
ruff check: All checks passed!
ruff format --check: 47 files already formatted
pyrefly check: 0 errors (67 suppressed, 7 warnings not shown)
pytest: 807 passed, 21 deselected in 5.03s
coverage: 97.49% (>= 95% required)
```

覆盖率从上一轮 97.44% 提升到 97.49%，未下降，符合 rule-11「覆盖率不得低于上一次的值」要求。

## 遗留事项

- 无

## 下一轮计划

- 无（任务完成）

## 提交记录

- `c67ddbe` chore: 自身打包排除 templates 目录
- `5ce0771` refactor: 按职责拆分大函数，最大函数从 260 行降到 91 行
- `6fa8d8e` docs: 补充 iter-40 迭代记录
- `refactor: 引入 BuildContext dataclass 聚合阶段函数上下文，参数从 6-8 个降到 1-3 个`
