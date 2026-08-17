# iter-01 项目结构优化

## 需求清单

来源 `.trae/req/req-01-项目结构优化.md`：拆分超大模块、归组前缀家族、消除 PLR0913、健壮性专项。

## 迭代目标

制定并执行项目结构优化：8 个超 500 行模块拆至单一职责，packaging 顶层家族归组，全程 `make check` 全绿（覆盖率 >= 95%）。

## 现状证据（2026-08-18 收集）

### 行数基线（Top，Get-Content 精确计数）

| 行数 | 模块 | 职责混杂点 |
|-----|------|-----------|
| 891 | doctor/envs.py | 环境检查 `_check_*` / 缓存健康 `_scan_*_health`+`_clean_*` / 归档完整性 `_is_zip_intact`+`_is_tar_intact`+`_is_pe_file` |
| 701 | packaging/wheels/resolver.py | uv 适配 `_find_uv`..`_extract_resolved_lines` / pip 下载 `_run_pip_download`+`_download_online` / 并行编排 `_download_resolved_parallel`..`_merge_parallel_results`；7 处 PLR0913 |
| 646 | config/parsing.py | pyproject 主解析 / entries 家族 `_parse_entries`..`_merge_entries`+`detect_entry` / 类型推断 `infer_app_type`+`_is_main_check` |
| 604 | packaging/installer/base.py | `Installer` ABC + 模块级构建入口 + `_prepare_dist`；5 处 PLR0913 |
| 591 | cli.py | 已拆出 cli_parser/cli_init，边缘达标 |
| 575 | cli_parser.py | 同上 |
| 573 | packaging/loader/compile.py | 待拆分点分析 |
| 539 | doctor/templates.py | 模板构建 `_run_template`+`_build_single_template` / 调试运行 / 报告渲染 `_print_*`+`_format_*` |

### 结构事实

- `__init__.py` facade 全部合规（纯 docstring/version，无业务定义）
- ruff check src tests 全绿（0 违规）
- packaging 顶层 20+ 散模块：runtime 家族 5 文件（runtime.py 为 facade）、pyc 家族 3 文件（pyc.py 为 facade）、win7 家族 3 文件（无 facade，被 doctor/win7.py、pipeline/、installer/nsis.py 交叉引用）
- doctor 分层已存在：envs.py（底层引擎）→ cache.py（命令渲染层）
- tests/ 50+ 文件平铺，命名与模块对应良好
- tox.ini 多版本矩阵与 CI 并存，非冗余

## 阶段计划（P1-P8，共约 12 轮）

### P1 基线固化（1 轮）—— 本轮

- 记录行数基线、lint/typecheck/cov 三门禁现状
- 产出本计划，用户确认 P6 归组方向

### P2 doctor 域拆分（2 轮）

1. `envs.py`（891）拆三：
   - `doctor/integrity.py` ← `_is_zip_intact`/`_is_tar_intact`/`_is_pe_file`/`_parse_deps_entry`/`_try_unlink`/`_file_size`（归档完整性检测）
   - `doctor/cache_health.py` ← `_scan_cache_health`/`_scan_*_health`×7/`_clean_*`×4/`_cache_dir_by_attr`/`_scan_cache_by_type`/`_scan_all_caches`（缓存健康引擎）
   - `envs.py` 保留 `_check_*` 纯环境检查（预计 <200 行）
   - `cache.py`（命令层）改从 `cache_health.py` 导入；测试 patch 路径同步迁移
2. `templates.py`（539）拆二：报告渲染 `_print_*`/`_format_*` → `doctor/template_report.py`；templates.py 保留构建与运行逻辑

### P3 wheels 域拆分 + 参数收敛（2 轮）

1. `resolver.py`（701）拆三：
   - `wheels/uv_bridge.py` ← uv 发现/能力检测/输出格式转换/`_resolve_with_uv`/`_extract_resolved_lines`
   - `wheels/parallel.py` ← `_download_resolved_parallel`/`_download_one_with_uv`/`_download_one_resolved`/`_merge_parallel_results`
   - `resolver.py` 保留编排入口 `_download_online`/`_download_with_hashes`
2. 消除 7 处 PLR0913：下载上下文参数（req/平台标记/缓存目录/进度回调等）封装 `DownloadContext` dataclass

### P4 config 域拆分（1 轮）

- `parsing.py`（646）拆三：
  - `config/entries.py` ← `_parse_entries`/`_parse_project_scripts`/`_resolve_module_script`/`_merge_entries`/`detect_entry`/`_has_entry`
  - `config/app_type.py` ← `infer_app_type`/`_is_main_check`
  - `parsing.py` 保留 pyproject 主解析 + `expand_extras`

### P5 installer / loader 瘦身（2 轮）

1. `installer/base.py`（604）：`_prepare_dist`/`_exe_path`/`_exe_exists`/`_check_exe`/`_py_tag`/`_release_base` → `installer/dist_prep.py`；模块级入口 `build_installer`/`build_linux_installer`/`build_release`/`_resolve_formats` → `installer/facade.py`；base.py 仅剩 `Installer` ABC + stage 工具。5 处 PLR0913 以 `InstallerRequest` dataclass 收敛
2. `loader/compile.py`（573）：按「命令构造 / 进程执行 / 缓存管理」拆分（拆分点实现轮分析）

### P6 packaging 顶层家族归组（2 轮，方向待用户确认）

- `runtime*.py`×5 → `packaging/runtime/` 子包，原 `runtime.py` facade 语义并入 `__init__.py` re-export（导入路径 `fspack.packaging.runtime.download_embed` 不变）
- `pyc*.py`×3 → `packaging/pyc/` 子包，同上保持 `fspack.packaging.pyc` 路径兼容
- `win7_*.py`×3 → `packaging/win7/` 子包，跨包引用点（doctor/win7.py、pipeline/×3、installer/nsis.py）同步更新
- 风险控制：项目并发用 ThreadPoolExecutor（线程池，无 pickle 定位问题）；测试 monkeypatch 同步迁移至新定义模块；每家族独立提交

### P7 健壮性专项（2 轮）

1. 异常链审查：`raise ... from exc` 保留因果；捕获范围收窄；捕获后必有处理（记录/包装/重抛）
2. 死代码交叉验证：ruff `--select F` + pyrefly 死分支 + coverage 长期 0% 区域三方交叉，假阳性（动态分发/`__all__`/dunder）保留并注释；不安装 vulture/deptry（避免新依赖）
3. 类型收紧：公共 API 返回类型补全；`Any` 收窄

### P8 收尾（1 轮）

- `docs/architecture.rst` 模块导览同步；`packaging/__init__.py` docstring 概览同步
- 全量 `make check` 复核；req-01 勾选并移入 `.trae/req/done/`
- 提交推送

## 关键决策

1. **只拆不改语义**：所有拆分保持函数签名与行为不变，测试断言不放宽，仅迁移 patch 路径
2. **渐进归组**：P6 家族归组通过 facade re-export 保持导入路径兼容，外部引用零破坏
3. **不引入清理工具依赖**：vulture/deptry 需新增依赖（暂停项），以 ruff F + pyrefly + coverage 交叉替代
4. **P6 前置确认**：模块归组属「重命名公共模块/包」类高风险操作，方向经用户确认后执行
5. **每维度独立提交**：单提交仅含一个拆分/归组维度，可独立回滚

## 代码实现

本轮为计划制定，无代码改动。

## 整合优化

无（计划态）。

## 测试结果

- ruff check src tests：0 违规（基线全绿）
- 行数基线已记录（见「现状证据」），作为拆分后对比基准

## 遗留事项

- P6 归组方向待用户确认（runtime/pyc/win7 三家族是否归子包）
- cli.py（591）/cli_parser.py（575）边缘超标，P5 后视余量决定是否处理
- `_util` 包命名不理想但职责清晰（format/fsutil/jsoncache），暂不动

## 下一轮计划

进入 P2 第 1 轮：拆分 `doctor/envs.py` → `integrity.py` + `cache_health.py` + 瘦身 envs.py；同步迁移 `doctor/cache.py` 导入与测试 patch 路径；`make check` 全绿后独立提交。
