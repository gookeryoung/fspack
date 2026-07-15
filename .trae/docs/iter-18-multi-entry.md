# 迭代 18：单项目多入口打包

## 迭代目标

支持单个项目通过 `[tool.fspack.entries]` 声明多个入口，每个入口生成独立 exe，
共享项目依赖与 Python 运行时；多入口支持混合 cli/gui/web 类型，按脚本自身
import 推断每个入口的 app_type。新增 `examples/multi_entry` 示例（cli+gui+web
三入口）。

## 需求清单

- [x] `pyproject.toml` 新增 `[tool.fspack.entries]` 表声明多入口
- [x] 多入口共享 runtime/deps/src，仅生成多个 exe
- [x] 多入口混合类型支持（cli/gui/web）
- [x] `fsp r --entry <name>` 选择入口运行
- [x] 单入口完全向后兼容
- [x] C loader 缓存跨项目复用不受影响
- [x] 示例 `examples/multi_entry` 含 cli+gui+web 三入口
- [x] 测试覆盖多入口功能，门禁通过

## 改动文件清单

### 核心数据结构与解析

- `src/fspack/config.py`：
  - 新增 `EntryPoint` dataclass（frozen），含 `name`/`module`/`file`/`app_type`
    字段
  - `EntryPoint.from_script(name, script_path)` 类方法：惰性导入
    `infer_app_type`，**只按脚本 import 推断 app_type**（不传 declared，
    多入口项目共享 declared，不能据项目级依赖判断单个入口类型）
  - `EntryPoint.entry_rel(src_dir)` 方法：返回入口脚本相对源码目录的
    POSIX 路径
  - `ProjectInfo` 新增 `entries: tuple[EntryPoint, ...] = ()` 字段（默认空，
    表示单入口模式）
  - `ProjectInfo.all_entries` 属性：统一接口，单入口模式构造单一 EntryPoint，
    多入口模式返回 entries
- `src/fspack/project.py`：
  - 导入 `EntryPoint` 与 `Any`
  - `infer_app_type` 从私有 `_infer_app_type` 改为公开函数（供 EntryPoint.
    from_script 调用）
  - `parse_project` 新增 `[tool.fspack.entries]` 解析逻辑：声明多入口时填
    `entries`，首个入口作为主入口（`entry_module`/`entry_file`/`app_type`
    取首个，保持向后兼容）
  - 新增 `_parse_entries` 函数：校验入口名、脚本路径存在性，构造 EntryPoint
    元组
  - 显式 `dict[str, Any]` 类型注解修复 pyrefly `implicit-any-empty-container`

### C loader 改造

- `src/fspack/loader.py`：
  - Windows 与 Linux C 源码均新增 `split_exe` 函数：拆分 exe 路径为 dir 与
    basename（去 `.exe` 后缀）
  - `read_entry` 改为接收 `exe_path` 参数：先读 `<dir>\<base>.entry`（多入口
    模式），回退 `<dir>\.entry`（单入口兼容）
  - `generate_loader_source` 签名不变（仍 `(py_xy, platform)`），loader 源码
    仅依赖 `py_xy` 与平台，缓存键 `(py_xy, app_type, platform)` 不变

### 构建流水线

- `src/fspack/builder.py`：
  - "生成 C loader" 阶段循环 `info.all_entries`，为每个入口：
    - 计算入口脚本相对源码目录的 POSIX 路径
    - 多入口模式写 `<name>.entry`，单入口模式写 `.entry`（向后兼容）
    - 按 `app_type` 编译 loader（GUI 加 `-mwindows`）
    - 收集 exe 路径列表
  - 单入口输出 `构建完成: <exe>`，多入口输出 `构建完成: N 个入口` 列出所有 exe

### 运行入口选择

- `src/fspack/commands/run.py`：
  - `run` 函数新增 `entry: str | None = None` 参数
  - 新增 `_select_entry(info, entry)` 函数：`entry=None` 返回首个入口（多入口
    日志提示可指定 `--entry`），按名匹配，未找到报错列出可用入口
  - `_build_debug_cmd` 改为接收 `EntryPoint` 参数（用 `ep.file` 计算入口路径）
  - `_find_exe` 改为接收 `name` 参数（按入口名查找 dist 下 exe）
- `src/fspack/cli.py`：
  - `p_run` 新增 `--entry` 参数，`help="多入口项目指定要运行的入口名
    （与 [tool.fspack.entries] 键匹配）"`
  - dispatch 传 `entry=ns.entry`

### 示例

- `examples/multi_entry/`（新建）：
  - `pyproject.toml`：`[tool.fspack.entries]` 声明 cli/gui/web 三入口，
    `dependencies = ["PySide2>=5.15.2", "flask"]`，
    `requires-python = ">=3.8,<3.11"`
  - `.python-version`：`3.10`（PySide2 5.15.2.1 wheel 含 cp310 标签 + click 8.2+
    的 PEP 634 match 语法需 3.10+）
  - `cli.py`：CLI 入口，`print("hello from multi_entry cli")`
  - `gui.py`：PySide2 GUI 入口，显示带文字的 QLabel 窗口，offscreen 模式
    QTimer 1s 后退出（参考 pyside2_app 示例模式）
  - `web.py`：flask `test_client()` 验证路由响应（不启动开发服务器，避免
    `fsp r` 挂起）

### 测试

- `tests/test_project.py`：新增 9 个测试
  - `test_parse_project_multi_entry_example`：multi_entry 示例解析为三个入口
  - `test_parse_project_multi_entry_single_declared_compat`：无 entries 走单入口
  - `test_parse_project_multi_entry_missing_script`：脚本不存在报错
  - `test_parse_project_multi_entry_empty_path`：脚本路径为空报错
  - `test_infer_app_type_by_import`：按 import 推断
  - `test_infer_app_type_by_declared`：按 declared 推断
  - `test_entry_point_from_script`：EntryPoint.from_script 测试
  - `test_entry_point_entry_rel`：entry_rel 方法测试
- `tests/test_config.py`：新增 3 个测试
  - `test_project_info_all_entries_single`：单入口 all_entries
  - `test_project_info_all_entries_multi`：多入口 all_entries
  - `test_project_info_from_dir_multi_entry`：from_dir 解析多入口
- `tests/test_commands.py`：新增 6 个测试
  - `test_select_entry_default_returns_first`：默认返回首个
  - `test_select_entry_by_name`：按名匹配
  - `test_select_entry_not_found`：未找到报错
  - `test_select_entry_single_project_no_warn`：单入口不提示
  - `test_run_run_multi_entry_select`：`fsp r --entry gui` 运行对应 exe
- `tests/test_cli.py`：
  - 修复 `fake_run` 签名添加 `entry: str | None = None` 参数
  - 新增 `test_run_entry_flag` 测试

## 关键决策与依据

### 1. EntryPoint dataclass 封装入口元信息

`EntryPoint` 含 `name`（exe 名）/`module`（脚本 stem）/`file`（脚本绝对路径）
/`app_type`（CLI/GUI）四字段，`from_script` 类方法构造，`entry_rel` 方法返回
相对源码目录的 POSIX 路径。封装后调用方无需手动拼接路径或推断类型，统一通过
EntryPoint 访问。

### 2. all_entries 统一接口

`ProjectInfo.all_entries` 属性统一单入口与多入口两种模式：单入口构造单一
EntryPoint（用项目级 `entry_module`/`entry_file`/`app_type`），多入口返回
`entries` 字段。调用方（builder/run）只迭代 `all_entries`，无需分支判断模式。

### 3. 多入口 app_type 按脚本 import 推断

`EntryPoint.from_script` 调用 `infer_app_type(script_path, ())`（不传 declared）。
原因：多入口项目共享 `[project.dependencies]`，若传 declared，含 PySide2 的
项目所有入口都会被判为 GUI（即使 cli.py 不 import PySide2）。每个入口按自身
import 推断类型，才能正确区分 cli/gui/web。

### 4. C loader 入口文件查找：先 `<base>.entry` 后 `.entry`

C loader 运行时从 `GetModuleFileNameW`/`readlink("/proc/self/exe")` 获取 exe
路径，`split_exe` 拆分为 dir 与 basename（去 `.exe`），先尝试
`<dir>\<base>.entry`（多入口模式），失败回退 `<dir>\.entry`（单入口兼容）。
这样单入口项目旧 dist 仍可运行，多入口项目每个 exe 找到对应入口文件。

### 5. loader 缓存键不变

`_loader_cache_key = sha256(source + app_type + platform)`，source 仅含
`py_xy` 与平台（不含入口路径），app_type 由入口自身决定。同 py_xy + 同 app_type
+ 同平台的不同入口共享同一缓存文件（如 multi_entry 的 cli.exe 与 web.exe 都是
CLI 类型，共享一个缓存；gui.exe 是 GUI 类型，单独缓存）。

### 6. 单入口向后兼容

无 `[tool.fspack.entries]` 时走原 `detect_entry` 路径，`entries=()`，`all_entries`
构造单一 EntryPoint，builder 写 `.entry` 文件，loader 回退路径命中。已存在的
单入口项目 dist 仍可运行。

### 7. multi_entry 用 .python-version=3.10

PySide2 5.15.2.1 wheel 含 cp310 标签（兼容 3.10），但 click 8.2+ 使用 PEP 634
match 语法需 Python 3.10+。`.python-version=3.9` 会导致 flask 的 click 依赖
`SyntaxError`。改为 `3.10` 同时满足 PySide2 兼容与 click 语法要求。

### 8. web 入口用 test_client 不启动服务器

`web.py` 用 `flask.test_client()` 验证路由响应，不调用 `app.run()`。原因：
`app.run()` 启动开发服务器会阻塞，`fsp r` 永不退出。test_client 在进程内模拟
HTTP 请求，验证后正常退出，适合打包验证。

## 代码实现情况

### EntryPoint dataclass（config.py）

```python
@dataclass(frozen=True)
class EntryPoint:
    """单个打包入口：用于多入口项目生成多个可执行文件。."""
    name: str
    module: str
    file: Path
    app_type: AppType

    @classmethod
    def from_script(cls, name: str, script_path: Path) -> EntryPoint:
        from fspack.project import infer_app_type
        return cls(
            name=name,
            module=script_path.stem,
            file=script_path,
            app_type=infer_app_type(script_path, ()),  # 多入口只看 import
        )

    def entry_rel(self, src_dir: Path) -> str:
        return self.file.relative_to(src_dir).as_posix()
```

### all_entries 统一接口（config.py）

```python
@property
def all_entries(self) -> tuple[EntryPoint, ...]:
    """所有入口：多入口模式返回 entries，单入口模式构造单一入口。."""
    if self.entries:
        return self.entries
    return (
        EntryPoint(
            name=self.name,
            module=self.entry_module,
            file=self.entry_file,
            app_type=self.app_type,
        ),
    )
```

### 多入口构建循环（builder.py）

```python
exes: list[Path] = []
with tracker.stage("生成 C loader") as st:
    source = generate_loader_source(info.py_xy, target)
    build_dir = cfg.dist_dir / "build"
    for ep in info.all_entries:
        entry_rel = ep.entry_rel(info.src_dir)
        entry_file_in_dist = f"src/{entry_rel}"
        if info.entries:
            (cfg.dist_dir / f"{ep.name}.entry").write_text(entry_file_in_dist, encoding="utf-8")
        else:
            (cfg.dist_dir / ".entry").write_text(entry_file_in_dist, encoding="utf-8")
        exe_name = f"{ep.name}.exe" if target is Platform.WINDOWS else ep.name
        exe = cfg.dist_dir / exe_name
        compile_loader(source, exe, ep.app_type, build_dir, target, stage=st)
        exes.append(exe)
    st.processed(len(exes))
```

### C loader read_entry（loader.py Windows 版）

```c
static void split_exe(const wchar_t *exe_path, wchar_t *dir, size_t dir_cap,
                      wchar_t *base, size_t base_cap) {
    /* 拆分 dir 与 base，去除 .exe 后缀 */
}

static int read_entry(const wchar_t *exe_path, wchar_t *entry_out, size_t cap) {
    wchar_t dir[MAX_PATH], base[MAX_PATH], path[MAX_PATH];
    split_exe(exe_path, dir, MAX_PATH, base, MAX_PATH);
    /* 多入口模式：<dir>\<base>.entry */
    _snwprintf(path, MAX_PATH, L"%s\\%s.entry", dir, base);
    FILE *f = _wfopen(path, L"rb");
    if (!f) {
        /* 单入口模式回退：<dir>\.entry */
        _snwprintf(path, MAX_PATH, L"%s\\.entry", dir);
        f = _wfopen(path, L"rb");
        if (!f) { /* 报错 */ return 1; }
    }
    /* 读取内容 */
}
```

### --entry 选择入口（commands/run.py）

```python
def _select_entry(info: ProjectInfo, entry: str | None) -> EntryPoint:
    all_entries = info.all_entries
    if entry is None:
        if len(all_entries) > 1:
            names = ", ".join(ep.name for ep in all_entries)
            _logger.info("多入口项目未指定 --entry，使用首个入口 %s（可用: %s）",
                         all_entries[0].name, names)
        return all_entries[0]
    for ep in all_entries:
        if ep.name == entry:
            return ep
    available = ", ".join(ep.name for ep in all_entries)
    raise FspackError(f"未找到入口: {entry}（可用入口: {available}）")
```

## 整合优化情况

- `infer_app_type` 公开化：从 `_infer_app_type` 改为 `infer_app_type`，供
  `EntryPoint.from_script` 调用，避免暴露私有函数
- `all_entries` 属性统一接口：消除 builder/run 中的单入口/多入口分支判断
- C loader 缓存键不变：多入口共享缓存，不增加缓存文件数
- 单入口完全兼容：无 `[tool.fspack.entries]` 走原路径，旧 dist 仍可运行

## 测试验证结果

### 门禁

- `ruff check`：All checks passed
- `ruff format --check`：45 files already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack`：323 passed，覆盖率 97.86%

### 新增测试

- `tests/test_project.py`：+9（多入口解析、infer_app_type、EntryPoint）
- `tests/test_config.py`：+3（all_entries、from_dir 多入口）
- `tests/test_commands.py`：+6（_select_entry、多入口运行）
- `tests/test_cli.py`：+1（--entry 参数解析）

### 手动验证

- `fsp b examples\multi_entry` 构建成功，生成 3 个 exe（cli.exe/gui.exe/web.exe）
- `fsp r examples\multi_entry --entry cli` 输出 "hello from multi_entry cli"
- `fsp r examples\multi_entry --entry web` 输出 "hello from multi_entry web"
- `fsp r examples\multi_entry --entry gui --debug`（offscreen 模式）输出
  "hello from multi_entry gui"

## 遗留事项

- multi_entry 未纳入 slow 端到端测试矩阵（`tests/test_e2e_slow.py`）：slow 测试
  需要工具链（mingw+wine/gcc）且耗时，本迭代用手动验证 + 单元测试覆盖。后续可
  添加 slow 测试函数 `test_build_and_run_multi_entry` 验证三个入口运行结果。
- `[tool.fspack.entries]` 暂不支持按入口声明独立依赖（所有入口共享项目级
  `[project.dependencies]`）。若需按入口差异化依赖，可在后续迭代增加
  `[tool.fspack.entries.<name>]` 子表声明。

## 下一轮计划

无。本迭代需求清单全部完成，门禁通过。
