# iter-120：builder facade 精简 + cli.py 拆分 + import 基线（结构解耦收尾）

## 需求清单

- [x] builder facade 移除标准库/运行时 re-export（json/re/shutil/subprocess/tempfile/TkinterBundler/download_embed 等）
- [x] 测试 patch 迁移：`fspack.builder.subprocess.run` → 直接 patch `subprocess.run`
- [x] cli.py 拆分：parser 构建代码 → `cli_parser.py`，cli.py 聚焦 main/dispatch
- [x] import 基线测试：`fsp --help` 全链路不加载 config/console/platform/rich
- [x] 全套门禁通过（ruff/pyrefly/pytest 1843 passed/coverage ≥ 95%）

## 迭代目标

优化方案收尾轮，解决两类残留耦合：

1. **builder.py facade 虚胖**：为兼容测试 monkeypatch 路径而 re-export
   50+ 符号（含 5 个标准库模块与 TkinterBundler/download_embed 等运行时
   依赖），任何 `import fspack.builder` 都被迫连带加载。
2. **cli.py 结构失衡**：863 行中 400 行是 argparse 参数声明，与命令分发
   逻辑混居；`fsp --help` 虽经 iter-117~119 已不加载重模块，但缺少
   全链路基线测试固化收益，回退无拦截。

## 改动文件清单

### src/fspack/cli_parser.py（新建，449 行）

- 承载 `build_parser` + 6 个 `_add_*_subparser`（build/run/clean/package/init/doctor）；
- 顶部仅 `argparse` + `__version__`，保持零重依赖（`--mirror` 仍无 choices，
  执行期校验语义不变）；
- docstring 说明拆分缘由与 config 懒加载约束。

### src/fspack/cli.py（863 → 472 行）

- 删除全部 parser 构建函数（迁至 cli_parser.py）；
- `from fspack.cli_parser import build_parser` re-export 保持既有引用兼容
  （测试 `fspack.cli.build_parser`、入口 `fspack.cli:main` 不变）；
- 保留 main/dispatch/`_resolve_mirror`/`discover_subprojects` 等执行逻辑。

### src/fspack/builder.py

- 移除 5 个标准库模块 re-export（json/re/shutil/subprocess/tempfile/Path）；
- 移除运行时依赖 re-export（TkinterBundler/compile_loader/download_embed 等）；
- 移除无人引用的私有符号（BuildContext/_prepare_runtime/_analyze_dependencies/
  `_normalize_pkg_name`/`_strip_version_specifier`/`_WIN7_COMPAT_DLL_NAME` 等）；
- 保留两类既有引用：测试 `from fspack.builder import ...`（test_builder/test_icon）
  与 `nuitka.standalone` 的 `_inject_win7_compat_dll`；
- docstring 更新：明确 monkeypatch 标准库调用应直接 patch 标准库属性。

### tests/test_builder.py（~30 处）

- `monkeypatch.setattr("fspack.builder.subprocess.run", ...)` →
  `monkeypatch.setattr("subprocess.run", ...)`：patch 标准库单例等效，
  不再依赖 facade 命名空间；
- 个别 `fspack.builder.shutil`/`tempfile` 引用同样迁移到底层模块或标准库。

### tests/test_cli.py

- `test_cli_module_no_top_level_console_import` /
  `test_cli_module_no_top_level_platform_import`：锚点从 `def build_parser`
  改为 `def main`（拆分后 cli.py 已无 `def build_parser`，旧锚点会误扫全文）；
- 新增 `test_help_does_not_load_heavy_modules`：子进程执行
  `main(['--help'])` 后断言 `fspack.config`/`fspack.console`/`fspack.platform`/
  `rich` 均未进入 `sys.modules`——import 基线固化 iter-117~120 全部收益。

## 关键决策与依据

1. **拆分为 cli_parser.py 而非 dispatch 外迁**：入口 `fspack.cli:main` 是
   公开契约（pyproject scripts），main/dispatch 留在 cli.py，参数声明
   外迁；`build_parser` re-export 一行成本换全部既有引用零改动。
2. **facade 不留 monkeypatch 兼容层**：monkeypatch 标准库单例
   （`subprocess.run`）与 patch facade 属性等效，兼容层只是历史惯性；
   移除后 facade 回归"API 索引"本职，import 链更短。
3. **基线测试用子进程而非进程内断言**：`sys.modules` 是全局态，进程内
   断言会被其他测试的导入污染；子进程隔离确保基线语义精确（与
   `test_build_parser_does_not_load_config` 同一模式）。

## 代码实现情况

完成，见改动文件清单。

## 整合优化情况

- cli.py 863 → 472 行（-45%），cli_parser.py 449 行，职责各一；
- builder.py 84 → 66 行，facade 不再承载标准库/运行时兼容符号；
- 无重复代码、无新增依赖；全部既有导入路径保持兼容。

## 测试验证结果

### 性能收益（实测，iter-117 前 → iter-120 后）

| 指标 | 优化前 | iter-120 后 |
|------|--------|------------|
| `import fspack.cli` | ~55ms（连带 config/rich/platform） | **18.2ms（-67%）** |
| `fsp --help` 冷启动 | ~100ms | **61ms（-39%，含解释器启动 ~40ms）** |
| `fsp --help` 模块集 | 含 rich/config/platform | 仅 argparse/logging/pathlib/os/sys |

### 门禁

- ruff check / ruff format --check：All checks passed（117 files）
- pyrefly check：0 errors（11 suppressed）
- pytest：1843 passed, 12 skipped（较 iter-119 +1：新增 import 基线测试）
- coverage：95.27% ≥ 95%

## 遗留事项

- `.venv_broken` 残骸目录待清理（同 iter-117）
- `fspack.slim.unpack → fspack.progress` 加载 rich 为进度条功能所需，不处理

## 下一轮计划

结构优化主题收尾。后续方向（按需开启新主题）：
- 打包产物 wrapper 的 `--lazy-import` 已在 iter-120 前交付，可收集用户侧收益反馈；
- `fsp b` 热路径（builder 链）import 已 93.4ms，可评估 slim/analyzer 进一步惰性化。
