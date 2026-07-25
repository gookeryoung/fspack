# req-29 BuildOptions 封装与 build 命令层整合

## 需求

- [x] 整合 `commands/build.py` 单函数层（CLI 直接调用 `builder.build()`）
- [x] 将 `builder.build()` 的 8 个开关参数封装为 `BuildOptions` dataclass
- [x] 评估 `commands/run.py` 是否需要 `RunOptions`（结论：不需要，参数仅 4 个）

## 背景

`commands/build.py` 仅含一个 `run()` 转发函数，是冗余中间层；`builder.build()` 含 8 个开关参数（`keep_modules`/`icon`/`no_stdlib_trim`/`no_pyc`/`pyc_strip`/`pyc_optimize`/`no_site`/`nuitka`），违反 `rule-11` 函数参数 ≤ 5 约束。需消除冗余层并将开关聚合为 dataclass。

## 实现

详见 `iter-36-BuildOptions封装与build模块整合.md`。

- `config.py` 新增 `BuildOptions` frozen dataclass 聚合 8 个构建开关
- `builder.py` `build()` 签名收编为 `options: BuildOptions | None = None`
- `cli.py` `build/b` 子命令直接调用 `builder.build()` 并构造 `BuildOptions` 透传
- 删除 `commands/build.py`
