# iter-36: BuildOptions 封装与 build 命令层整合

## 需求清单

- [x] 整合 `commands/build.py` 单函数层，CLI 直接调用 `builder.build()`
- [x] 将 `build()` 的 8 个开关参数封装为 `BuildOptions` dataclass
- [x] 评估 `commands/run.py` 是否需要 `RunOptions`（结论：不需要）

## 迭代目标

用户指出 `commands/build.py` 仅含一个 `run()` 转发函数，是冗余中间层；同时 `builder.build()` 参数过多（含 8 个开关），违反 `rule-11` 函数参数 ≤ 5 的约束。本次迭代消除冗余层并将开关聚合为 `BuildOptions` dataclass，便于扩展与透传。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| src/fspack/commands/build.py | 删除（冗余中间层） |
| src/fspack/config.py | 新增 `BuildOptions` frozen dataclass，聚合 8 个构建开关 |
| src/fspack/builder.py | `build()` 签名收编为 `options: BuildOptions \| None = None`；内部用 `opts = options or BuildOptions()`；`noqa: PLR0913` 保留（路径参数仍超 5） |
| src/fspack/cli.py | `build/b` 子命令直接 `from fspack.builder import build` 并构造 `BuildOptions` 透传 |
| tests/test_builder.py | 测试用例改用 `options=BuildOptions(...)` 传参 |
| tests/test_cli.py | mock 目标改为 `fspack.builder.build`；新增 `_capture_build` 校验 `BuildOptions` 字段 |
| tests/test_commands.py | 删除 5 个 build 相关测试（已无 `commands.build`） |
| tests/test_e2e_slow.py | `keep_modules=...` 改为 `options=BuildOptions(keep_modules=...)` |

## 关键决策与依据

### `BuildOptions` 与 `BuildConfig` 职责划分

- `BuildConfig`：封装路径与镜像配置（必需，源自 `pyproject.toml` 与镜像源）
- `BuildOptions`：封装构建行为开关（可选，默认值对应原 `build()` 行为）

`BuildOptions` 为 `frozen=True` dataclass，字段全部带默认值，调用方按需覆盖。8 个字段：`keep_modules`/`icon`/`no_stdlib_trim`/`no_pyc`/`pyc_strip`/`pyc_optimize`/`no_site`/`nuitka`。

### `commands/build.py` 整合

`commands/build.py` 仅含一个 `run()` 函数，纯转发 `builder.build()`。删除后 CLI 直接调用 `builder.build()`，减少一层无意义包装。`commands/run.py`、`commands/clean.py`、`commands/package.py` 保留（各自有非平凡逻辑）。

### `RunOptions` 评估结论：不需要

`commands/run.py` 的 `run()` 仅 4 个参数（`project`/`rest_args`/`debug`/`entry`），未超 `rule-11` 的 ≤ 5 阈值，无需封装。`debug` 与 `entry` 是高频独立开关，封装为 dataclass 反而增加调用方负担。

### `PLR0913` 处理

`builder.build()` 整合后仍有 7 个参数（`project`/`mirror`/`py_version`/`dist_dir`/`embed_cache`/`target`/`options`），加 `noqa: PLR0913`。这些是路径/配置参数，与 `BuildOptions`（行为开关）职责不同，强行封装会混淆必需与可选语义。

## 代码实现情况

### config.py BuildOptions

```python
@dataclass(frozen=True)
class BuildOptions:
    """构建行为开关（不影响产物路径与运行时环境）。"""
    keep_modules: set[str] | None = None
    icon: Path | None = None
    no_stdlib_trim: bool = False
    no_pyc: bool = False
    pyc_strip: bool = False
    pyc_optimize: int = 0
    no_site: bool = False
    nuitka: bool = False
```

### builder.py build() 签名

```python
def build(  # noqa: PLR0912, PLR0913
    project: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    dist_dir: Path | None = None,
    embed_cache: Path | None = None,
    target: Platform | None = None,
    options: BuildOptions | None = None,
) -> ProjectInfo:
    opts = options or BuildOptions()
    # ... 后续引用 opts.keep_modules / opts.icon / opts.no_pyc 等
```

### cli.py build 分发

```python
if command in ("build", "b"):
    from fspack.builder import build
    from fspack.config import BuildOptions, get_mirror

    build(
        project,
        get_mirror(ns.mirror),
        ns.py_version,
        target=_parse_target(ns.target),
        options=BuildOptions(
            keep_modules=set(ns.keep_modules) if ns.keep_modules else None,
            icon=Path(ns.icon).resolve() if ns.icon else None,
            no_stdlib_trim=ns.no_stdlib_trim,
            no_pyc=ns.no_pyc,
            pyc_strip=ns.pyc_strip,
            pyc_optimize=ns.pyc_optimize,
            no_site=ns.no_site,
            nuitka=ns.nuitka,
        ),
    )
```

## 整合优化情况

- 删除 `commands/build.py` 后，`commands/` 仅保留有非平凡逻辑的 `run.py`/`clean.py`/`package.py`
- `BuildOptions` 为后续新增构建开关提供统一扩展点，CLI → builder 透传链路稳定
- 测试侧 `_capture_build` helper 集中校验 `BuildOptions` 字段，避免每个用例重复断言

## 测试验证结果

- ruff check: All checks passed
- ruff format --check: 50 files already formatted
- pyrefly check: 0 errors (59 suppressed, 7 warnings not shown)
- pytest -m "not slow": 742 passed, 21 deselected, 覆盖率 97.37%
- 测试调整：
  - `test_builder.py`：`keep_modules=...` 改为 `options=BuildOptions(keep_modules=...)`，新增 `BuildOptions` 导入
  - `test_cli.py`：mock 改为 `fspack.builder.build`，新增 `_capture_build` 校验 `BuildOptions` 字段；`test_build_pyc_options_default_false` 断言所有开关默认值
  - `test_commands.py`：删除 5 个 `commands.build` 相关测试
  - `test_e2e_slow.py`：`keep_modules=...` 改为 `options=BuildOptions(keep_modules=...)`

## 遗留事项

- `builder.build()` 仍带 `noqa: PLR0913`，若未来进一步封装路径参数（如 `BuildProject` dataclass）可消除
- `commands/run.py` 的 `run()` 参数未变，保持 4 参数直传

## 下一轮计划

无明确下一轮计划，等待用户反馈。
