# iter-105: 修复多入口模板 doctor 运行验证跳过问题

## 需求清单

- [x] 定位 doctor --test 跳过多入口模板运行验证的根因
- [x] 修改 _build_single_template 用 ProjectInfo.all_entries[0] 取首个入口名
- [x] 更新 _print_run_summary / _format_run_status 注释（移除"多入口"措辞）
- [x] 本地验证 multi_entry_py310 模板运行验证通过
- [x] 全套门禁通过（ruff / pyrefly / pytest）

## 迭代目标

用户反馈"多入口 doctor 测试不该跳过，应该选择一个入口测试"。`fsp doctor --test`
对多入口模板（如 `multi_entry_py310`）构建成功后跳过运行验证，因为代码用
`template.name`（如"多入口项目（CLI + GUI）"）作为入口名查找 exe，但多入口
项目产出的 exe 名是 `[tool.fspack.entries]` 的键（如 cli/gui/web），不等于
`template.name`，找不到 exe 就 `run_result=None` 跳过。

## 根因分析

`_build_single_template`（[cli_doctor.py:786](file:///f:/Dev/fspack/src/fspack/cli_doctor.py#L786)）
原代码：

```python
debug = _build_debug_cmd(proj_dir, template.name)  # template.name = "多入口项目（CLI + GUI）"
exe = _find_dist_exe(proj_dir, template.name)      # 找 dist/多入口项目（CLI + GUI）.exe → None
```

`multi_entry_py310` 模板的 `[tool.fspack.entries]` 声明了 3 个入口：
`cli = "cli.py"`、`gui = "gui.py"`、`web = "web.py"`。构建产出 `cli.exe`/
`gui.exe`/`web.exe`，但 doctor 用 `template.name` 找 exe，找不到就跳过。

`fsp r` 命令的 `runner.py` 已通过 `_select_entry(info, entry=None)` 处理多入口
（返回首个入口），doctor 应复用此逻辑。

## 改动文件清单

### 修改

- `src/fspack/cli_doctor.py`
  - `_build_single_template`：构建成功后用 `ProjectInfo.from_dir(proj_dir)
    .all_entries[0].name` 取首个入口名（与 `fsp r` 默认行为一致），替换
    `template.name` 传给 `_build_debug_cmd` / `_find_dist_exe`
  - 移除注释"多入口项目产出的 exe 名与 template.name 不一致时跳过验证"
  - `_logger.debug` 消息从"未找到匹配的可执行文件（多入口？）"改为
    "未找到入口 {entry_name} 的可执行文件"
  - `_print_run_summary` docstring 与消息：`run_skip` 注释从"多入口或未找到
    可执行文件"改为"未找到可执行文件"
  - `_format_run_status` docstring：`run_result=None` 注释从"多入口或未找到
    exe"改为"未找到 exe"

## 关键决策与依据

1. **用 `ProjectInfo.all_entries[0]` 而非 `template.name`**：`all_entries`
   统一处理单入口与多入口——单入口返回 `(EntryPoint(name=self.name, ...),)`，
   多入口返回 `entries` 元组。`[0]` 取首个入口，与 `fsp r` 的
   `_select_entry(entry=None)` 行为一致。`from_dir` 有 `lru_cache`，构建时
   已缓存，再次调用零开销。

2. **选首个入口而非全部入口**：doctor 是回归门禁，选一个有代表性的入口
   验证即可。首个入口通常是 CLI（pyproject.toml 中 `cli = "cli.py"` 在最前，
   Python 3.7+ dict 保序），CLI 入口 print 后退出，最适合自动化测试（不会
   像 GUI 那样挂起需要超时）。全部入口测试会增加 CI 时间且 GUI 入口需超时
   策略，收益有限。

3. **不 import `runner.py` 复用 `_select_entry`**：`cli_doctor.py` 注释明确
   "独立于 runner 模块以避免 doctor ↔ runner 循环依赖"。`all_entries[0]`
   一行代码即可替代，无需引入循环依赖。

## 代码实现情况

- `_build_single_template` 构建成功后新增 3 行：
  ```python
  from fspack.config import ProjectInfo
  entry_name = ProjectInfo.from_dir(proj_dir).all_entries[0].name
  ```
- 后续 `_build_debug_cmd(proj_dir, entry_name)` / `_find_dist_exe(proj_dir,
  entry_name)` 用 `entry_name` 替换 `template.name`
- 注释与日志消息清理"多入口"措辞，统一为"未找到可执行文件"

## 整合优化情况

- 无重复代码引入
- 无新风险：`ProjectInfo.from_dir` 在构建成功后调用，pyproject.toml 已验证
  有效，不会失败；`all_entries` 至少返回 1 个入口，`[0]` 不会越界
- 行为变化：多入口模板从"跳过运行验证"变为"用首个入口验证"，与 `fsp r`
  默认行为一致

## 测试验证结果

### 本地端到端验证

用 Python 直接调用 `_build_single_template` 测试 `multi_entry_py310` 模板：

- 构建成功：3 个入口（cli.exe、gui.exe、web.exe）全部生成 ✓
- **修复前**：`run_result=None`（跳过运行验证）
- **修复后**：`run_result.success=True, exit_code=0`（用首个入口 `cli` 验证通过）

### 门禁检查

- ruff check / format：All checks passed
- pyrefly：0 errors
- pytest test_cli_doctor.py：93 passed

## 遗留事项

- 无

## 下一轮计划

- 继续 req-47 阶段 4 后续：启动速度优化（iter-106）
