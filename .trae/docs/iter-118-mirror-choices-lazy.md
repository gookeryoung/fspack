# iter-118：--mirror choices 真懒加载（启动性能优化 P1）

## 需求清单

- [x] 移除 `--mirror` 的 argparse choices（parser 构建期不再导入 `fspack.config`）
- [x] 新增 `_resolve_mirror` 执行期校验（非法值退出码 2，与 argparse 一致）
- [x] help 文本静态列出镜像键，配套防漂移测试
- [x] 全套门禁通过（ruff/pyrefly/pytest 1842 passed/coverage ≥ 95%）

## 迭代目标

iter-117 消除了 typing_extensions 后，`fsp --help`（`build_parser()`）仍耗时
47.7ms，其中 ~20ms 是 `choices=_mirrors_choices()` 在 parser 构建期立即
导入 `fspack.config` 所致——cli.py 注释声称"延迟导入避免 fsp --help 加载
config"实际并未生效（`_mirrors_choices()` 在 `add_argument` 调用时立即
求值）。本轮将 choices 校验从构建期移到执行期，使 `--help`/`run`/`clean`/
`init`/`doctor` 等轻命令不再白付 config 加载成本。

## 改动文件清单

### src/fspack/cli.py

1. **删除 `_mirrors_choices()`**：该函数是"假懒加载"——虽写在函数里，
   但被 `add_argument(choices=...)` 在 parser 构建期立即调用。
2. **两处 `--mirror` 参数**：`choices=_mirrors_choices()` → `metavar="MIRROR"`
   + help 静态文本 `镜像源（huawei/aliyun/tsinghua，默认 tsinghua）`，
   附注释说明刻意不写 choices 的原因与同步维护要求。
3. **新增 `_resolve_mirror(value) -> MirrorConfig`**：执行期调 `get_mirror`，
   捕获 `KeyError` → `console.error` + `SystemExit(2)`（退出码与 argparse
   choices 校验失败一致，用户体感无差异）。`MirrorConfig` 加入 TYPE_CHECKING
   导入块。
4. `_run_build`/`_run_package` 中 `get_mirror(ns.mirror)` →
   `_resolve_mirror(ns.mirror)`，顶部 docstring 更新设计说明。

### tests/test_cli.py

- **改写** `test_mirrors_choices_returns_valid_list` →
  `test_build_parser_does_not_load_config`：subprocess 断言 `build_parser()`
  后 `fspack.config` 不在 `sys.modules`（行为级守护，防止回归）。
- **新增** `test_mirror_help_lists_all_mirror_keys`：遍历 parser 全部
  `--mirror` 参数的 help 文本，断言包含 `MIRRORS` 所有键（防止静态列表
  与 `config.models.MIRRORS` 漂移）。
- **新增** `test_resolve_mirror_invalid_exits_with_code_2`：非法镜像
  `SystemExit` 且退出码为 2。
- **修正** `test_invalid_mirror_rejected`：补 `_make_minimal_project`——
  choices 移除后，空目录会先卡在 `ProjectInfo.from_dir`（ProjectError）
  而非 mirror 校验；最小项目保证流程走到 `_resolve_mirror`。
- **修正** `test_cli_module_no_top_level_platform_import`：TYPE_CHECKING
  豁免块字符串适配新增的 `MirrorConfig` 导入行。

## 关键决策与依据

1. **执行期校验而非自定义 argparse Action**：`type=`/`Action` 方案同样在
   parse_args 阶段触发（`fsp --help` 不经过 parse_args 倒是安全，但
   `fsp run` 等不需 config 的命令仍会触发）。执行期校验只在真正需要
   mirror 的 build/package 路径付费，语义最精确。
2. **退出码 2 保持与 argparse 一致**：用户脚本/CI 若依赖退出码判断
   参数错误，行为不变。`test_invalid_mirror_rejected` 原断言 SystemExit
   依然成立。
3. **help 静态列表 + 测试守护**：镜像键变动频率极低（MIRRORS 自引入
   以来未变过），静态写入 help 可避免构建期导入；漂移风险由
   `test_mirror_help_lists_all_mirror_keys` 兜底。

## 代码实现情况

完成，见改动文件清单。

## 整合优化情况

- 无新增抽象：`_resolve_mirror` 为一次性校验逻辑，两个调用点共用。
- cli.py 行数 851 → 862（注释与 `_resolve_mirror` 增加），为 iter-120
  的 cli_parser 拆分积累内容。

## 测试验证结果

### 性能收益（实测）

| 指标 | iter-117 后 | iter-118 后 |
|------|------------|------------|
| `build_parser()`（`fsp --help` 路径） | 47.7ms | **26.2ms（-45%）** |
| `fspack.config` 在 build_parser 导入链 | 存在 | **消失** |
| 相比优化前基线（iter-116） | 47.5ms | **-45%** |

### 门禁

- ruff check / ruff format --check：All checks passed
- pyrefly check：0 errors（11 suppressed）
- pytest：1842 passed, 12 skipped（较 iter-117 净增 2 个测试）
- coverage：95.25% ≥ 95%

## 遗留事项

- `.venv_broken` 残骸目录待清理（同 iter-117）

## 下一轮计划

iter-119：`_compat.py` 拆分——`CICompat` 移入 `console.py`（唯一消费方），
`_compat` 仅留 `override`/`tomllib` 零第三方导入；顺带将 `platform.py`
的 `import platform` 延迟到函数内（省 Windows `_wmi` ~1.5ms）。
