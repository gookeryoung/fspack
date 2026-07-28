# iter-88: 打包产物大小报告

## 需求清单

- [x] 新增 `packaging/size_report.py` 模块实现体积报告扫描与渲染
- [x] `BuildOptions`/`BuildDefaults` 添加 `no_size_report` 字段
- [x] `[tool.fspack]` 配置支持 `no_size_report` 键
- [x] CLI `fsp b` 添加 `--no-size-report` 标志
- [x] `pipeline.build()` 末尾按 `opts.no_size_report` 控制输出体积报告
- [x] 体积报告含 runtime/src/site-packages/其他 四类 + site-packages Top 10 包
- [x] 编写测试覆盖 size_report 模块与 CLI 透传
- [x] 全套门禁通过 + 文档更新 + git 提交

## 迭代目标

在 `fsp b` 完成后输出 dist 体积报告，按 runtime/src/site-packages/其他四大类
统计总体积与占比，site-packages 按 dist-info 目录统计 Top 10 包体积排序，
帮助用户定位体积热点。支持 `--no-size-report` 关闭。

## 改动文件清单

- `src/fspack/packaging/size_report.py`：新增模块，实现 `collect_size_report`/
  `print_size_report` 与 `SizeCategory`/`PackageSize`/`SizeReport` 数据类
- `src/fspack/packaging/pipeline.py`：`build()` 末尾按 `opts.no_size_report`
  调用 `print_size_report`
- `src/fspack/config/models.py`：`BuildOptions`/`BuildDefaults` 添加
  `no_size_report` 字段；`build_options_from_defaults` 处理新字段
- `src/fspack/config/parsing.py`：`_BUILD_DEFAULT_KEYS` 添加 `no_size_report`
  映射；`_parse_build_defaults` docstring 同步更新
- `src/fspack/cli.py`：`_add_build_subparser` 注册 `--no-size-report` 标志；
  `_run_build` 用 `replace()` 应用 `no_size_report=ns.no_size_report or base.no_size_report`
- `tests/test_size_report.py`：新增 28 个测试覆盖 size_report 模块与 CLI 透传
- `README.md`：`fsp build` 命令速查表与章节添加 `--no-size-report` 选项
- `.trae/req/req-47-feature-perf-polish.md`：iter-88 标记完成

## 关键决策与依据

### 1. 独立模块而非内联到 pipeline.py

按用户偏好「按责任拆分模块」，体积报告涉及扫描、解析 dist-info、RECORD 文件
解析、表格渲染等多个关注点，独立到 `packaging/size_report.py` 便于测试与
复用。pipeline.py 仅在 `build()` 末尾做一次 `print_size_report` 调用。

### 2. 包体积估算优先用 RECORD 文件

wheel 安装时生成的 `dist-info/RECORD` 记录该包所有文件的相对路径，按 RECORD
累加最准确。无 RECORD 时回退到按包名前缀匹配顶层目录（best effort），且跳过
`.dist-info`/`.egg-info` 元数据目录避免误匹配。

### 3. 默认输出，可关闭

体积报告对定位打包体积热点价值大，默认输出。CI 等场景需静默时用
`--no-size-report` 关闭。配置层 `[tool.fspack] no_size_report = true` 同样
生效，CLI 标志可覆盖配置。

### 4. site-packages 自动定位

支持 Windows embed（`runtime/Lib/site-packages`）与 Linux standalone
（`runtime/python/lib/python<X.Y>/site-packages`）两种路径模式，用 glob
匹配避免硬编码 Python 版本。

## 代码实现情况

### size_report.py 核心函数

```python
def collect_size_report(dist_dir: Path, *, top_n: int = 10) -> SizeReport:
    """扫描 dist 目录，返回结构化体积报告."""
    # 扫描 runtime/src/site-packages/其他 四大类
    # site-packages 按 dist-info 目录统计 Top N 包
    ...

def print_size_report(dist_dir: Path, *, top_n: int = 10) -> SizeReport:
    """扫描 dist 目录并渲染体积报告到控制台."""
    # 用 rich.table 渲染类别分布表 + Top N 包表
    ...
```

### pipeline.py 集成

```python
console.rich.print(tracker.summary())
if not opts.no_size_report:
    from fspack.packaging.size_report import print_size_report
    print_size_report(cfg.dist_dir)
```

### CLI 透传

```python
p.add_argument(
    "--no-size-report",
    action="store_true",
    help="关闭构建结束后的体积报告",
)

# _run_build
options = replace(
    base,
    ...
    no_size_report=ns.no_size_report or base.no_size_report,
)
```

## 测试验证结果

`tests/test_size_report.py` 28 个测试：

**辅助函数（11 个）**：
- `_dir_size`：空目录/不存在/累加文件
- `_find_site_packages`：Windows/Linux/未找到
- `_normalize_pkg_name`：分隔符替换
- `_parse_dist_info_name`：正常/无版本/复杂包名
- `_size_from_record`：累加/缺失文件跳过
- `_package_dir_size`：有 RECORD/无 RECORD 回退

**collect_size_report（6 个）**：
- 空 dist/类别列表/总计/Top N 排序/top_n 限制/其他类别

**print_size_report（3 个）**：
- 渲染表格/空 dist 跳过/无包时跳过 Top 表

**数据类（3 个）**：
- `SizeCategory.size_formatted`/`PackageSize.size_formatted`/`SizeReport.total_size_formatted`

**CLI 透传（2 个）**：
- `--no-size-report` 透传 True/未指定默认 False

**门禁结果**：
- ruff check：All checks passed
- ruff format：86 files already formatted
- pyrefly check：0 errors
- pytest：1363 passed, 1 skipped, 覆盖率 97.76%（>= 95%）

## 整合优化情况

- 修复 `_package_dir_size` 回退逻辑：跳过 `.dist-info`/`.egg-info` 目录，
  避免按包名前缀匹配时把元数据目录误计入包体积

## 遗留事项

无。体积报告已完整覆盖扫描、渲染、CLI 透传、配置层支持。

## 下一轮计划

iter-89：`--log-file` 构建日志持久化，支持 text/json 双格式。
