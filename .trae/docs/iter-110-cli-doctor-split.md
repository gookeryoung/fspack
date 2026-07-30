# iter-110：cli_doctor.py 1115 行拆分为 facade + 5 子模块

## 需求清单

- [x] 拆分 cli_doctor.py 1115 行（项目最大文件）为 facade + 5 个职责子模块
- [x] 保持公开 API 不变（`__all__` 与 `from fspack.cli_doctor import` 路径兼容）
- [x] 全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
- [x] 性能基线无退化

## 迭代目标

按 iter-109 收尾确认的拆分方向，将 cli_doctor.py 按职责拆为 facade
+ doctor_models/doctor_envs/doctor_tools/doctor_report/doctor_bench/
doctor_templates，确保测试 monkeypatch 路径（`fspack.cli_doctor._xxx`）
继续生效，零破坏现有 1835 测试。

## 改动文件清单

新增：
- `src/fspack/doctor_models.py`（88 行）：核心数据类
  CheckStatus/CheckResult/DoctorReport/TemplateRunResult/TemplateBuildResult
- `src/fspack/doctor_envs.py`（101 行）：环境检查
  _check_python/_check_platform_info/_check_fspack_version/_check_mirror_config/
  _check_cache_dir + _dir_size + _format_size
- `src/fspack/doctor_tools.py`（192 行）：工具检查
  _VERSION_TIMEOUT + _check_tool_version + 9 个 _check_*
  （mingw/gcc/clang/nsis/makensis_on_linux/wine/pip/uv/pillow）
- `src/fspack/doctor_report.py`（68 行）：报告渲染
  print_doctor_report/_build_table/_format_status/_print_summary
- `src/fspack/doctor_bench.py`（257 行）：基准历史持久化与横向对比
  _machine_id/_collect_machine_info/_bench_history_group_dir/
  _serialize_bench_results/_deserialize_bench_results/_save_bench_history/
  _load_previous_bench_history/_format_bench_delta/_print_bench_comparison/
  _save_and_compare_bench
- `src/fspack/doctor_templates.py`（486 行）：模板构建测试与基准
  _RUN_TIMEOUT_SEC/_TERMINATE_GRACE_SEC/_logger +
  TemplateRunResult/TemplateBuildResult（已下沉 doctor_models）+
  _find_dist_exe/_build_run_cmd/_find_debug_python/_find_wrapper/
  _build_debug_cmd/_run_template/_build_single_template +
  run_doctor_test/run_doctor_bench +
  _print_template_build_summary/_print_run_summary/_format_run_status/
  _print_performance_analysis

修改：
- `src/fspack/cli_doctor.py`（1115 → 178 行 facade）：保留模块文档字符串 +
  run_doctor 编排逻辑 + re-export 全部公开 API 与测试需要的私有名。
  顶层 `import shutil`/`import subprocess` 保留为模块属性，使测试 patch
  `fspack.cli_doctor.shutil.which`/`subprocess.run`/`subprocess.Popen`
  能修改标准库模块属性全局生效。
- `tests/test_cli_doctor.py`：line 451 patch 路径从
  `fspack.cli_doctor._dir_size` 改为 `fspack.doctor_envs._dir_size`
  （`_check_cache_dir` 在 doctor_envs 调用 `_dir_size` 访问 doctor_envs
  命名空间，patch facade 无效）

清理：
- `.trae/docs/iter-105-doctor-multi-entry-run.md`（保留最新 5 条记录）

## 关键决策与依据

1. **数据类下沉 doctor_models.py 而非 facade**：CheckStatus/CheckResult/
   DoctorReport/TemplateRunResult/TemplateBuildResult 被 5 个子模块共享。
   若放 facade，子模块 `from fspack.cli_doctor import CheckResult` 会循环
   导入（facade 初始化未完成时子模块拿不到）。独立 doctor_models.py 切断
   循环，子模块单向依赖 doctor_models，facade 单向依赖子模块。依赖图：
   doctor_models →（无）→ doctor_envs/doctor_tools/doctor_report →
   doctor_bench（依赖 doctor_envs 的 _format_size）→
   doctor_templates（依赖 doctor_envs 的 _dir_size/_format_size + doctor_bench
   的 _save_and_compare_bench）→ cli_doctor facade（依赖全部）。

2. **facade 顶层 import shutil/subprocess 保留为模块属性**：测试通过
   `patch("fspack.cli_doctor.shutil.which", ...)` 与
   `monkeypatch.setattr("fspack.cli_doctor.subprocess.Popen", ...)` 修改
   标准库模块属性。这些 patch 的本质是 `getattr(cli_doctor, "shutil")`
   拿到 shutil 模块对象后 setattr 其 `which` 属性，全局生效（doctor_tools
   与 doctor_templates 用 shutil/subprocess 也受影响）。facade 必须
   `import shutil`/`import subprocess` 提供模块属性，加 `# noqa: F401`
   标注未直接使用。

3. **facade __all__ 包含私有名**：测试通过 `from fspack.cli_doctor import
   _check_pillow, _dir_size, ...` 显式 import 私有名，并通过
   `monkeypatch.setattr("fspack.cli_doctor._check_pillow", ...)` patch。
   facade 顶层 `from doctor_tools import _check_pillow` 把 `_check_pillow`
   绑定到 facade 命名空间，`run_doctor` 调用 `_check_pillow()` 访问 facade
   globals，patch `fspack.cli_doctor._check_pillow` 修改 facade globals 即
   生效。ruff F401 检测到这些 import 未在 facade 代码直接使用，需在
   `__all__` 中列出（iter-109 slim/qt.py 同模式）让 ruff 视为 re-export。
   `__all__` 列出私有名不影响 `from cli_doctor import *` 行为（私有名不会被
   `*` 导入），仅用于 ruff 静态检查。

4. **_check_cache_dir 的 _dir_size patch 路径必须改**：原测试 line 451
   patch `fspack.cli_doctor._dir_size`，但 `_check_cache_dir` 在 doctor_envs
   里调用 `_dir_size()` 访问 doctor_envs 命名空间，patch facade 无效。改为
   `fspack.doctor_envs._dir_size`。其他 `_check_*` patch 路径无需改——
   `run_doctor` 在 facade 调用它们，访问 facade globals，patch facade
   命名空间有效。

5. **_format_size/_dir_size 放 doctor_envs 而非 doctor_models**：这俩是
   环境检查的工具函数（递归目录大小、字节数格式化），被 doctor_templates
   与 doctor_bench 复用。放 doctor_envs 让 doctor_models 保持纯数据类
   职责单一。子模块从 doctor_envs import 这俩工具函数。

6. **_VERSION_TIMEOUT/_RUN_TIMEOUT_SEC/_TERMINATE_GRACE_SEC/_logger 各归
   其主**：_VERSION_TIMEOUT 仅 doctor_tools 用，放 doctor_tools；
   _RUN_TIMEOUT_SEC/_TERMINATE_GRACE_SEC/_logger 仅 doctor_templates 用，
   放 doctor_templates。不集中到 doctor_models 避免数据模块承载非数据职责。

## 代码实现情况

### 拆分前后对比

| 模块 | 拆分前 | 拆分后 |
|------|--------|--------|
| cli_doctor.py | 1115 行 | 178 行 facade |
| doctor_models.py | - | 88 行 |
| doctor_envs.py | - | 101 行 |
| doctor_tools.py | - | 192 行 |
| doctor_report.py | - | 68 行 |
| doctor_bench.py | - | 257 行 |
| doctor_templates.py | - | 486 行 |

facade 178 行含：模块文档字符串（32 行）+ import 语句（60 行）+
`__all__`（54 行）+ `run_doctor` 编排（30 行）。`run_doctor` 是 facade
唯一保留的业务逻辑，因为它需要按平台过滤调用各 `_check_*`，patch 路径
依赖 facade 命名空间。

### facade 模式

```python
# cli_doctor.py facade 关键结构
import shutil  # noqa: F401 - 测试 patch 需要
import subprocess  # noqa: F401 - 测试 patch 需要

from fspack.doctor_envs import _check_python, _check_pip, ...
from fspack.doctor_tools import _check_tool_version, ...
from fspack.doctor_models import CheckResult, CheckStatus, DoctorReport, ...
from fspack.doctor_report import print_doctor_report, ...
from fspack.doctor_templates import run_doctor_test, run_doctor_bench, ...
from fspack.doctor_bench import _save_and_compare_bench, ...

__all__ = [...]  # 含私有名让 ruff 视为 re-export

def run_doctor() -> DoctorReport:
    # 调用 _check_python() 等访问 facade globals，patch 路径有效
    ...
```

## 整合优化情况

- 无重复代码引入：facade 仅 re-export，子模块无交叉引用（除 doctor_templates
  → doctor_envs/doctor_bench 单向依赖）
- 无新风险：`__all__` 与 import 路径完全兼容，1835 测试全通过
- 新模块覆盖率：doctor_models 100%/doctor_envs 97%/doctor_tools 91%/
  doctor_report 100%/doctor_bench 98%/doctor_templates 98%。
  doctor_tools 91% 较低是因为 `_check_mingw`/`_check_gcc` 等函数体需要
  真实工具环境，测试通过 patch `_check_tool_version` 间接覆盖，未实际
  调用这些函数体——与拆分前同样的覆盖模式（拆分前 cli_doctor.py 也是
  这些行 missing）

## 测试验证结果

### 全套门禁

- `ruff check` / `ruff format --check`：All checks passed
- `pyrefly check`：0 errors
- `pytest --cov`：1835 passed, 12 skipped, 8 deselected
- 覆盖率 96.09%（≥ 95% 门禁，较 iter-109 的 96.07% 略升 0.02pp）

### 性能基线

cli_doctor 拆分不在性能基线测试范围（test_perf_baseline.py 测
classify_entry/collect_imports/source_fingerprint/slim_unpack/
analyze_dependencies），无基线对比需求。基线测试随全量 pytest 通过
未退化。

## 遗留事项

- req-40 iter-85（mixin Protocol 类型声明）、iter-86（配置加载缓存）、
  iter-87（AST 内存优化）、iter-88（测试 fixture 共享化）、iter-89（基线
  矩阵扩展）、iter-90（CI 门禁固化）状态待后续迭代核实与推进
- cli.py 749 行可参考 cli_doctor.py 拆分模式按子命令拆为
  cli_build.py/cli_package.py/cli_recursive.py 等，留 iter-111+
- nuitka_compile.py 669 行 iter-109 审查结论是职责集中无需拆分，维持现状

## 下一轮计划

- iter-111：核实 req-40 iter-85~90 完成状态；若已完成则补标，否则推进
  iter-85（mixin Protocol 类型声明，pyrefly 抑制警告降至 ≤10）
- iter-112+：cli.py 749 行拆分（按子命令拆为 cli_build/cli_package/
  cli_recursive 等），参考本次 cli_doctor.py 拆分模式
